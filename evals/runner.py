"""Run one scenario through the production pipeline and record what happened.

Each run gets its own copy of the seed business (so runs can go concurrently and never see each
other's rows), a frozen clock pinned to the scenario, and a fresh Agent. Customer messages go
through ``handle_inbound`` with ``Channel.EVAL`` exactly as SMS would (D-013). After the
conversation, the trace is read back from the database: the same tables production writes.
"""

import itertools
import logging
import time
import traceback
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.loop import Agent, AgentConfig
from app.agent.providers.base import LLMProvider
from app.db.models import (
    AgentTurn,
    Booking,
    Conversation,
    Customer,
    Escalation,
    LLMCall,
    Message,
    ServiceType,
)
from app.db.models import ToolCallRecord as ToolCallRow
from app.domain.clock import FixedClock
from app.domain.enums import Channel
from app.domain.notifications import RecordingNotifier
from app.pipeline.inbound import handle_inbound
from evals import fixtures
from evals.customers import make_customer
from evals.results import (
    BookingRecord,
    EscalationRecord,
    Exchange,
    MessageRecord,
    ScenarioRun,
    ToolTrace,
    TurnRecord,
)
from evals.scenario import TZ, Scenario

logger = logging.getLogger(__name__)


def phone_numbers(prefix: str = "+1555") -> Iterator[str]:
    """Unique business numbers for the copies of the seed business, one per scenario run."""
    for n in itertools.count(1):
        yield f"{prefix}{n:07d}"


@dataclass
class EvalEnv:
    sessionmaker: async_sessionmaker[AsyncSession]
    agent_provider: LLMProvider
    agent_config: AgentConfig
    sim_provider: LLMProvider | None = None
    sim_model: str = "deepseek-flash"
    numbers: Iterator[str] = field(default_factory=phone_numbers)


async def run_scenario(env: EvalEnv, scenario: Scenario, repeat: int = 0) -> ScenarioRun:
    run = ScenarioRun(
        scenario_id=scenario.id,
        category=scenario.category,
        split=scenario.split,
        title=scenario.title,
        tags=list(scenario.tags),
        repeat=repeat,
        customer_kind=scenario.customer.kind,
    )
    began = time.monotonic()
    business_id: uuid.UUID | None = None
    try:
        clock = FixedClock(scenario.clock)
        async with env.sessionmaker() as session, session.begin():
            built = await fixtures.build(session, scenario.fixture, clock, next(env.numbers))
        business_id = built.business.id
        await _converse(env, scenario, run, clock, business_id)
        await _collect(env.sessionmaker, run, built)
    except Exception as exc:
        logger.exception("scenario %s failed in the harness", scenario.id)
        run.harness_error = f"{exc!r}\n{traceback.format_exc(limit=5)}"
    run.wall_seconds = round(time.monotonic() - began, 2)
    return run


async def _converse(
    env: EvalEnv, scenario: Scenario, run: ScenarioRun, clock: FixedClock, business_id: uuid.UUID
) -> None:
    agent = Agent(env.sessionmaker, env.agent_provider, clock, env.agent_config)
    customer = make_customer(
        scenario.customer,
        provider=env.sim_provider,
        model=env.sim_model,
        now_local=scenario.local_clock,
    )
    notifier = RecordingNotifier()
    message: str | None = customer.opening()
    while message is not None:
        at = clock.now().astimezone(TZ)
        outcome = await handle_inbound(
            env.sessionmaker,
            business_id,
            channel=Channel.EVAL,
            from_number=fixtures.CUSTOMER_PHONE,
            body=message,
            agent=agent,
            notifier=notifier,
            clock=clock,
        )
        if outcome is None:
            raise RuntimeError("handle_inbound reported a duplicate for a fresh message")
        async with env.sessionmaker() as session:
            mode = await session.scalar(
                select(Conversation.mode).where(Conversation.id == outcome.conversation_id)
            )
        run.exchanges.append(
            Exchange(
                customer=message,
                replies=outcome.all_replies,
                handled_by=outcome.handled_by,
                at=at,
                mode_after=mode or "",
            )
        )
        clock.advance(timedelta(seconds=scenario.reply_delay_seconds))
        message = await customer.next_message(run.exchanges)
    run.end_reason = customer.end_reason
    run.simulator_broke = customer.broke
    run.infra_error = customer.infra
    run.sim_calls = customer.calls
    run.sim_cost_usd = customer.cost_usd


async def _collect(
    sessionmaker: async_sessionmaker[AsyncSession], run: ScenarioRun, built: fixtures.Built
) -> None:
    business_id = built.business.id
    label_of = {booking_id: label for label, booking_id in built.labels.items()}
    async with sessionmaker() as session:
        customer = await session.scalar(
            select(Customer).where(
                Customer.business_id == business_id,
                Customer.phone_e164 == fixtures.CUSTOMER_PHONE,
            )
        )
        if customer is None:
            return
        run.opted_out = customer.opted_out_at is not None
        conversations = select(Conversation.id).where(Conversation.customer_id == customer.id)

        for m in await session.scalars(
            select(Message).where(Message.conversation_id.in_(conversations)).order_by(Message.seq)
        ):
            run.messages.append(
                MessageRecord(direction=m.direction, author=m.author, body=m.body, status=m.status)
            )

        rows = await session.execute(
            select(Booking, ServiceType)
            .join(ServiceType, ServiceType.id == Booking.service_type_id)
            .where(Booking.customer_id == customer.id)
            .order_by(Booking.created_at, Booking.ref)
        )
        for booking, service in rows:
            original = built.original_starts.get(booking.id)
            assert booking.time_window.lower is not None and booking.time_window.upper is not None
            run.bookings.append(
                BookingRecord(
                    ref=booking.ref,
                    label=label_of.get(booking.id),
                    service=service.code,
                    trade=service.trade,
                    start=booking.time_window.lower.astimezone(TZ),
                    end=booking.time_window.upper.astimezone(TZ),
                    status=booking.status,
                    urgency=booking.urgency,
                    address=booking.address,
                    zip=booking.zip,
                    original_start=original.astimezone(TZ) if original else None,
                )
            )

        for e in await session.scalars(
            select(Escalation)
            .where(Escalation.conversation_id.in_(conversations))
            .order_by(Escalation.created_at, Escalation.id)
        ):
            run.escalations.append(
                EscalationRecord(
                    reason=e.reason,
                    hazard=e.hazard,
                    source=e.source,
                    mode=e.mode,
                    status=e.status,
                    summary=e.summary,
                )
            )

        turns = list(
            await session.scalars(
                select(AgentTurn)
                .where(AgentTurn.conversation_id.in_(conversations))
                .order_by(AgentTurn.seq)
            )
        )
        index = {turn.id: i for i, turn in enumerate(turns)}
        usage = {
            row.turn_id: row
            for row in await session.execute(
                select(
                    LLMCall.turn_id,
                    func.count(LLMCall.id).label("calls"),
                    func.coalesce(func.sum(LLMCall.cost_usd), 0).label("cost"),
                    func.coalesce(func.sum(LLMCall.input_tokens), 0).label("input"),
                    func.coalesce(func.sum(LLMCall.cache_hit_tokens), 0).label("hit"),
                    func.coalesce(func.sum(LLMCall.output_tokens), 0).label("output"),
                )
                .where(LLMCall.turn_id.in_(index))
                .group_by(LLMCall.turn_id)
            )
        }
        for turn in turns:
            u = usage.get(turn.id)
            run.turns.append(
                TurnRecord(
                    outcome=turn.outcome,
                    latency_ms=turn.latency_ms,
                    llm_calls=u.calls if u else 0,
                    cost_usd=Decimal(u.cost) if u else Decimal(0),
                    input_tokens=u.input if u else 0,
                    cache_hit_tokens=u.hit if u else 0,
                    output_tokens=u.output if u else 0,
                )
            )

        for call in await session.scalars(
            select(ToolCallRow)
            .where(ToolCallRow.turn_id.in_(index))
            .order_by(ToolCallRow.created_at, ToolCallRow.id)
        ):
            error = call.result.get("error") if isinstance(call.result, dict) else None
            run.tool_calls.append(
                ToolTrace(
                    turn=index[call.turn_id],
                    name=call.name,
                    arguments=call.arguments,
                    status=call.status,
                    error=str(error) if error else None,
                )
            )
        run.tool_calls.sort(key=lambda c: c.turn)

        run.double_bookings = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM bookings a JOIN bookings b "
                    "ON a.technician_id = b.technician_id AND a.id < b.id "
                    "AND a.time_window && b.time_window "
                    "WHERE a.status = 'confirmed' AND b.status = 'confirmed' "
                    "AND a.business_id = :business"
                ),
                {"business": business_id},
            )
            or 0
        )
