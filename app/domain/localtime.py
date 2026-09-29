"""Local wall time <-> UTC, and customer-facing labels (D-009).

Rules:

* Storage is always UTC (``timestamptz``). The business's IANA zone is used only to turn local
  wall times into instants and back.
* Conversion happens **per date**: 08:00 on Oct 30 2026 is 12:00 UTC (EDT), 08:00 on Nov 2 2026
  is 13:00 UTC (EST).
* Labels are plain ASCII (hyphen, not an en dash). One non-GSM-7 character switches a whole SMS to
  UCS-2 encoding, which cuts a segment from 160 to 70 characters and roughly doubles the cost.
"""

from datetime import UTC, date, datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class UnknownTimezoneError(ValueError):
    pass


@lru_cache(maxsize=64)
def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezoneError(f"unknown IANA time zone: {name!r}") from exc


def local_to_utc(day: date, wall: time, tz: ZoneInfo) -> datetime | None:
    """The UTC instant of a local wall-clock time on a given date.

    Returns ``None`` if that wall time does not exist on that date (the spring-forward gap, e.g.
    02:30 on 2027-03-14 in New York). An ambiguous time (the repeated hour at fall-back) resolves
    to its first occurrence, which is ``fold=0`` in zoneinfo.
    """
    naive = datetime.combine(day, wall)
    instant = naive.replace(tzinfo=tz).astimezone(UTC)
    if instant.astimezone(tz).replace(tzinfo=None) != naive:
        return None
    return instant


def to_local(instant: datetime, tz: ZoneInfo) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("naive datetime: pass a timezone-aware value")
    return instant.astimezone(tz)


def local_today(now: datetime, tz: ZoneInfo) -> date:
    return to_local(now, tz).date()


def day_label(day: date) -> str:
    """``Tue Sep 29``. English names are hard-coded so output never depends on the OS locale."""
    return f"{_DAYS[day.weekday()]} {_MONTHS[day.month - 1]} {day.day}"


def _clock_parts(t: time) -> tuple[str, str]:
    hour12 = t.hour % 12 or 12
    meridiem = "am" if t.hour < 12 else "pm"
    text = str(hour12) if t.minute == 0 else f"{hour12}:{t.minute:02d}"
    return text, meridiem


def window_label(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    """``Tue Sep 29, 10am-12pm``; ``Tue Sep 29, 1-3pm`` when both ends share am/pm."""
    local_start, local_end = to_local(start, tz), to_local(end, tz)
    s_text, s_mer = _clock_parts(local_start.time())
    e_text, e_mer = _clock_parts(local_end.time())
    span = f"{s_text}-{e_text}{e_mer}" if s_mer == e_mer else f"{s_text}{s_mer}-{e_text}{e_mer}"
    return f"{day_label(local_start.date())}, {span}"


def slot_id(start: datetime, tz: ZoneInfo) -> str:
    """Stable identifier for an arrival window: its local start with the UTC offset,
    e.g. ``2026-09-29T10:00-04:00``. Readable for the model, unambiguous even at fall-back."""
    return to_local(start, tz).isoformat(timespec="minutes")


def parse_slot_id(value: str) -> datetime:
    """Inverse of :func:`slot_id`. Raises ``ValueError`` on anything without an explicit offset."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"slot id has no UTC offset: {value!r}")
    return parsed.astimezone(UTC)
