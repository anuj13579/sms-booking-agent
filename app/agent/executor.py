"""Runs the model's tool calls against the domain services.

* **Session-scoped** (D-010). The executor is built for one conversation and injects the
  business, customer, and conversation itself. Tool arguments can't name anyone else.
* **One transaction per call** (D-024). Each call commits on its own, so a booking that succeeded
  stays booked even if a later step of the turn fails.
* **Errors are data.** Invalid arguments and domain failures come back as structured results the
  model can recover from (``slot_unavailable`` carries alternative windows). Only a real crash is
  logged as an exception, and even then the model gets a result, not a stack trace.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.providers.base import ToolCall
from app.agent.tools import (
    TOOLS,
    CancelBookingArgs,
    CreateBookingArgs,
    EscalateArgs,
    GetAvailabilityArgs,
    RescheduleBookingArgs,
)
from app.db.models import Business, Conversation, Customer, ServiceType
from app.domain.availability import Slot, find_available_slots, get_service, load_calendar
from app.domain.booking import (
    BookingErr,
    BookingResult,
    NewBooking,
    cancel_booking,
    create_booking,
    reschedule_booking,
)
from app.domain.clock import Clock
from app.domain.enums import (
    EscalationMode,
    EscalationReason,
    EscalationSource,
    Hazard,
    ToolCallStatus,
)
from app.domain.escalation import ESCALATION_POLICY, escalate
from app.domain.localtime import day_label, local_to_utc, zone
from app.domain.notifications import AlertKind, OwnerAlert

logger = logging.getLogger(__name__)

OTHER_WINDOWS_LIMIT = 3


@dataclass(frozen=True)
class Scope:
    """Who this conversation belongs to. Comes from the server, never from the model."""

    business_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID


@dataclass(frozen=True)
class ParsedCall:
    call: ToolCall
    arguments: dict[str, Any]  # what the model sent, for the log
    args: Any = None  # the validated Pydantic model, when valid
    error: dict[str, Any] | None = None  # the result to return, when invalid

    @property
    def is_handoff_escalation(self) -> bool:
        """An escalation that takes the bot out of the conversation. It runs before any other
        call in the same response, and the others are skipped: nothing gets booked through an
        emergency (D-003)."""
        return (
            isinstance(self.args, EscalateArgs)
            and ESCALATION_POLICY[self.args.reason.domain()] is EscalationMode.HANDOFF
        )


@dataclass
class ToolOutcome:
    status: ToolCallStatus
    result: dict[str, Any]
    alerts: list[OwnerAlert] = field(default_factory=list)
    handoff: bool = False  # the owner owns the conversation now; the turn ends
    customer_message: str | None = None  # the fixed text to send when handing off


SKIPPED = ToolOutcome(
    ToolCallStatus.SKIPPED,
    {"error": "not_executed", "message": "Skipped: the conversation was handed to the owner."},
)


def parse_call(call: ToolCall) -> ParsedCall:
    try:
        arguments = json.loads(call.arguments) if call.arguments.strip() else {}
    except json.JSONDecodeError:
        return ParsedCall(
            call,
            {"_raw": call.arguments},
            error={"error": "invalid_arguments", "message": "Arguments were not valid JSON."},
        )
    if not isinstance(arguments, dict):
        return ParsedCall(
            call,
            {"_raw": call.arguments},
            error={"error": "invalid_arguments", "message": "Arguments must be a JSON object."},
        )
    spec = TOOLS.get(call.name)
    if spec is None:
        return ParsedCall(
            call,
            arguments,
            error={"error": "unknown_tool", "message": f"There is no tool named {call.name!r}."},
        )
    try:
        return ParsedCall(call, arguments, args=spec[0].model_validate(arguments))
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
            for e in exc.errors(include_url=False)
        ]
        return ParsedCall(
            call,
            arguments,
            error={"error": "invalid_arguments", "message": "; ".join(problems)},
        )


def _slot(slot: Slot) -> dict[str, str]:
    return {"slot_id": slot.slot_id, "label": slot.label}


def parse_slot_start(value: str, tz: ZoneInfo) -> datetime | None:
    """A slot_id as ``get_availability`` returns it (local time with offset). A value without an
    offset is read as the business's local wall time, which is what the model meant."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return local_to_utc(parsed.date(), parsed.time(), tz)
    return parsed.astimezone(UTC)


_INVALID_SLOT = {
    "error": "invalid_slot",
    "message": "slot_id must be copied exactly from a get_availability result, "
    "e.g. 2026-10-05T10:00-04:00.",
}

_OUTCOME_STATUS = {
    "created": "booked",
    "existing": "already_booked",
    "updated": "already_booked_details_updated",
    "rescheduled": "rescheduled",
    "unchanged": "unchanged",
    "cancelled": "cancelled",
    "already_cancelled": "already_cancelled",
}


def booking_outcome(result: BookingResult) -> ToolOutcome:
    if isinstance(result, BookingErr):
        payload: dict[str, Any] = {"error": result.code.value, "message": result.message}
        if result.alternatives:
            payload["alternatives"] = [_slot(s) for s in result.alternatives]
        return ToolOutcome(ToolCallStatus.ERROR, payload)
    booking = result.booking
    return ToolOutcome(
        ToolCallStatus.OK,
        {
            "status": _OUTCOME_STATUS[result.outcome],
            "ref": booking.ref,
            "service": booking.service_label,
            "window": booking.slot.label,
            "address": booking.address,
        },
    )


def _alert_label(reason: EscalationReason, hazard: Hazard | None) -> str:
    label = reason.value.replace("_", " ").upper()
    return f"{label} ({hazard.value})" if hazard else label


@dataclass
class _Ctx:
    session: AsyncSession
    business: Business
    customer: Customer
    conversation: Conversation
    now: datetime


class ToolExecutor:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], scope: Scope, clock: Clock
    ) -> None:
        self._sessionmaker = sessionmaker
        self._scope = scope
        self._clock = clock

    async def execute(self, parsed: ParsedCall) -> ToolOutcome:
        if parsed.error is not None:
            return ToolOutcome(ToolCallStatus.INVALID, parsed.error)
        handler = getattr(self, f"_{parsed.call.name}")
        try:
            async with self._sessionmaker() as session, session.begin():
                ctx = await self._load(session)
                outcome: ToolOutcome = await handler(ctx, parsed.args)
                return outcome
        except Exception:
            logger.exception("tool %s crashed", parsed.call.name)
            return ToolOutcome(
                ToolCallStatus.EXCEPTION,
                {
                    "error": "internal_error",
                    "message": "Something went wrong on our side. Don't retry; if you can't "
                    "continue, escalate with reason uncertain.",
                },
            )

    async def _load(self, session: AsyncSession) -> _Ctx:
        business = await session.get(Business, self._scope.business_id)
        customer = await session.get(Customer, self._scope.customer_id, populate_existing=True)
        conversation = await session.get(
            Conversation, self._scope.conversation_id, populate_existing=True
        )
        assert business is not None and customer is not None and conversation is not None
        return _Ctx(session, business, customer, conversation, self._clock.now())

    # ----------------------------------------------------------------------------------------

    async def _get_availability(self, ctx: _Ctx, args: GetAvailabilityArgs) -> ToolOutcome:
        session, business, now = ctx.session, ctx.business, ctx.now
        service = await get_service(session, business.id, args.service_type)
        if service is None:
            codes = await session.scalars(
                select(ServiceType.code).where(
                    ServiceType.business_id == business.id, ServiceType.active.is_(True)
                )
            )
            return ToolOutcome(
                ToolCallStatus.ERROR,
                {
                    "error": "unknown_service",
                    "message": f"No service {args.service_type!r}.",
                    "valid_service_types": sorted(codes),
                },
            )
        if not service.bookable:
            return ToolOutcome(
                ToolCallStatus.ERROR,
                {
                    "error": "service_not_bookable",
                    "message": f"{service.label} needs an on-site estimate first, so it can't "
                    "be booked here. Escalate with reason price_quote and tell the customer the "
                    "owner will be in touch to set up an estimate.",
                },
            )
        cal = await load_calendar(session, business)
        first, last = cal.bookable_days(now)
        earliest = max(args.earliest_date or first, first)
        latest = min(args.latest_date or last, last)
        if args.earliest_date and args.latest_date and args.latest_date < args.earliest_date:
            return ToolOutcome(
                ToolCallStatus.ERROR,
                {"error": "invalid_dates", "message": "latest_date is before earliest_date."},
            )

        async def search(**kwargs: Any) -> list[Slot]:
            return await find_available_slots(session, cal, service, now, **kwargs)

        slots = (
            await search(earliest=earliest, latest=latest, part_of_day=args.part_of_day)
            if earliest <= latest
            else []
        )
        result: dict[str, Any] = {"service": service.label}
        if earliest <= latest:
            result["searched"] = f"{day_label(earliest)} to {day_label(latest)}"
        result["windows"] = [_slot(s) for s in slots]
        if slots:
            return ToolOutcome(ToolCallStatus.OK, result)

        # Nothing matched: say why, and offer the nearest alternatives in one call.
        others: list[Slot] = []
        if args.part_of_day and earliest <= latest:
            others = await search(earliest=earliest, latest=latest, limit=OTHER_WINDOWS_LIMIT)
        if not others and latest < last:
            others = await search(earliest=latest + timedelta(days=1), limit=OTHER_WINDOWS_LIMIT)
        if not others:
            others = await search(limit=OTHER_WINDOWS_LIMIT)
        if others:
            result["message"] = "No open windows matched that request. Nearest other openings:"
            result["other_windows"] = [_slot(s) for s in others]
        else:
            result["message"] = (
                f"Nothing is open in the next {cal.horizon_days} days. If the job is urgent, "
                "escalate with reason urgent_no_availability."
            )
        if args.earliest_date and args.earliest_date > last:
            result["note"] = f"The business books only {cal.horizon_days} days ahead."
        return ToolOutcome(ToolCallStatus.OK, result)

    async def _create_booking(self, ctx: _Ctx, args: CreateBookingArgs) -> ToolOutcome:
        start = parse_slot_start(args.slot_id, zone(ctx.business.timezone))
        if start is None:
            return ToolOutcome(ToolCallStatus.ERROR, dict(_INVALID_SLOT))
        result = await create_booking(
            ctx.session,
            ctx.business,
            ctx.customer,
            NewBooking(
                service_code=args.service_type,
                slot_start=start,
                customer_name=args.customer_name,
                address=args.address,
                zip=args.zip,
                problem_description=args.problem_description,
                urgency=args.urgency,
            ),
            self._clock,
        )
        return booking_outcome(result)

    async def _reschedule_booking(self, ctx: _Ctx, args: RescheduleBookingArgs) -> ToolOutcome:
        start = parse_slot_start(args.new_slot_id, zone(ctx.business.timezone))
        if start is None:
            return ToolOutcome(ToolCallStatus.ERROR, dict(_INVALID_SLOT))
        result = await reschedule_booking(
            ctx.session, ctx.business, ctx.customer, args.booking_ref, start, self._clock
        )
        return booking_outcome(result)

    async def _cancel_booking(self, ctx: _Ctx, args: CancelBookingArgs) -> ToolOutcome:
        result = await cancel_booking(
            ctx.session,
            ctx.business,
            ctx.customer,
            args.booking_ref,
            self._clock,
            reason=args.reason,
        )
        return booking_outcome(result)

    async def _escalate_to_owner(self, ctx: _Ctx, args: EscalateArgs) -> ToolOutcome:
        reason = args.reason.domain()
        hazard = (args.hazard or Hazard.OTHER) if reason is EscalationReason.EMERGENCY else None
        result = await escalate(
            ctx.session,
            ctx.business,
            ctx.conversation,
            reason=reason,
            hazard=hazard,
            summary=args.summary,
            source=EscalationSource.AGENT,
            now=ctx.now,
        )
        alerts = []
        if result.created:
            alerts.append(
                OwnerAlert(
                    AlertKind.ESCALATION,
                    ctx.business.id,
                    ctx.conversation.id,
                    f"{_alert_label(reason, hazard)}: {args.summary}",
                    result.escalation.id,
                )
            )
        owner = ctx.business.owner_name
        if result.mode is EscalationMode.HANDOFF:
            return ToolOutcome(
                ToolCallStatus.OK,
                {
                    "status": "handed_off",
                    "message": f"{owner} now owns this conversation and the customer has been "
                    "told. Don't reply.",
                },
                alerts,
                handoff=True,
                customer_message=result.customer_message,
            )
        return ToolOutcome(
            ToolCallStatus.OK,
            {
                "status": "owner_notified" if result.created else "already_notified",
                "message": f"{owner} has been notified. Carry on helping the customer; don't "
                "promise a price or a time for the follow-up.",
            },
            alerts,
        )
