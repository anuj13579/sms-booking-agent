"""Injectable clock (D-009). Nothing in the domain calls ``datetime.now()`` directly, so tests and
eval scenarios can freeze time on any date, including DST transitions."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant, timezone-aware, in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that only moves when told to."""

    def __init__(self, at: datetime) -> None:
        self._now = _require_aware(at).astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        self._now = _require_aware(at).astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime: pass a timezone-aware value")
    return value
