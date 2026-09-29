"""Closed vocabularies shared by the schema, the domain services, and (later) the agent tools.

The database stores these as text with CHECK constraints rather than Postgres ENUM types:
adding a value is then a one-line migration instead of an ALTER TYPE dance.
"""

from enum import StrEnum


class Trade(StrEnum):
    HVAC = "hvac"
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Urgency(StrEnum):
    ROUTINE = "routine"
    URGENT = "urgent"


class Actor(StrEnum):
    """Who caused a change. Written to audit rows."""

    AGENT = "agent"
    OWNER = "owner"
    SYSTEM = "system"


class BookingEventType(StrEnum):
    CREATED = "created"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Channel(StrEnum):
    SMS = "sms"
    WEBCHAT = "webchat"
    EVAL = "eval"


class ConversationMode(StrEnum):
    BOT = "bot"  # the agent replies
    HANDOFF = "handoff"  # a human owns it; the bot stays silent
    CLOSED = "closed"  # session ended (24 h idle, D-021)


class MessageDirection(StrEnum):
    IN = "in"
    OUT = "out"


class MessageAuthor(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"  # LLM-written text
    OWNER = "owner"
    SYSTEM = "system"  # fixed templates: safety, opt-out, handoff


class MessageStatus(StrEnum):
    # inbound
    RECEIVED = "received"  # stored, waiting for a worker
    PROCESSED = "processed"
    SKIPPED = "skipped"  # deliberately not answered (e.g. customer opted out)
    # outbound
    QUEUED = "queued"  # written, waiting for the channel to send it
    SENT = "sent"
    FAILED = "failed"


class EscalationReason(StrEnum):
    EMERGENCY = "emergency"
    UPSET_CUSTOMER = "upset_customer"
    HUMAN_REQUESTED = "human_requested"
    UNCERTAIN = "uncertain"
    OUT_OF_SCOPE = "out_of_scope"
    PRICE_QUOTE = "price_quote"
    URGENT_NO_AVAILABILITY = "urgent_no_availability"
    SYSTEM_ERROR = "system_error"


class EscalationMode(StrEnum):
    HANDOFF = "handoff"  # bot goes silent until the owner resolves it
    NOTIFY = "notify"  # owner is pinged, bot keeps going


class EscalationStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class EscalationSource(StrEnum):
    PREFILTER = "prefilter"  # deterministic emergency pre-filter
    AGENT = "agent"  # the LLM called escalate_to_owner
    SYSTEM = "system"  # server-raised (loop limits, guard failures)


class Hazard(StrEnum):
    """Ordered from most to least severe; the pre-filter reports the first that matches."""

    GAS = "gas"
    CO = "co"
    ELECTRICAL = "electrical"
    FLOODING = "flooding"
    NO_HEAT = "no_heat"
    OTHER = "other"  # LLM-detected emergency that fits no specific hazard


def sql_in(enum: type[StrEnum]) -> str:
    """Render ``'a', 'b', 'c'`` for a CHECK constraint."""
    return ", ".join(f"'{member.value}'" for member in enum)
