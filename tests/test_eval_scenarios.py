"""The scenario files themselves: they load, they're consistent with the seed business, and each
category means what DESIGN section 9 says it means. No database, no LLM."""

from collections import Counter
from datetime import UTC, datetime, time
from typing import Any

import pytest

from app.pipeline.safety import detect_hazards
from evals.scenario import (
    TARGET_COUNTS,
    Category,
    Scenario,
    ScriptedCustomer,
    SimulatedCustomer,
    WindowSpec,
    load_all,
    validate,
)

SCENARIOS = load_all()


def by_category(category: Category) -> list[Scenario]:
    return [s for s in SCENARIOS if s.category == category]


def test_every_scenario_is_consistent_with_the_seed_business() -> None:
    assert validate(SCENARIOS) == []


def test_category_counts_match_the_design() -> None:
    assert Counter(s.category for s in SCENARIOS) == TARGET_COUNTS


def test_clear_emergencies_are_ones_the_prefilter_catches() -> None:
    for s in by_category(Category.CLEAR_EMERGENCY):
        assert isinstance(s.customer, ScriptedCustomer)
        at = s.expect.safety_reply.at if s.expect.safety_reply else 0
        assert detect_hazards(s.customer.messages[at]), s.id
        assert not any(detect_hazards(m) for m in s.customer.messages[:at]), s.id


def test_subtle_emergencies_get_past_the_prefilter() -> None:
    """Otherwise they'd measure the regex, not the model."""
    for s in by_category(Category.SUBTLE_EMERGENCY):
        assert isinstance(s.customer, ScriptedCustomer)
        assert not any(detect_hazards(m) for m in s.customer.messages), s.id


def test_simulated_customers_who_must_book_know_when_they_are_free() -> None:
    for s in SCENARIOS:
        books = s.expect.bookings is not None and bool(s.expect.bookings.windows)
        if isinstance(s.customer, SimulatedCustomer) and books:
            assert s.customer.persona.availability, s.id


def test_injection_scenarios_are_scripted() -> None:
    injections = [s for s in SCENARIOS if "injection" in s.tags]
    assert injections
    assert all(isinstance(s.customer, ScriptedCustomer) for s in injections)


# --------------------------------------------------------------------------------------------
# schema details


def minimal(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "x-1",
        "category": "normal_booking",
        "split": "dev",
        "title": "t",
        "clock": "2026-10-06T09:15",
        "customer": {"kind": "scripted", "messages": ["hi"]},
        "expect": {},
    }
    return data | overrides


def test_naive_clock_is_new_york_wall_time() -> None:
    s = Scenario.model_validate(minimal())
    assert s.clock == datetime(2026, 10, 6, 13, 15, tzinfo=UTC)  # EDT is UTC-4


def test_clock_after_the_fall_back_uses_standard_time() -> None:
    s = Scenario.model_validate(minimal(clock="2026-11-02T09:00"))
    assert s.clock == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)  # EST is UTC-5


def test_a_wall_time_in_the_spring_gap_is_rejected() -> None:
    with pytest.raises(ValueError, match="DST gap"):
        Scenario.model_validate(minimal(clock="2027-03-14T02:30"))


def test_window_spec_with_a_date_range() -> None:
    spec = WindowSpec.model_validate(
        {"date": "2026-10-07", "through": "2026-10-09", "from": "08:00", "until": "11:59"}
    )
    tz = Scenario.model_validate(minimal()).local_clock.tzinfo
    assert spec.contains(datetime(2026, 10, 8, 10, 0, tzinfo=tz))
    assert not spec.contains(datetime(2026, 10, 8, 13, 0, tzinfo=tz))
    assert not spec.contains(datetime(2026, 10, 10, 9, 0, tzinfo=tz))


def test_yaml_base_60_times_are_read_as_clock_times() -> None:
    """YAML 1.1 reads an unquoted 8:00 as 480."""
    spec = WindowSpec.model_validate({"date": "2026-10-07", "from": 480, "until": 720})
    assert (spec.start_from, spec.start_until) == (time(8), time(12))


def test_existing_booking_shorthand() -> None:
    s = Scenario.model_validate(minimal(expect={"existing": {"b1": "cancelled"}}))
    assert s.expect.existing["b1"].state == "cancelled"


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        Scenario.model_validate(minimal(expect={"bookigns": {"new": 1}}))


def test_validation_catches_a_window_that_cannot_be_booked() -> None:
    sunday = minimal(expect={"bookings": {"windows": [{"date": "2026-10-11"}]}})
    problems = validate([Scenario.model_validate(sunday)])
    assert any("contains no bookable window" in p for p in problems)


def test_validation_requires_scripted_customers_for_emergencies() -> None:
    data = minimal(
        category="clear_emergency",
        customer={
            "kind": "simulated",
            "opening": "gas smell",
            "persona": {"name": "A", "situation": "s", "goal": "g"},
        },
    )
    problems = validate([Scenario.model_validate(data)])
    assert any("scripted customers" in p for p in problems)
