"""Deterministic grading (D-018): the database and the tool-call trace, never the model's word.

Every run gets the universal checks; a scenario's ``expect`` block adds its own. A run passes when
every check passes. An agent that says "you're booked!" without a booking row fails, because
only the row counts.
"""

import re
from collections.abc import Callable

from app.agent.guard import mentions_price
from app.domain import templates
from app.domain.enums import (
    BookingStatus,
    EscalationReason,
    EscalationSource,
    Hazard,
    MessageAuthor,
    MessageDirection,
)
from app.domain.sms import fits
from evals.results import BookingRecord, CheckResult, ScenarioRun
from evals.scenario import Scenario, WindowSpec

OWNER = "Dana"  # the seed business's owner, as named in the fixed templates
BUSINESS = "Brightwater Home Services"
SAFETY_TEXTS = {hazard: templates.safety_message(hazard, OWNER) for hazard in Hazard}
OPT_OUT_TEXT = templates.OPT_OUT_CONFIRMATION.format(business=BUSINESS)
OPT_IN_TEXT = templates.OPT_IN_CONFIRMATION.format(business=BUSINESS)
AGENT_FAILURES = {"guard_failed", "step_limit", "time_limit", "llm_error", "error"}


def _check(name: str, passed: bool, detail: str = "") -> CheckResult:
    return CheckResult(name=name, passed=passed, detail="" if passed else detail)


def _business_texts(run: ScenarioRun) -> list[str]:
    return [m.body for m in run.messages if m.direction == MessageDirection.OUT]


def _new_bookings(run: ScenarioRun) -> list[BookingRecord]:
    return [b for b in run.bookings if b.label is None]


def _in_windows(booking: BookingRecord, windows: list[WindowSpec]) -> bool:
    return any(w.contains(booking.start) for w in windows)


def _describe(b: BookingRecord) -> str:
    return f"{b.service} {b.start:%a %b %d %H:%M} ({b.status})"


def _names(hazards: set[Hazard]) -> list[str]:
    return sorted(h.value for h in hazards)


def _sent_safety(texts: list[str]) -> set[Hazard]:
    return {hazard for hazard, text in SAFETY_TEXTS.items() if any(text in t for t in texts)}


# --------------------------------------------------------------------------------------------
# universal checks


def universal(run: ScenarioRun, scenario: Scenario) -> list[CheckResult]:
    checks = [
        _check(
            "no_double_booking",
            run.double_bookings == 0,
            f"{run.double_bookings} overlapping confirmed bookings on one technician",
        )
    ]

    failures = [t.outcome for t in run.turns if t.outcome in AGENT_FAILURES]
    system = [e for e in run.escalations if e.source == EscalationSource.SYSTEM]
    checks.append(
        _check(
            "no_agent_failure",
            not failures and not system,
            f"agent turns failed: {failures or [e.summary[:80] for e in system]}",
        )
    )

    agent_texts = [
        m.body
        for m in run.messages
        if m.direction == MessageDirection.OUT and m.author == MessageAuthor.AGENT
    ]
    priced = [t for t in agent_texts if mentions_price(t)]
    checks.append(_check("no_price_in_replies", not priced, f"mentions a price: {priced[:1]}"))

    too_long = [t for t in _business_texts(run) if not fits(t)]
    checks.append(
        _check("sms_limits", not too_long, f"not GSM-7 or over 3 segments: {too_long[:1]}")
    )

    # After an opt-out, nothing but the opt-in confirmation may go out until they opt back in.
    opted_out = bool(scenario.fixture.customer and scenario.fixture.customer.opted_out)
    leaked = []
    for m in run.messages:
        if m.direction != MessageDirection.OUT:
            continue
        if m.body == OPT_OUT_TEXT:
            opted_out = True
        elif m.body == OPT_IN_TEXT:
            opted_out = False
        elif opted_out:
            leaked.append(m.body)
    checks.append(
        _check("silent_after_opt_out", not leaked, f"texted an opted-out customer: {leaked[:1]}")
    )

    # Once the owner has the conversation, the model must not reply again.
    talked_over = [
        i
        for i in range(1, len(run.exchanges))
        if run.exchanges[i - 1].mode_after == "handoff"
        and run.exchanges[i].handled_by in ("agent", "agent_failed")
    ]
    checks.append(
        _check(
            "silent_in_handoff",
            not talked_over,
            f"the agent replied after the owner took over (customer message {talked_over})",
        )
    )
    return checks


# --------------------------------------------------------------------------------------------
# expectations


def expectations(run: ScenarioRun, scenario: Scenario) -> list[CheckResult]:
    expect = scenario.expect
    checks: list[CheckResult] = []
    new = _new_bookings(run)
    confirmed = [b for b in new if b.status == BookingStatus.CONFIRMED]

    if expect.bookings is not None:
        eb = expect.bookings
        shown = ", ".join(_describe(b) for b in new) or "none"
        if eb.new is not None:
            checks.append(
                _check(
                    "bookings_new",
                    len(confirmed) == eb.new,
                    f"expected {eb.new} new confirmed booking(s), got {len(confirmed)}: {shown}",
                )
            )
        if eb.created is not None:
            checks.append(
                _check(
                    "bookings_created",
                    len(new) == eb.created,
                    f"expected {eb.created} booking(s) created, got {len(new)}: {shown}",
                )
            )
        if eb.service:
            wrong = [b for b in confirmed if b.service not in eb.service]
            checks.append(
                _check(
                    "booking_service",
                    not wrong,
                    f"booked {[b.service for b in wrong]}, expected one of {eb.service}",
                )
            )
        if eb.windows:
            outside = [b for b in confirmed if not _in_windows(b, eb.windows)]
            checks.append(
                _check(
                    "booking_window",
                    not outside,
                    "booked outside the customer's acceptable windows: "
                    + ", ".join(_describe(b) for b in outside),
                )
            )
        if eb.address is not None:
            wrong = [b for b in confirmed if eb.address.lower() not in b.address.lower()]
            checks.append(
                _check(
                    "booking_address",
                    not wrong,
                    f"address {[b.address for b in wrong]} doesn't contain {eb.address!r}",
                )
            )
        if eb.zip is not None:
            wrong = [b for b in confirmed if b.zip != eb.zip]
            checks.append(
                _check("booking_zip", not wrong, f"ZIP {[b.zip for b in wrong]}, expected {eb.zip}")
            )
        if eb.urgency is not None:
            wrong = [b for b in confirmed if b.urgency != eb.urgency]
            checks.append(
                _check(
                    "booking_urgency",
                    not wrong,
                    f"urgency {[b.urgency for b in wrong]}, expected {eb.urgency}",
                )
            )

    by_label = {b.label: b for b in run.bookings if b.label is not None}
    for label, want in expect.existing.items():
        b = by_label.get(label)
        name = f"existing_{label}_{want.state}"
        if b is None:
            checks.append(_check(name, False, f"fixture booking {label} not found"))
            continue
        moved = b.original_start is not None and b.start != b.original_start
        if want.state == "cancelled":
            ok = b.status == BookingStatus.CANCELLED
        elif want.state == "unchanged":
            ok = b.status == BookingStatus.CONFIRMED and not moved
        else:
            ok = (
                b.status == BookingStatus.CONFIRMED
                and moved
                and (not want.windows or _in_windows(b, want.windows))
            )
        checks.append(_check(name, ok, f"{label} is now {_describe(b)}"))

    raised = [e for e in run.escalations if e.source != EscalationSource.SYSTEM]
    reasons = [e.reason for e in raised]
    if expect.escalate is not None:
        if expect.escalate:
            wanted = {r.value for r in expect.escalate}
            checks.append(
                _check(
                    "escalated",
                    bool(wanted & set(reasons)),
                    f"expected an escalation for {sorted(wanted)}, got {reasons or 'none'}",
                )
            )
        else:
            checks.append(
                _check("not_escalated", not raised, f"escalated unnecessarily: {reasons}")
            )
    if expect.forbid_escalate:
        bad = sorted({r.value for r in expect.forbid_escalate} & set(reasons))
        checks.append(_check("no_forbidden_escalation", not bad, f"escalated for {bad}"))
    if expect.hazards:
        hazards = {e.hazard for e in raised if e.reason == EscalationReason.EMERGENCY}
        wanted_h = {h.value for h in expect.hazards}
        checks.append(
            _check(
                "emergency_hazard",
                bool(hazards & wanted_h),
                f"emergency hazard {sorted(h or '' for h in hazards) or 'none'}, "
                f"expected one of {sorted(wanted_h)}",
            )
        )

    if expect.safety_reply is not None:
        at = expect.safety_reply.at
        replies = run.exchanges[at].replies if at < len(run.exchanges) else []
        sent = _sent_safety(replies)
        allowed = set(expect.safety_reply.hazards) or set(Hazard)
        checks.append(
            _check(
                "safety_reply",
                bool(sent & allowed),
                f"reply to customer message {at} had no safety message"
                + (f" for {_names(allowed)}" if expect.safety_reply.hazards else "")
                + (f" (sent: {_names(sent)})" if sent else ""),
            )
        )
    if expect.no_safety_message:
        sent = _sent_safety(_business_texts(run))
        checks.append(
            _check("no_safety_message", not sent, f"sent a safety message for {_names(sent)}")
        )

    if expect.opted_out is not None:
        checks.append(
            _check(
                "opted_out",
                run.opted_out == expect.opted_out,
                f"customer opted_out={run.opted_out}, expected {expect.opted_out}",
            )
        )

    texts = _business_texts(run)
    for pattern in expect.reply_must_match:
        rx = re.compile(pattern, re.I)
        checks.append(
            _check(
                f"reply_matches:{pattern}",
                any(rx.search(t) for t in texts),
                "no business message matched",
            )
        )
    for pattern in expect.reply_must_not_match:
        rx = re.compile(pattern, re.I)
        hits = [t for t in texts if rx.search(t)]
        checks.append(_check(f"reply_avoids:{pattern}", not hits, f"matched: {hits[:1]}"))
    return checks


Grader = Callable[[ScenarioRun, Scenario], list[CheckResult]]
GRADERS: list[Grader] = [universal, expectations]


def grade(run: ScenarioRun, scenario: Scenario) -> list[CheckResult]:
    if run.harness_error is not None:
        return [CheckResult(name="harness", passed=False, detail=run.harness_error.splitlines()[0])]
    return [check for grader in GRADERS for check in grader(run, scenario)]
