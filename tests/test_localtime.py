"""Time zone handling (D-009), including both 2026-27 US DST transitions."""

from datetime import UTC, date, datetime, time

import pytest

from app.domain.clock import FixedClock
from app.domain.localtime import (
    UnknownTimezoneError,
    local_to_utc,
    parse_slot_id,
    slot_id,
    window_label,
    zone,
)

NY = zone("America/New_York")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestFallBack2026:
    """Clocks go from 02:00 EDT back to 01:00 EST on Sunday 2026-11-01."""

    def test_same_wall_time_maps_to_different_utc_before_and_after(self) -> None:
        assert local_to_utc(date(2026, 10, 30), time(8), NY) == utc(2026, 10, 30, 12)  # EDT, -4
        assert local_to_utc(date(2026, 11, 2), time(8), NY) == utc(2026, 11, 2, 13)  # EST, -5

    def test_on_the_transition_day(self) -> None:
        assert local_to_utc(date(2026, 11, 1), time(0, 30), NY) == utc(2026, 11, 1, 4, 30)
        assert local_to_utc(date(2026, 11, 1), time(8), NY) == utc(2026, 11, 1, 13)

    def test_ambiguous_hour_resolves_to_first_occurrence(self) -> None:
        # 01:30 happens twice; fold=0 is the first (still EDT).
        assert local_to_utc(date(2026, 11, 1), time(1, 30), NY) == utc(2026, 11, 1, 5, 30)


class TestSpringForward2027:
    """Clocks jump from 02:00 EST to 03:00 EDT on Sunday 2027-03-14."""

    def test_same_wall_time_maps_to_different_utc_before_and_after(self) -> None:
        assert local_to_utc(date(2027, 3, 12), time(8), NY) == utc(2027, 3, 12, 13)  # EST
        assert local_to_utc(date(2027, 3, 15), time(8), NY) == utc(2027, 3, 15, 12)  # EDT

    def test_nonexistent_wall_time_is_none(self) -> None:
        assert local_to_utc(date(2027, 3, 14), time(2, 30), NY) is None

    def test_times_either_side_of_the_gap_exist(self) -> None:
        assert local_to_utc(date(2027, 3, 14), time(1, 59), NY) == utc(2027, 3, 14, 6, 59)
        assert local_to_utc(date(2027, 3, 14), time(3, 0), NY) == utc(2027, 3, 14, 7, 0)


@pytest.mark.parametrize(
    ("start", "end", "label"),
    [
        (time(10), time(12), "Tue Sep 29, 10am-12pm"),
        (time(13), time(15), "Tue Sep 29, 1-3pm"),
        (time(8), time(10), "Tue Sep 29, 8-10am"),
        (time(11), time(13), "Tue Sep 29, 11am-1pm"),
        (time(8, 30), time(10), "Tue Sep 29, 8:30-10am"),
        (time(12), time(14), "Tue Sep 29, 12-2pm"),
    ],
)
def test_window_label(start: time, end: time, label: str) -> None:
    day = date(2026, 9, 29)
    s, e = local_to_utc(day, start, NY), local_to_utc(day, end, NY)
    assert s is not None and e is not None
    assert window_label(s, e, NY) == label
    assert label.isascii()  # GSM-7 safe: no en dash


def test_labels_use_local_time_across_dst() -> None:
    fri = window_label(utc(2026, 10, 30, 12), utc(2026, 10, 30, 14), NY)
    mon = window_label(utc(2026, 11, 2, 13), utc(2026, 11, 2, 15), NY)
    assert fri == "Fri Oct 30, 8-10am"
    assert mon == "Mon Nov 2, 8-10am"


def test_slot_id_round_trip_and_carries_offset() -> None:
    edt = utc(2026, 10, 30, 12)
    est = utc(2026, 11, 2, 13)
    assert slot_id(edt, NY) == "2026-10-30T08:00-04:00"
    assert slot_id(est, NY) == "2026-11-02T08:00-05:00"
    assert parse_slot_id(slot_id(edt, NY)) == edt
    assert parse_slot_id(slot_id(est, NY)) == est


def test_slot_id_without_offset_is_rejected() -> None:
    with pytest.raises(ValueError, match="offset"):
        parse_slot_id("2026-10-30T08:00")


def test_unknown_zone() -> None:
    with pytest.raises(UnknownTimezoneError):
        zone("America/Hoboken")


def test_fixed_clock_rejects_naive_and_normalises_to_utc() -> None:
    with pytest.raises(ValueError, match="naive"):
        FixedClock(datetime(2026, 10, 5, 8))
    clock = FixedClock(datetime(2026, 10, 5, 8, tzinfo=NY))
    assert clock.now() == utc(2026, 10, 5, 12)
    assert clock.now().tzinfo is UTC
