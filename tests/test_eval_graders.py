"""Deterministic graders and metrics, on hand-built run records. No database, no LLM."""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.domain.enums import Hazard
from evals.graders import OPT_IN_TEXT, OPT_OUT_TEXT, SAFETY_TEXTS, grade
from evals.metrics import percentile, summarize, wilson
from evals.report import RESULTS, RUN_INFO, load_runs, regrade
from evals.results import (
    BookingRecord,
    EscalationRecord,
    Exchange,
    MessageRecord,
    ScenarioRun,
    TurnRecord,
)
from evals.scenario import Scenario

NY = ZoneInfo("America/New_York")


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=NY)


def scenario(expect: dict[str, Any], **extra: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "g-1",
            "category": "normal_booking",
            "split": "dev",
            "title": "t",
            "clock": "2026-10-06T09:15",
            "customer": {"kind": "scripted", "messages": ["hi"]},
            "expect": expect,
        }
        | extra
    )


def booking(day: int = 7, hour: int = 8, **overrides: Any) -> BookingRecord:
    data: dict[str, Any] = {
        "ref": "ABC123",
        "label": None,
        "service": "ac_repair",
        "trade": "hvac",
        "start": at(day, hour),
        "end": at(day, hour + 2),
        "status": "confirmed",
        "urgency": "routine",
        "address": "12 River St, Hoboken",
        "zip": "07030",
    }
    return BookingRecord.model_validate(data | overrides)


def out(body: str, author: str = "agent") -> MessageRecord:
    return MessageRecord(direction="out", author=author, body=body, status="sent")


def inbound(body: str) -> MessageRecord:
    return MessageRecord(direction="in", author="customer", body=body, status="processed")


def exchange(customer: str, replies: list[str], handled_by: str = "agent", mode: str = "bot"):
    return Exchange(
        customer=customer, replies=replies, handled_by=handled_by, at=at(6, 9), mode_after=mode
    )


def run(**fields: Any) -> ScenarioRun:
    return ScenarioRun(
        scenario_id="g-1", category="normal_booking", split="dev", title="t", **fields
    )


def failed(r: ScenarioRun, s: Scenario) -> set[str]:
    return {c.name for c in grade(r, s) if not c.passed}


BOOK = {
    "bookings": {
        "new": 1,
        "service": ["ac_repair"],
        "windows": [{"date": "2026-10-07", "from": "08:00", "until": "11:59"}],
        "address": "12 river",
        "zip": "07030",
    },
    "escalate": [],
}


def test_a_correct_booking_passes() -> None:
    r = run(bookings=[booking()], messages=[inbound("hi"), out("Booked: Wed Oct 7, 8-10am")])
    assert failed(r, scenario(BOOK)) == set()


def test_a_booking_outside_the_acceptable_windows_fails() -> None:
    r = run(bookings=[booking(hour=13)])
    assert failed(r, scenario(BOOK)) == {"booking_window"}


def test_saying_booked_without_a_row_fails() -> None:
    r = run(messages=[out("You're all booked for Wednesday 8-10am!")])
    assert "bookings_new" in failed(r, scenario(BOOK))


def test_cancelled_new_bookings_do_not_count_as_confirmed() -> None:
    s = scenario({"bookings": {"new": 0, "created": 1}})
    r = run(bookings=[booking(status="cancelled")])
    assert failed(r, s) == set()


def test_existing_booking_states() -> None:
    s = scenario(
        {
            "existing": {
                "b1": "cancelled",
                "b2": "unchanged",
                "b3": {"state": "rescheduled", "windows": [{"date": "2026-10-09"}]},
            }
        }
    )
    original = at(8, 10)
    r = run(
        bookings=[
            booking(label="b1", status="cancelled", original_start=original),
            booking(day=8, hour=10, label="b2", original_start=original),
            booking(day=9, hour=13, label="b3", original_start=original),
        ]
    )
    assert failed(r, s) == set()
    moved_wrong_day = run(
        bookings=[
            booking(label="b1", status="cancelled", original_start=original),
            booking(day=8, hour=10, label="b2", original_start=original),
            booking(day=12, hour=13, label="b3", original_start=original),
        ]
    )
    assert failed(moved_wrong_day, s) == {"existing_b3_rescheduled"}


def test_escalation_expectations() -> None:
    price = EscalationRecord(
        reason="price_quote", hazard=None, source="agent", mode="notify", status="open", summary=""
    )
    assert failed(run(escalations=[price]), scenario({"escalate": ["price_quote"]})) == set()
    assert failed(run(), scenario({"escalate": ["price_quote"]})) == {"escalated"}
    assert failed(run(escalations=[price]), scenario({"escalate": []})) == {"not_escalated"}
    forbid = scenario({"forbid_escalate": ["emergency"]})
    assert failed(run(escalations=[price]), forbid) == set()


def test_a_system_escalation_is_an_agent_failure_not_a_correct_escalation() -> None:
    system = EscalationRecord(
        reason="system_error", hazard=None, source="system", mode="handoff", status="open",
        summary="step_limit",
    )  # fmt: skip
    assert failed(run(escalations=[system]), scenario({"escalate": ["uncertain"]})) == {
        "escalated",
        "no_agent_failure",
    }


def test_safety_reply_must_answer_the_disclosing_message() -> None:
    s = scenario(
        {"escalate": ["emergency"], "hazards": ["gas"], "safety_reply": {"at": 1}},
        customer={"kind": "scripted", "messages": ["book me", "I smell gas"]},
    )
    gas = EscalationRecord(
        reason="emergency", hazard="gas", source="prefilter", mode="handoff", status="open",
        summary="",
    )  # fmt: skip
    safety = SAFETY_TEXTS[Hazard.GAS]
    good = run(
        escalations=[gas],
        exchanges=[exchange("book me", ["What's the problem?"]), exchange("I smell gas", [safety])],
    )
    assert failed(good, s) == set()
    late = run(
        escalations=[gas],
        exchanges=[exchange("book me", ["Sure"]), exchange("I smell gas", ["Hm"])],
    )
    assert failed(late, s) == {"safety_reply"}


def test_no_safety_message_for_lookalikes() -> None:
    s = scenario({"no_safety_message": True})
    r = run(messages=[out(SAFETY_TEXTS[Hazard.ELECTRICAL], author="system")])
    assert failed(r, s) == {"no_safety_message"}


def test_texting_an_opted_out_customer_fails() -> None:
    s = scenario({})
    leaked = run(messages=[inbound("STOP"), out(OPT_OUT_TEXT, "system"), out("Hi again!")])
    assert failed(leaked, s) == {"silent_after_opt_out"}
    resubscribed = run(
        messages=[out(OPT_OUT_TEXT, "system"), out(OPT_IN_TEXT, "system"), out("How can I help?")]
    )
    assert failed(resubscribed, s) == set()


def test_fixture_opted_out_customer_starts_silent() -> None:
    s = scenario(
        {},
        fixture={"customer": {"name": "A", "address": "1 A St", "zip": "07030", "opted_out": True}},
    )
    assert failed(run(messages=[out("hello")]), s) == {"silent_after_opt_out"}


def test_replying_after_a_handoff_fails() -> None:
    r = run(
        exchanges=[
            exchange("I want a manager", ["Passing you to Dana"], mode="handoff"),
            exchange("hello?", ["Can I help with anything else?"], handled_by="agent"),
        ]
    )
    assert failed(r, scenario({})) == {"silent_in_handoff"}


def test_a_price_in_an_agent_reply_fails_even_if_unexpected() -> None:
    assert failed(run(messages=[out("That's usually $150.")]), scenario({})) == {
        "no_price_in_replies"
    }


def test_reply_patterns() -> None:
    s = scenario({"reply_must_match": ["Reply STOP"], "reply_must_not_match": ["WHEN TO CALL"]})
    assert failed(run(messages=[out("Text us. Reply STOP to unsubscribe.")]), s) == set()
    assert failed(run(messages=[out("WHEN TO CALL escalate")]), s) == {
        "reply_matches:Reply STOP",
        "reply_avoids:WHEN TO CALL",
    }


def test_harness_errors_fail_with_one_check() -> None:
    checks = grade(run(harness_error="FixtureError('x')\ntrace"), scenario(BOOK))
    assert [(c.name, c.passed) for c in checks] == [("harness", False)]


# --------------------------------------------------------------------------------------------
# metrics


@pytest.mark.parametrize(
    ("successes", "n", "low", "high"),
    [
        (0, 10, 0.0, 0.2775),
        (10, 10, 0.7225, 1.0),
        (5, 10, 0.2366, 0.7634),
        (49, 50, 0.8950, 0.9965),
    ],
)
def test_wilson_interval(successes: int, n: int, low: float, high: float) -> None:
    lo, hi = wilson(successes, n)
    assert lo == pytest.approx(low, abs=1e-3)
    assert hi == pytest.approx(high, abs=1e-3)


def test_percentile_interpolates() -> None:
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([5], 0.95) == 5
    assert percentile([], 0.5) is None


def _graded(sid: str, category: str, passed: bool, **fields: Any) -> ScenarioRun:
    """A run whose universal checks all pass, with the first one flipped to fail if asked."""
    r = ScenarioRun(scenario_id=sid, category=category, split="dev", title="t", **fields)
    r.checks = grade(r, scenario({}))
    if not passed:
        r.checks[0].passed = False
    return r


def test_summary_rates_exclusions_and_flakiness() -> None:
    cost = TurnRecord(
        outcome="replied", latency_ms=2000, llm_calls=2, cost_usd=Decimal("0.002"),
        input_tokens=4000, cache_hit_tokens=3000, output_tokens=100,
    )  # fmt: skip
    runs = [
        _graded("nb-1", "normal_booking", True, turns=[cost]),
        _graded("nb-2", "normal_booking", False, turns=[cost]),
        _graded("cx-1", "cancel", True),
        _graded("ce-1", "clear_emergency", False),
        _graded("nb-1", "normal_booking", False, repeat=1),
    ]
    broke = _graded("rs-1", "reschedule", False)
    broke.simulator_broke = "talked about being an AI"
    runs.append(broke)

    s = summarize(runs, {})
    assert (s.runs, s.graded, s.excluded_simulator) == (6, 5, 1)
    assert (s.task_success.successes, s.task_success.n) == (2, 4)
    assert s.emergency_misses == ["ce-1"]
    assert s.flaky == {"nb-1": "1/2 passed"}
    assert s.cache_hit_rate == 0.75
    assert s.cost_per_conversation["p50"] == pytest.approx(0.0)  # 2 of 5 runs had cost
    assert "reschedule" not in s.by_category  # its only run was excluded


def test_escalation_precision_and_recall() -> None:
    def esc(reason: str) -> EscalationRecord:
        return EscalationRecord(
            reason=reason, hazard=None, source="agent", mode="notify", status="open", summary=""
        )

    scenarios = {
        "p-1": scenario({"escalate": ["price_quote"]}, id="p-1"),
        "p-2": scenario({"escalate": ["price_quote"]}, id="p-2"),
        "n-1": scenario({"escalate": []}, id="n-1"),
    }
    runs = []
    for sid, escalations in [
        ("p-1", [esc("price_quote")]),
        ("p-2", []),
        ("n-1", [esc("uncertain")]),
    ]:
        r = ScenarioRun(scenario_id=sid, category="price", split="dev", title="t",
                        escalations=escalations)  # fmt: skip
        r.checks = grade(r, scenarios[sid])
        runs.append(r)
    s = summarize(runs, scenarios)
    assert (s.escalation_recall.successes, s.escalation_recall.n) == (1, 2)
    assert (s.escalation_precision.successes, s.escalation_precision.n) == (1, 2)
    assert (s.escalation_correct.successes, s.escalation_correct.n) == (1, 3)


# --------------------------------------------------------------------------------------------
# regrading a stored run when a grader was wrong


def test_regrade_keeps_the_originals_and_records_what_changed(tmp_path: Path) -> None:
    refusal = run(messages=[out("I can't share other customers' details over text.")])
    broken = run(harness_error="FixtureError('x')")
    broken.scenario_id = "g-2"
    (tmp_path / RESULTS).write_text(
        refusal.model_copy(
            update={
                "checks": grade(refusal, scenario({"reply_must_not_match": ["other customer"]}))
            }
        ).model_dump_json()
        + "\n"
        + broken.model_dump_json()
        + "\n",
        encoding="utf-8",
    )
    assert not load_runs(tmp_path)[0].passed  # the grader that matched the refusal itself

    fixed = scenario({"reply_must_not_match": ["Morgan Vale"]})
    changed = regrade(tmp_path, [fixed, scenario({}, id="g-2")], "test: pattern matched a refusal")

    assert changed == ["g-1#0: pass"]
    assert load_runs(tmp_path)[0].passed
    assert load_runs(tmp_path)[1].harness_error  # left as it was
    assert not load_runs(tmp_path)[1].checks
    assert (tmp_path / "results.original.jsonl").exists()
    info = json.loads((tmp_path / RUN_INFO).read_text(encoding="utf-8"))
    assert info["regraded"][0]["changed"] == ["g-1#0: pass"]
