"""The agent loop: one customer turn, at most six LLM calls (D-001, DESIGN section 4).

    prepare   rebuild the prompt from the database (D-005): system prompt, every earlier turn's
              messages replayed unchanged, and one new user message (context note + customer
              text). Record the turn.
    loop      call the model. Tool calls run through the session-scoped executor, one
              transaction each, and their results go back to the model. A text reply goes
              through the output guard; a rejected reply gets one rewrite.
    finish    record how the turn ended. Return the reply, or report failure so the pipeline
              sends the fixed fallback and hands the conversation to the owner.

Limits, each ending the turn in the fallback path: 6 LLM calls (the last one is made with
``tool_choice="none"`` so the model has to answer in words), a 45 s wall-clock budget, 20 s per
call, and one retry per call for timeouts, rate limits, and 5xx responses.

A handoff escalation ends the turn immediately with the fixed template from the escalation
policy (D-011). The model never gets another call after it, so it can't book through an
emergency or talk over the owner.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import guard
from app.agent.context import (
    interim_lines,
    load_profile,
    merge_adjacent_user_messages,
    render_system_prompt,
    render_turn_note,
)
from app.agent.executor import SKIPPED, Scope, ToolExecutor, ToolOutcome, parse_call
from app.agent.prompts import SYSTEM_PROMPT_NAME, prompt_version, system_template
from app.agent.providers.base import LLMError, LLMProvider, LLMResult, Message, ToolChoice
from app.agent.tools import tool_specs
from app.db.models import AgentTurn, Business, Conversation, Customer, LLMCall, ToolCallRecord
from app.domain.booking import upcoming_bookings
from app.domain.clock import Clock
from app.domain.conversations import transcript
from app.domain.enums import AgentTurnOutcome, MessageAuthor, MessageStatus
from app.domain.notifications import OwnerAlert
from app.pipeline.inbound import AgentTurnInput, AgentTurnOutput

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentConfig:
    model: str = "deepseek-flash"
    max_steps: int = 6
    turn_budget_seconds: float = 45.0
    call_timeout_seconds: float = 20.0
    retry_backoff_seconds: float = 1.0
    # Don't start a call with less than this left in the turn budget.
    min_call_seconds: float = 2.0
    prompt_name: str = SYSTEM_PROMPT_NAME


@dataclass(frozen=True)
class _Finish:
    outcome: AgentTurnOutcome
    reply: str | None = None
    author: MessageAuthor = MessageAuthor.AGENT
    error: str | None = None


@dataclass(frozen=True)
class _Prepared:
    turn_id: uuid.UUID
    scope: Scope
    prompt_version: str
    messages: list[Message]  # system prompt + replayed history + this turn's user message
    user: Message
    tools: list[dict[str, Any]]


@dataclass
class _State:
    messages: list[Message]  # everything sent to the model
    appended: list[Message]  # this turn's slice of the transcript, stored for replay
    alerts: list[OwnerAlert] = field(default_factory=list)

    def append(self, message: Message) -> None:
        self.messages.append(message)
        self.appended.append(message)


def _ms_since(began: float) -> int:
    return round((time.monotonic() - began) * 1000)


def replayable(messages: list[Message]) -> list[Message]:
    """Drop a trailing assistant tool-call message whose results never arrived.

    Stored turns are replayed into every later prompt (D-030), and the API rejects a tool call
    without its result. A crash between the two would otherwise poison the whole session."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            asked = {call["id"] for call in message["tool_calls"]}
            answered = {m.get("tool_call_id") for m in messages[index + 1 :]}
            return messages if asked <= answered else messages[:index]
    return messages


class Agent:
    """Implements the pipeline's ``TurnAgent`` port."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        provider: LLMProvider,
        clock: Clock,
        config: AgentConfig | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._provider = provider
        self._clock = clock
        self.config = config or AgentConfig()

    async def run_turn(self, turn: AgentTurnInput) -> AgentTurnOutput:
        started = time.monotonic()
        prepared = await self._prepare(turn)
        state = _State(list(prepared.messages), [prepared.user])
        try:
            finish = await self._loop(prepared, state, started + self.config.turn_budget_seconds)
        except Exception as exc:
            logger.exception("agent turn %s crashed", prepared.turn_id)
            finish = _Finish(AgentTurnOutcome.ERROR, error=f"{type(exc).__name__}: {exc}")
        await self._finish(prepared, state, finish, started)

        alerts = tuple(state.alerts)
        if finish.outcome in (AgentTurnOutcome.REPLIED, AgentTurnOutcome.HANDED_OFF):
            return AgentTurnOutput(finish.reply, finish.author, alerts, turn_id=prepared.turn_id)
        failed = finish.outcome.value + (f": {finish.error}" if finish.error else "")
        return AgentTurnOutput(None, alerts=alerts, failed=failed, turn_id=prepared.turn_id)

    # ----------------------------------------------------------------------------------------
    # prepare

    async def _prepare(self, turn: AgentTurnInput) -> _Prepared:
        async with self._sessionmaker() as session, session.begin():
            business = await session.get(Business, turn.business_id)
            customer = await session.get(Customer, turn.customer_id, populate_existing=True)
            conversation = await session.get(Conversation, turn.conversation_id)
            assert business is not None and customer is not None and conversation is not None
            now = self._clock.now()
            profile = await load_profile(session, business)
            upcoming = await upcoming_bookings(session, business, customer, self._clock)

            # Turns that finished. A turn with no outcome crashed mid-way; its messages are
            # still pending and come back as this turn's (or a later turn's) triggers.
            previous = list(
                await session.scalars(
                    select(AgentTurn)
                    .where(
                        AgentTurn.conversation_id == conversation.id,
                        AgentTurn.outcome.is_not(None),
                    )
                    .order_by(AgentTurn.seq)
                )
            )
            seen_up_to = previous[-1].max_message_seq if previous else 0
            triggers = set(turn.message_ids)
            log = await transcript(session, conversation.id)
            interim = [
                m
                for m in log
                if m.seq > seen_up_to
                and m.agent_turn_id is None
                and m.id not in triggers
                and m.status != MessageStatus.RECEIVED
            ]
            max_seq = max(
                [seen_up_to, *(m.seq for m in interim), *(m.seq for m in log if m.id in triggers)]
            )

            user: Message = {
                "role": "user",
                "content": render_turn_note(
                    now=now,
                    profile=profile,
                    customer=customer,
                    upcoming=upcoming,
                    interim=interim_lines(interim, business.owner_name),
                    customer_texts=turn.texts,
                ),
            }
            system: Message = {
                "role": "system",
                "content": render_system_prompt(profile, system_template(self.config.prompt_name)),
            }
            history = [m for t in previous for m in t.messages]
            version = prompt_version(self.config.prompt_name)
            record = AgentTurn(
                conversation_id=conversation.id,
                trigger_message_ids=list(turn.message_ids),
                max_message_seq=max_seq,
                model=self.config.model,
                prompt_version=version,
                messages=[user],
                started_at=now,
            )
            session.add(record)
            await session.flush()
            return _Prepared(
                turn_id=record.id,
                scope=Scope(business.id, customer.id, conversation.id),
                prompt_version=version,
                messages=[system, *merge_adjacent_user_messages([*history, user])],
                user=user,
                tools=tool_specs(profile.service_codes),
            )

    # ----------------------------------------------------------------------------------------
    # loop

    async def _loop(self, prepared: _Prepared, state: _State, deadline: float) -> _Finish:
        max_steps = self.config.max_steps
        rejected = 0
        for step in range(1, max_steps + 1):
            last_step = step == max_steps
            called = await self._call(
                prepared, state, step, "none" if last_step else "auto", deadline
            )
            if isinstance(called, _Finish):
                return called
            result, llm_call_id = called

            if result.tool_calls:
                if last_step:
                    return _Finish(
                        AgentTurnOutcome.STEP_LIMIT,
                        error="the model asked for tools on its last allowed call",
                    )
                state.append(result.assistant_message())
                finish = await self._run_tools(prepared, state, result, llm_call_id)
                await self._save_messages(prepared, state)
                if finish is not None:
                    return finish
                continue

            checked = guard.check_reply(result.content)
            if checked.ok:
                message = result.assistant_message()
                message["content"] = checked.text
                state.append(message)
                return _Finish(AgentTurnOutcome.REPLIED, reply=checked.text)
            rejected += 1
            if rejected >= 2:
                return _Finish(AgentTurnOutcome.GUARD_FAILED, error="; ".join(checked.problems))
            # The model sees its rejected reply and why; the stored transcript keeps neither,
            # so later turns only ever see what the customer actually received.
            state.messages.append(result.assistant_message())
            state.messages.append({"role": "user", "content": guard.feedback(checked.problems)})
        return _Finish(AgentTurnOutcome.STEP_LIMIT, error=f"no reply after {max_steps} LLM calls")

    async def _call(
        self,
        prepared: _Prepared,
        state: _State,
        step: int,
        tool_choice: ToolChoice,
        deadline: float,
    ) -> tuple[LLMResult, uuid.UUID] | _Finish:
        config = self.config
        error: LLMError | None = None
        for attempt in (1, 2):
            remaining = deadline - time.monotonic()
            if remaining < config.min_call_seconds:
                return _Finish(
                    AgentTurnOutcome.TIME_LIMIT,
                    error=f"turn budget used up before LLM call {step}"
                    + (f" (last error: {error})" if error else ""),
                )
            timeout = min(config.call_timeout_seconds, remaining)
            began = time.monotonic()
            try:
                async with asyncio.timeout(timeout):
                    result = await self._provider.complete(
                        model=config.model,
                        messages=state.messages,
                        tools=prepared.tools,
                        tool_choice=tool_choice,
                    )
            except TimeoutError:
                error = LLMError(
                    f"no response within {timeout:.1f}s",
                    retryable=True,
                    latency_ms=_ms_since(began),
                )
            except LLMError as exc:
                error = exc
            else:
                return result, await self._record_llm_call(prepared, step, attempt, result=result)
            await self._record_llm_call(prepared, step, attempt, error=error)
            if not error.retryable:
                break
            if attempt == 1:
                pause = min(
                    config.retry_backoff_seconds,
                    deadline - time.monotonic() - config.min_call_seconds,
                )
                if pause > 0:
                    await asyncio.sleep(pause)
        return _Finish(AgentTurnOutcome.LLM_ERROR, error=str(error))

    async def _run_tools(
        self, prepared: _Prepared, state: _State, result: LLMResult, llm_call_id: uuid.UUID
    ) -> _Finish | None:
        executor = ToolExecutor(self._sessionmaker, prepared.scope, self._clock)
        parsed = [parse_call(call) for call in result.tool_calls]
        # A handoff escalation runs first and everything else in the response is skipped.
        order = sorted(range(len(parsed)), key=lambda i: not parsed[i].is_handoff_escalation)
        outcomes: dict[int, tuple[ToolOutcome, int]] = {}
        handoff: ToolOutcome | None = None
        for index in order:
            if handoff is not None:
                outcomes[index] = (SKIPPED, 0)
                continue
            began = time.monotonic()
            outcome = await executor.execute(parsed[index])
            outcomes[index] = (outcome, _ms_since(began))
            if outcome.handoff:
                handoff = outcome

        records = []
        for index, call in enumerate(parsed):
            outcome, latency = outcomes[index]
            state.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call.id,
                    "content": json.dumps(outcome.result, ensure_ascii=False),
                }
            )
            state.alerts.extend(outcome.alerts)
            records.append(
                ToolCallRecord(
                    turn_id=prepared.turn_id,
                    llm_call_id=llm_call_id,
                    call_id=call.call.id,
                    name=call.call.name,
                    arguments=call.arguments,
                    result=outcome.result,
                    status=outcome.status,
                    latency_ms=latency,
                )
            )
        async with self._sessionmaker() as session, session.begin():
            session.add_all(records)

        if handoff is None:
            return None
        if handoff.customer_message:
            # Stored as the turn's last word, so a later turn knows what the customer was told.
            state.append({"role": "assistant", "content": handoff.customer_message})
        return _Finish(
            AgentTurnOutcome.HANDED_OFF, reply=handoff.customer_message, author=MessageAuthor.SYSTEM
        )

    # ----------------------------------------------------------------------------------------
    # records

    async def _record_llm_call(
        self,
        prepared: _Prepared,
        step: int,
        attempt: int,
        *,
        result: LLMResult | None = None,
        error: LLMError | None = None,
    ) -> uuid.UUID:
        row = LLMCall(
            turn_id=prepared.turn_id,
            step=step,
            attempt=attempt,
            model=self.config.model,
            prompt_version=prepared.prompt_version,
        )
        if result is not None:
            usage = result.usage
            row.input_tokens = usage.input_tokens
            row.cache_hit_tokens = usage.cache_hit_tokens
            row.output_tokens = usage.output_tokens
            row.reasoning_tokens = usage.reasoning_tokens
            row.cost_usd = result.cost_usd
            row.latency_ms = result.latency_ms
            row.finish_reason = result.finish_reason
            row.content = result.content
            row.tool_calls = [c.as_message_part() for c in result.tool_calls] or None
        else:
            assert error is not None
            row.latency_ms = error.latency_ms
            row.error = str(error)[:2000]
        async with self._sessionmaker() as session, session.begin():
            session.add(row)
            await session.flush()
            return row.id

    async def _save_messages(self, prepared: _Prepared, state: _State) -> None:
        """Persist the turn's transcript slice at a consistent point (every tool call answered),
        so a turn that fails later still leaves a replayable record of what it did."""
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                update(AgentTurn)
                .where(AgentTurn.id == prepared.turn_id)
                .values(messages=replayable(state.appended))
            )

    async def _finish(
        self, prepared: _Prepared, state: _State, finish: _Finish, started: float
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                update(AgentTurn)
                .where(AgentTurn.id == prepared.turn_id)
                .values(
                    outcome=finish.outcome,
                    reply=finish.reply,
                    error=finish.error[:2000] if finish.error else None,
                    messages=replayable(state.appended),
                    finished_at=self._clock.now(),
                    latency_ms=_ms_since(started),
                )
            )
