"""What the model sees each turn (D-005, D-015).

The prompt has two parts:

* **System prompt**: the static rules plus the business profile. Identical for every customer of
  the business and every turn, so the provider caches it.
* **Transcript**: every earlier turn's messages replayed exactly as stored, then one new user
  message for this turn: a context note (current time, a date table, what's on file for the
  customer, their upcoming bookings read fresh from the database), anything that happened since
  the agent last spoke (keyword replies, safety texts, owner messages), and the customer's new
  text. Nothing earlier is rewritten, so each turn only appends to the cached prefix.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import system_template
from app.db.models import Business, Customer, Message, ServiceType, WindowTemplate
from app.domain.booking import BookingView
from app.domain.enums import MessageAuthor, MessageDirection
from app.domain.localtime import clock_label, day_label, span_label, to_local, zone

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class ServiceInfo:
    code: str
    label: str
    trade: str
    bookable: bool


@dataclass(frozen=True)
class BusinessProfile:
    name: str
    owner_name: str
    timezone: str
    zips: tuple[str, ...]
    horizon_days: int
    min_lead_minutes: int
    hours: dict[int, list[tuple[time, time]]]  # weekday (0 = Monday) -> windows
    services: tuple[ServiceInfo, ...]

    @property
    def service_codes(self) -> list[str]:
        return [s.code for s in self.services]


async def load_profile(session: AsyncSession, business: Business) -> BusinessProfile:
    services = await session.scalars(
        select(ServiceType)
        .where(ServiceType.business_id == business.id, ServiceType.active.is_(True))
        .order_by(ServiceType.trade, ServiceType.code)
    )
    windows = await session.execute(
        select(WindowTemplate.weekday, WindowTemplate.start_local, WindowTemplate.end_local)
        .where(WindowTemplate.business_id == business.id)
        .order_by(WindowTemplate.weekday, WindowTemplate.start_local)
    )
    hours: dict[int, list[tuple[time, time]]] = defaultdict(list)
    for weekday, start, end in windows:
        hours[weekday].append((start, end))
    return BusinessProfile(
        name=business.name,
        owner_name=business.owner_name,
        timezone=business.timezone,
        zips=tuple(business.service_area_zips),
        horizon_days=business.horizon_days,
        min_lead_minutes=business.min_lead_minutes,
        hours=dict(hours),
        services=tuple(ServiceInfo(s.code, s.label, s.trade, s.bookable) for s in services),
    )


def hours_text(hours: dict[int, list[tuple[time, time]]]) -> str:
    """``Mon-Fri 8-10am, 10am-12pm; Sat 9-11am; Sun closed``: runs of days with equal windows."""
    parts: list[str] = []
    day = 0
    while day < 7:
        windows = hours.get(day, [])
        end = day
        while end + 1 < 7 and hours.get(end + 1, []) == windows:
            end += 1
        days = _WEEKDAYS[day] if end == day else f"{_WEEKDAYS[day]}-{_WEEKDAYS[end]}"
        spans = ", ".join(span_label(s, e) for s, e in windows) if windows else "closed"
        parts.append(f"{days} {spans}")
        day = end + 1
    return "; ".join(parts)


def _duration_text(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"


def render_system_prompt(profile: BusinessProfile, template: str | None = None) -> str:
    services = "\n".join(
        f"- {s.code}: {s.label}" + ("" if s.bookable else " (needs an estimate, not bookable)")
        for s in profile.services
    )
    return (template or system_template()).format(
        business_name=profile.name,
        owner_name=profile.owner_name,
        timezone=profile.timezone,
        hours=hours_text(profile.hours),
        zips=", ".join(profile.zips) or "any",
        horizon_days=profile.horizon_days,
        min_lead=_duration_text(profile.min_lead_minutes),
        services=services,
    )


# --------------------------------------------------------------------------------------------
# the per-turn note


def date_table(now: datetime, tz_name: str, days: int) -> str:
    """``Tue Sep 29 = 2026-09-29 (today), Wed Sep 30 = 2026-09-30 (tomorrow), ...``"""
    today = to_local(now, zone(tz_name)).date()
    entries = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        suffix = " (today)" if offset == 0 else " (tomorrow)" if offset == 1 else ""
        entries.append(f"{day_label(day)} = {day.isoformat()}{suffix}")
    return ", ".join(entries)


def customer_on_file(customer: Customer) -> str:
    parts = []
    if customer.name:
        parts.append(f"name {customer.name}")
    if customer.address:
        address = customer.address
        if customer.zip and customer.zip not in address:
            address += f" {customer.zip}"
        parts.append(f"address {address}")
    elif customer.zip:
        parts.append(f"ZIP {customer.zip}")
    return "; ".join(parts) if parts else "nothing yet (new customer)"


def bookings_text(bookings: Sequence[BookingView]) -> str:
    if not bookings:
        return "none"
    return "; ".join(
        f"ref {b.ref}: {b.service_label} (service {b.service_code}), {b.slot.label}, at {b.address}"
        for b in bookings
    )


def interim_lines(messages: Sequence[Message], owner_name: str) -> list[str]:
    """Messages the agent didn't handle, as it should hear about them."""
    lines = []
    for m in messages:
        if m.direction == MessageDirection.IN:
            lines.append(f"Customer wrote: {m.body}")
        elif m.author == MessageAuthor.OWNER:
            lines.append(f"{owner_name} (owner) texted the customer: {m.body}")
        elif m.author == MessageAuthor.SYSTEM:
            lines.append(f"Automatic message sent to the customer: {m.body}")
        else:
            lines.append(f"You texted: {m.body}")
    return lines


def render_turn_note(
    *,
    now: datetime,
    profile: BusinessProfile,
    customer: Customer,
    upcoming: Sequence[BookingView],
    interim: Sequence[str],
    customer_texts: Sequence[str],
) -> str:
    local_now = to_local(now, zone(profile.timezone))
    lines = [
        "[Context from the booking system, not written by the customer]",
        f"Now: {day_label(local_now.date())} {local_now.year}, "
        f"{clock_label(local_now.time().replace(second=0, microsecond=0))}",
        f"Dates: {date_table(now, profile.timezone, profile.horizon_days)}",
        f"Customer on file: {customer_on_file(customer)}",
        f"Upcoming bookings: {bookings_text(upcoming)}",
    ]
    if interim:
        lines.append("Since your last reply:")
        lines.extend(f"- {line}" for line in interim)
    lines.append(
        "[Customer's new message]"
        if len(customer_texts) == 1
        else "[Customer's new messages, oldest first]"
    )
    lines.extend(customer_texts)
    return "\n".join(lines)


def merge_adjacent_user_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Some providers reject two user messages in a row. That only happens after a turn that
    failed before the model answered; join them rather than drop either."""
    merged: list[dict[str, Any]] = []
    for message in messages:
        if merged and message.get("role") == "user" and merged[-1].get("role") == "user":
            merged[-1] = {
                **merged[-1],
                "content": f"{merged[-1]['content']}\n\n{message['content']}",
            }
        else:
            merged.append(message)
    return merged
