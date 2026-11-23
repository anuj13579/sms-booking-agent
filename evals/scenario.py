"""Eval scenarios: the YAML schema, loading, and validation (DESIGN section 9, D-018).

A scenario file is ``evals/scenarios/<category>.yaml``::

    category: normal_booking
    defaults:              # deep-merged into every scenario below (scenario values win)
      clock: 2026-10-06T09:15
    scenarios:
      - id: nb-001
        split: dev
        title: AC blowing warm air, wants tomorrow morning
        customer: {kind: simulated, opening: "...", persona: {...}}
        expect: {bookings: {new: 1, service: [ac_repair]}, escalate: []}

Times without an offset are Brightwater's local time (America/New_York), converted per date so
scenarios can sit on either side of a DST change.
"""

import copy
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import EscalationReason, Hazard, Trade, Urgency
from app.domain.localtime import local_to_utc
from app.seed import SATURDAY_WINDOWS, SERVICE_AREA_ZIPS, SERVICES, TECHNICIANS, WEEKDAY_WINDOWS

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
TZ = ZoneInfo("America/New_York")
# Mirrors the seed business (app/seed.py). Validation checks scenarios against these.
MIN_LEAD = timedelta(minutes=120)
HORIZON_DAYS = 14
SERVICE_TRADES = {code: Trade(trade) for code, _, trade, _ in SERVICES}
BOOKABLE = {code for code, _, _, bookable in SERVICES if bookable}
TECH_NAMES = {name for name, _ in TECHNICIANS}
DEV_SHARE = 0.7


class Category(StrEnum):
    NORMAL_BOOKING = "normal_booking"
    AMBIGUOUS = "ambiguous"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    ANGRY = "angry"
    CLEAR_EMERGENCY = "clear_emergency"
    SUBTLE_EMERGENCY = "subtle_emergency"
    EMERGENCY_LOOKALIKE = "emergency_lookalike"
    PRICE = "price"
    OFF_TOPIC = "off_topic"  # off-topic, out-of-scope, or prompt injection
    OPT_OUT = "opt_out"  # STOP, HELP, START


TARGET_COUNTS: dict[Category, int] = {
    Category.NORMAL_BOOKING: 50,
    Category.AMBIGUOUS: 35,
    Category.RESCHEDULE: 25,
    Category.CANCEL: 20,
    Category.ANGRY: 20,
    Category.CLEAR_EMERGENCY: 20,
    Category.SUBTLE_EMERGENCY: 15,
    Category.EMERGENCY_LOOKALIKE: 15,
    Category.PRICE: 20,
    Category.OFF_TOPIC: 20,
    Category.OPT_OUT: 10,
}
# Headline metrics (DESIGN section 1).
TASK_CATEGORIES = frozenset({Category.NORMAL_BOOKING, Category.RESCHEDULE, Category.CANCEL})
EMERGENCY_CATEGORIES = frozenset({Category.CLEAR_EMERGENCY, Category.SUBTLE_EMERGENCY})
# These need no adaptivity and must be deterministic, so they use scripted customers (D-018).
SCRIPTED_CATEGORIES = frozenset(
    {Category.CLEAR_EMERGENCY, Category.SUBTLE_EMERGENCY, Category.OPT_OUT}
)
SCRIPTED_TAGS = frozenset({"injection"})


def _local_datetime(value: Any) -> Any:
    """Naive values (YAML ``2026-10-06T09:15`` or a string) are Brightwater wall times."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        raise ValueError("expected a date and time, got a bare date")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            converted = local_to_utc(value.date(), value.time(), TZ)
            if converted is None:
                raise ValueError(f"{value} does not exist in {TZ.key} (DST gap)")
            return converted
        return value
    return value


def _clock_time(value: Any) -> Any:
    """YAML reads ``08:00`` as a string, and ``8:00`` as a base-60 integer (480)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return time(value // 60, value % 60)
    return value


LocalDateTime = Annotated[datetime, BeforeValidator(_local_datetime)]
ClockTime = Annotated[time, BeforeValidator(_clock_time)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


# --------------------------------------------------------------------------------------------
# fixture: the database state before the first message


class FixtureCustomer(_Model):
    """A returning customer: this is what's on file for the texting number."""

    name: str | None = None
    address: str | None = None
    zip: str | None = None
    opted_out: bool = False


class FixtureBooking(_Model):
    label: str  # how expectations refer to it, e.g. "b1"
    service: str
    start: LocalDateTime
    problem: str = "Routine visit"
    urgency: Urgency = Urgency.ROUTINE


class Busy(_Model):
    """Book every technician of a trade (or every trade) for one window or a whole day, using
    other customers, so the agent finds nothing there."""

    trade: Trade | None = None  # None: every trade
    start: LocalDateTime | None = None
    on: date | None = Field(default=None, alias="date")

    @model_validator(mode="after")
    def _one_of(self) -> "Busy":
        if (self.start is None) == (self.on is None):
            raise ValueError("busy needs exactly one of start (one window) or date (all day)")
        return self


class TimeOff(_Model):
    tech: str
    start: LocalDateTime
    end: LocalDateTime


class Fixture(_Model):
    customer: FixtureCustomer | None = None
    bookings: list[FixtureBooking] = []
    busy: list[Busy] = []
    time_off: list[TimeOff] = []


# --------------------------------------------------------------------------------------------
# the customer


class Persona(_Model):
    name: str
    situation: str  # what is going on, as the customer sees it
    goal: str  # what they want out of the conversation
    facts: dict[str, str] = {}  # shared when asked: address, zip, details of the problem
    availability: str | None = None  # when they can have someone come
    style: str = "brief, casual texting"
    notes: str | None = None  # anything else about how they behave


class ScriptedCustomer(_Model):
    """Exact messages, sent in order whatever the business replies."""

    kind: Literal["scripted"]
    messages: list[str] = Field(min_length=1)


class SimulatedCustomer(_Model):
    """An LLM plays the persona after a fixed opening message."""

    kind: Literal["simulated"]
    opening: str
    persona: Persona
    max_turns: int = Field(default=8, ge=1, le=15)


CustomerSpec = Annotated[ScriptedCustomer | SimulatedCustomer, Field(discriminator="kind")]


# --------------------------------------------------------------------------------------------
# expectations


class WindowSpec(_Model):
    """Acceptable arrival windows: those on ``date`` (through ``through``, if given) whose local
    start is between ``from`` and ``until``, inclusive."""

    on: date = Field(alias="date")
    through: date | None = None
    start_from: ClockTime = Field(default=time(0), alias="from")
    start_until: ClockTime = Field(default=time(23, 59), alias="until")

    def contains(self, local_start: datetime) -> bool:
        return (
            self.on <= local_start.date() <= (self.through or self.on)
            and self.start_from <= local_start.time() <= self.start_until
        )


class ExpectBookings(_Model):
    """About bookings created during the conversation for the texting customer."""

    new: int | None = None  # exact number confirmed at the end
    created: int | None = None  # exact number created at all, even if later cancelled
    service: list[str] = []  # allowed service codes
    windows: list[WindowSpec] = []  # every new booking starts in one of these
    address: str | None = None  # case-insensitive substring of the booked address
    zip: str | None = None
    urgency: Urgency | None = None


class ExpectExisting(_Model):
    state: Literal["unchanged", "cancelled", "rescheduled"]
    windows: list[WindowSpec] = []  # where a rescheduled booking may land


def _existing(value: Any) -> Any:
    return {"state": value} if isinstance(value, str) else value


class ExpectSafety(_Model):
    at: int = Field(default=0, ge=0)  # index of the customer message that discloses the hazard
    hazards: list[Hazard] = []  # the template sent must be for one of these; empty: any


class Expect(_Model):
    bookings: ExpectBookings | None = None
    existing: dict[str, Annotated[ExpectExisting, BeforeValidator(_existing)]] = {}
    # At least one escalation with one of these reasons. An empty list: no escalation at all.
    # Omitted: escalation isn't graded for this scenario.
    escalate: list[EscalationReason] | None = None
    forbid_escalate: list[EscalationReason] = []
    hazards: list[Hazard] = []  # the emergency escalation's hazard is one of these
    safety_reply: ExpectSafety | None = None
    no_safety_message: bool = False
    opted_out: bool | None = None
    reply_must_match: list[str] = []  # regexes; each must match some business message
    reply_must_not_match: list[str] = []  # regexes; none may match any business message

    @field_validator("reply_must_match", "reply_must_not_match")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            re.compile(pattern)
        return patterns


# --------------------------------------------------------------------------------------------
# the scenario


class Scenario(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    category: Category
    split: Literal["dev", "test"]
    title: str
    tags: list[str] = []
    clock: LocalDateTime
    # The clock moves this much between the customer's messages.
    reply_delay_seconds: int = Field(default=60, ge=0)
    fixture: Fixture = Fixture()
    customer: CustomerSpec
    expect: Expect
    notes: str | None = None

    @property
    def local_clock(self) -> datetime:
        return self.clock.astimezone(TZ)


class ScenarioError(ValueError):
    pass


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _merge(base[key], value) if key in base else value
        return merged
    return copy.deepcopy(override)


def load_file(path: Path) -> list[Scenario]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path.name}: not valid YAML: {exc}") from exc
    category = raw.get("category", path.stem)
    defaults = raw.get("defaults", {})
    scenarios = []
    for index, item in enumerate(raw.get("scenarios", [])):
        base = dict(defaults)
        # A scenario that switches the customer kind replaces the default customer outright,
        # so simulated-only fields (max_turns) don't leak into a scripted customer.
        kind = (item.get("customer") or {}).get("kind") if isinstance(item, dict) else None
        if kind and kind != (base.get("customer") or {}).get("kind"):
            base.pop("customer", None)
        data = _merge(base, item)
        data.setdefault("category", category)
        try:
            scenarios.append(Scenario.model_validate(data))
        except ValueError as exc:
            ident = item.get("id", f"#{index}") if isinstance(item, dict) else f"#{index}"
            raise ScenarioError(f"{path.name}: scenario {ident}: {exc}") from exc
    return scenarios


def load_all(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.yaml")):
        scenarios.extend(load_file(path))
    return scenarios


# --------------------------------------------------------------------------------------------
# validation against the seed business


def _windows_on(day: date) -> list[tuple[time, time]]:
    if day.weekday() < 5:
        return list(WEEKDAY_WINDOWS)
    if day.weekday() == 5:
        return list(SATURDAY_WINDOWS)
    return []


def bookable_starts(now: datetime) -> list[datetime]:
    """Every window start the seed business could book at ``now``, as aware local datetimes."""
    today = now.astimezone(TZ).date()
    starts = []
    for offset in range(HORIZON_DAYS):
        day = today + timedelta(days=offset)
        for start, _ in _windows_on(day):
            instant = local_to_utc(day, start, TZ)
            if instant is not None and instant >= now + MIN_LEAD:
                starts.append(instant.astimezone(TZ))
    return starts


def _check_window_specs(
    scenario: Scenario, where: str, specs: list[WindowSpec], problems: list[str]
) -> None:
    starts = bookable_starts(scenario.clock)
    for spec in specs:
        if not any(spec.contains(s) for s in starts):
            problems.append(
                f"{scenario.id}: {where} window {spec.on} {spec.start_from}-{spec.start_until} "
                "contains no bookable window start"
            )


def validate(scenarios: list[Scenario]) -> list[str]:
    """Problems that schema validation can't see. An empty list means every scenario is
    consistent with the seed business."""
    problems: list[str] = []
    counts = Counter(s.id for s in scenarios)
    problems += [f"duplicate scenario id {i}" for i, n in counts.items() if n > 1]

    for s in scenarios:
        if (s.category in SCRIPTED_CATEGORIES or SCRIPTED_TAGS & set(s.tags)) and not isinstance(
            s.customer, ScriptedCustomer
        ):
            problems.append(f"{s.id}: {s.category} scenarios use scripted customers (D-018)")

        starts = set(bookable_starts(s.clock))
        fixture = s.fixture
        if fixture.bookings and not (
            fixture.customer and fixture.customer.name and fixture.customer.address
        ):
            problems.append(f"{s.id}: fixture bookings need a customer with name and address")
        customer_zip = fixture.customer.zip if fixture.customer else None
        if fixture.bookings and customer_zip not in SERVICE_AREA_ZIPS:
            problems.append(f"{s.id}: fixture bookings need a customer ZIP in the service area")
        labels = [b.label for b in fixture.bookings]
        if len(labels) != len(set(labels)):
            problems.append(f"{s.id}: duplicate fixture booking labels")
        for b in fixture.bookings:
            if b.service not in BOOKABLE:
                problems.append(f"{s.id}: fixture booking {b.label} service {b.service!r}")
            if b.start.astimezone(TZ) not in starts:
                problems.append(f"{s.id}: fixture booking {b.label} isn't a bookable window")
        for busy in fixture.busy:
            if busy.start is not None and busy.start.astimezone(TZ) not in starts:
                problems.append(f"{s.id}: busy start {busy.start} isn't a bookable window")
        for off in fixture.time_off:
            if off.tech not in TECH_NAMES:
                problems.append(f"{s.id}: unknown technician {off.tech!r}")
            if off.end <= off.start:
                problems.append(f"{s.id}: time off for {off.tech} ends before it starts")

        expect = s.expect
        if expect.bookings:
            for code in expect.bookings.service:
                if code not in BOOKABLE:
                    problems.append(f"{s.id}: expected service {code!r} isn't bookable")
            _check_window_specs(s, "expected booking", expect.bookings.windows, problems)
        for label, existing in expect.existing.items():
            if label not in labels:
                problems.append(f"{s.id}: expectation for unknown fixture booking {label!r}")
            _check_window_specs(s, f"rescheduled {label}", existing.windows, problems)
        if expect.safety_reply is not None:
            sent = len(s.customer.messages) if isinstance(s.customer, ScriptedCustomer) else 1 << 30
            if expect.safety_reply.at >= sent:
                problems.append(f"{s.id}: safety_reply.at is past the last scripted message")

    by_category: dict[Category, list[Scenario]] = {}
    for s in scenarios:
        by_category.setdefault(s.category, []).append(s)
    for category, members in by_category.items():
        dev = sum(1 for s in members if s.split == "dev")
        target = round(len(members) * DEV_SHARE)
        if abs(dev - target) > 1:
            problems.append(
                f"{category}: {dev} of {len(members)} scenarios are dev; the stratified "
                f"70/30 split wants {target}"
            )
    return problems


def select(
    scenarios: list[Scenario],
    *,
    split: str = "dev",
    categories: list[str] | None = None,
    ids: list[str] | None = None,
) -> list[Scenario]:
    chosen = [s for s in scenarios if split == "all" or s.split == split]
    if categories:
        chosen = [s for s in chosen if s.category in categories]
    if ids:
        wanted = set(ids)
        chosen = [s for s in scenarios if s.id in wanted]  # explicit ids ignore the split
    return chosen
