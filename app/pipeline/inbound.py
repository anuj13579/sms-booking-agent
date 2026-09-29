"""The inbound pipeline: one code path for SMS, the web-chat simulator, and evals (D-013).

Two stages, so the SMS webhook can answer Twilio in milliseconds (D-012):

1. ``receive_inbound``: dedupe, find or create the customer and conversation, store the message
   as ``received``. The webhook calls only this, then returns.
2. ``process_conversation``: claim the conversation's lease, then handle every pending message
   in one batch. Deterministic steps run first, and each can finish the turn without an LLM:

   a. **Keywords** (STOP / START / HELP, D-020).
   b. **Emergency pre-filter** (D-003). It runs *before* the handoff check: a customer whose
      chat is already with the owner still gets the safety text the moment they mention gas.
      It also runs for opted-out customers; they get no text, but the owner is paged.
   c. **Opted out**: nothing goes to the agent, nothing is sent; the owner hears about it so the
      lead isn't lost.
   d. **Handoff**: a human owns the chat; store, alert the owner, stay silent.
   e. **Agent**: everything else. The agent itself arrives in Phase 2; here it is a Protocol.

``handle_inbound`` runs both stages back to back, for channels that want the reply inline.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Business, Conversation, Customer, Message
from app.domain import templates
from app.domain.clock import Clock
from app.domain.conversations import (
    add_inbound,
    add_outbound,
    is_duplicate,
    mark_inbound,
    open_conversation,
    pending_inbound,
)
from app.domain.customers import get_or_create_customer, set_opted_in, set_opted_out
from app.domain.enums import (
    Channel,
    ConversationMode,
    EscalationReason,
    EscalationSource,
    MessageAuthor,
    MessageStatus,
)
from app.domain.escalation import escalate
from app.domain.notifications import AlertKind, OwnerAlert, OwnerNotifier
from app.pipeline import queue
from app.pipeline.optout import Keyword, classify_keyword
from app.pipeline.safety import detect_hazards

logger = logging.getLogger(__name__)

DEFAULT_LEASE = timedelta(seconds=120)
# While holding the lease, keep taking turns for messages that arrived mid-turn, up to this many
# extra turns. Beyond that, release and let the queue poller pick the conversation up again.
MAX_FOLLOW_UP_TURNS = 3


# --------------------------------------------------------------------------------------------
# the agent port (implemented in Phase 2)


@dataclass(frozen=True)
class AgentTurnInput:
    business_id: uuid.UUID
    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    message_ids: tuple[uuid.UUID, ...]
    texts: tuple[str, ...]


@dataclass(frozen=True)
class AgentTurnOutput:
    reply: str | None
    author: MessageAuthor = MessageAuthor.AGENT


class TurnAgent(Protocol):
    """Runs one agent turn. Opens its own DB sessions: each tool call commits on its own, so a
    booking that succeeded stays booked even if a later LLM call in the same turn fails."""

    async def run_turn(self, turn: AgentTurnInput) -> AgentTurnOutput: ...


# --------------------------------------------------------------------------------------------
# stage 1


@dataclass(frozen=True)
class Received:
    duplicate: bool
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None


async def receive_inbound(
    session: AsyncSession,
    business: Business,
    *,
    channel: Channel,
    from_number: str,
    body: str,
    clock: Clock,
    provider_sid: str | None = None,
) -> Received:
    """Store one inbound message. The caller commits. Webhook retries are no-ops."""
    if await is_duplicate(session, provider_sid):
        return Received(duplicate=True)
    now = clock.now()
    customer = await get_or_create_customer(session, business.id, from_number)
    conversation = await open_conversation(session, business.id, customer.id, channel, now)
    message = await add_inbound(session, conversation, body, now, provider_sid)
    if message is None:  # lost a race with a concurrent retry of the same webhook
        return Received(duplicate=True)
    return Received(duplicate=False, conversation_id=conversation.id, message_id=message.id)


# --------------------------------------------------------------------------------------------
# stage 2


class HandledBy(StrEnum):
    NOTHING = "nothing"  # no pending messages
    BUSY = "busy"  # another worker holds the lease
    KEYWORDS = "keywords"
    EMERGENCY = "emergency"
    OPTED_OUT = "opted_out"
    HANDOFF = "handoff"
    AGENT = "agent"
    AGENT_FAILED = "agent_failed"


@dataclass
class TurnOutcome:
    conversation_id: uuid.UUID
    handled_by: HandledBy
    replies: list[str] = field(default_factory=list)
    alerts: list[OwnerAlert] = field(default_factory=list)
    # Turns run straight after this one for messages that arrived while it was in progress.
    follow_ups: list["TurnOutcome"] = field(default_factory=list)

    @property
    def all_replies(self) -> list[str]:
        return [*self.replies, *(r for turn in self.follow_ups for r in turn.all_replies)]


@dataclass
class _Turn:
    """Mutable scratchpad for one pass over a conversation's pending messages."""

    session: AsyncSession
    business: Business
    customer: Customer
    conversation: Conversation
    clock: Clock
    outcome: TurnOutcome

    async def reply(self, text: str) -> None:
        await add_outbound(
            self.session, self.conversation, text, MessageAuthor.SYSTEM, self.clock.now()
        )
        self.outcome.replies.append(text)

    def alert(self, kind: AlertKind, text: str, escalation_id: uuid.UUID | None = None) -> None:
        self.outcome.alerts.append(
            OwnerAlert(kind, self.business.id, self.conversation.id, text, escalation_id)
        )

    async def finish(self, messages: list[Message], status: MessageStatus, by: HandledBy) -> None:
        await mark_inbound(self.session, [m.id for m in messages], status, self.clock.now())
        self.outcome.handled_by = by


async def process_conversation(
    sessionmaker: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    *,
    agent: TurnAgent,
    notifier: OwnerNotifier,
    clock: Clock,
    lease: timedelta = DEFAULT_LEASE,
) -> TurnOutcome:
    async with sessionmaker() as session, session.begin():
        token = await queue.claim(session, conversation_id, clock.now(), lease)
    if token is None:
        # Another worker has it. It will see this message before releasing (follow-up turns),
        # or the poller will.
        return TurnOutcome(conversation_id, HandledBy.BUSY)
    try:
        outcome = await _process_claimed(sessionmaker, conversation_id, agent, notifier, clock)
        for _ in range(MAX_FOLLOW_UP_TURNS):
            async with sessionmaker() as session:
                if not await pending_inbound(session, conversation_id):
                    break
            outcome.follow_ups.append(
                await _process_claimed(sessionmaker, conversation_id, agent, notifier, clock)
            )
        return outcome
    finally:
        async with sessionmaker() as session, session.begin():
            await queue.release(session, conversation_id, token)


async def _process_claimed(
    sessionmaker: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    agent: TurnAgent,
    notifier: OwnerNotifier,
    clock: Clock,
) -> TurnOutcome:
    outcome = TurnOutcome(conversation_id, HandledBy.NOTHING)
    agent_input: AgentTurnInput | None = None

    async with sessionmaker() as session, session.begin():
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        business = await session.get(Business, conversation.business_id)
        customer = await session.get(Customer, conversation.customer_id)
        assert business is not None and customer is not None
        pending = await pending_inbound(session, conversation_id)
        if pending:
            turn = _Turn(session, business, customer, conversation, clock, outcome)
            agent_input = await _deterministic_steps(turn, pending)

    await _dispatch(notifier, outcome.alerts)
    if agent_input is None:
        return outcome

    try:
        result = await agent.run_turn(agent_input)
    except Exception:
        logger.exception("agent turn failed for conversation %s", conversation_id)
        return await _agent_failed(sessionmaker, agent_input, notifier, clock, outcome)

    async with sessionmaker() as session, session.begin():
        conversation = await session.get(Conversation, conversation_id)
        customer = await session.get(Customer, agent_input.customer_id, populate_existing=True)
        assert conversation is not None and customer is not None
        # The customer may have texted STOP while the agent was thinking.
        if result.reply and customer.opted_out_at is None:
            await add_outbound(session, conversation, result.reply, result.author, clock.now())
            outcome.replies.append(result.reply)
        await mark_inbound(session, agent_input.message_ids, MessageStatus.PROCESSED, clock.now())
    outcome.handled_by = HandledBy.AGENT
    return outcome


async def _deterministic_steps(turn: _Turn, pending: list[Message]) -> AgentTurnInput | None:
    """Steps a-d from the module docstring. Returns the agent's input if step e is needed."""
    business, customer, conversation = turn.business, turn.customer, turn.conversation
    now = turn.clock.now()

    # a. keywords, in arrival order ("STOP" then "START" leaves them subscribed)
    keyword_messages, rest = [], []
    for message in pending:
        keyword = classify_keyword(message.body, opted_out=customer.opted_out_at is not None)
        if keyword is None:
            rest.append(message)
            continue
        keyword_messages.append(message)
        if keyword is Keyword.OPT_OUT:
            if await set_opted_out(turn.session, customer, now):
                await turn.reply(templates.OPT_OUT_CONFIRMATION.format(business=business.name))
        elif keyword is Keyword.OPT_IN:
            await set_opted_in(turn.session, customer)
            await turn.reply(templates.OPT_IN_CONFIRMATION.format(business=business.name))
        else:
            await turn.reply(
                templates.HELP_MESSAGE.format(
                    business=business.name,
                    owner_phone=templates.display_phone(business.owner_phone),
                )
            )
    if keyword_messages:
        await turn.finish(keyword_messages, MessageStatus.PROCESSED, HandledBy.KEYWORDS)
    if not rest:
        return None

    subscribed = customer.opted_out_at is None
    text = "\n".join(m.body for m in rest)

    # b. emergency pre-filter
    hazards = detect_hazards(text)
    if hazards:
        result = await escalate(
            turn.session,
            business,
            conversation,
            reason=EscalationReason.EMERGENCY,
            hazard=hazards[0],
            summary=f"Pre-filter matched {', '.join(hazards)}. Customer wrote: {text}",
            source=EscalationSource.PREFILTER,
            now=now,
        )
        if result.created:
            turn.alert(
                AlertKind.ESCALATION, f"EMERGENCY ({hazards[0]}): {text}", result.escalation.id
            )
        else:
            turn.alert(AlertKind.MESSAGE_WHILE_HANDED_OFF, text, result.escalation.id)
        if result.customer_message and subscribed:
            await turn.reply(result.customer_message)
        await turn.finish(rest, MessageStatus.PROCESSED, HandledBy.EMERGENCY)
        return None

    # c. opted out
    if not subscribed:
        turn.alert(AlertKind.MESSAGE_FROM_OPTED_OUT, text)
        await turn.finish(rest, MessageStatus.SKIPPED, HandledBy.OPTED_OUT)
        return None

    # d. a human owns the conversation
    if conversation.mode == ConversationMode.HANDOFF:
        turn.alert(AlertKind.MESSAGE_WHILE_HANDED_OFF, text)
        await turn.finish(rest, MessageStatus.PROCESSED, HandledBy.HANDOFF)
        return None

    # e. the agent
    return AgentTurnInput(
        business_id=business.id,
        conversation_id=conversation.id,
        customer_id=customer.id,
        message_ids=tuple(m.id for m in rest),
        texts=tuple(m.body for m in rest),
    )


async def _agent_failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    agent_input: AgentTurnInput,
    notifier: OwnerNotifier,
    clock: Clock,
    outcome: TurnOutcome,
) -> TurnOutcome:
    """The customer is never left without a reply: fixed fallback text, and the owner takes over."""
    alerts: list[OwnerAlert] = []
    async with sessionmaker() as session, session.begin():
        conversation = await session.get(Conversation, agent_input.conversation_id)
        business = await session.get(Business, agent_input.business_id)
        customer = await session.get(Customer, agent_input.customer_id, populate_existing=True)
        assert conversation is not None and business is not None and customer is not None
        now = clock.now()
        result = await escalate(
            session,
            business,
            conversation,
            reason=EscalationReason.SYSTEM_ERROR,
            summary="The agent failed while handling: " + " / ".join(agent_input.texts),
            source=EscalationSource.SYSTEM,
            now=now,
        )
        if result.created:
            alerts.append(
                OwnerAlert(
                    AlertKind.ESCALATION,
                    business.id,
                    conversation.id,
                    result.escalation.summary,
                    result.escalation.id,
                )
            )
        if result.customer_message and customer.opted_out_at is None:
            await add_outbound(
                session, conversation, result.customer_message, MessageAuthor.SYSTEM, now
            )
            outcome.replies.append(result.customer_message)
        await mark_inbound(session, agent_input.message_ids, MessageStatus.PROCESSED, now)
    await _dispatch(notifier, alerts)
    outcome.alerts.extend(alerts)
    outcome.handled_by = HandledBy.AGENT_FAILED
    return outcome


async def _dispatch(notifier: OwnerNotifier, alerts: list[OwnerAlert]) -> None:
    """After commit, and best effort: a failed page must not fail the customer's turn."""
    for alert in alerts:
        try:
            await notifier.notify(alert)
        except Exception:
            logger.exception("owner notification failed: %s", alert)


# --------------------------------------------------------------------------------------------
# both stages


async def handle_inbound(
    sessionmaker: async_sessionmaker[AsyncSession],
    business_id: uuid.UUID,
    *,
    channel: Channel,
    from_number: str,
    body: str,
    agent: TurnAgent,
    notifier: OwnerNotifier,
    clock: Clock,
    provider_sid: str | None = None,
) -> TurnOutcome | None:
    """Receive and process immediately. None for a duplicate delivery."""
    async with sessionmaker() as session, session.begin():
        business = await session.get(Business, business_id)
        if business is None:
            raise LookupError(f"no business {business_id}")
        received = await receive_inbound(
            session,
            business,
            channel=channel,
            from_number=from_number,
            body=body,
            clock=clock,
            provider_sid=provider_sid,
        )
    if received.duplicate or received.conversation_id is None:
        return None
    return await process_conversation(
        sessionmaker, received.conversation_id, agent=agent, notifier=notifier, clock=clock
    )
