"""ORM models. The Alembic migrations are the source of truth for the schema; these mirror them.

tests/test_schema.py fails if the two drift apart.

The agent's own tables (``agent_turns``, ``llm_calls``, ``tool_calls``) are an audit log: what the
model saw, what it called, what it cost. The eval harness reads cost and latency from the same
rows production writes (DESIGN section 9).

Integrity rules that must hold no matter which code path writes (D-006) are declared here as
constraints, not checked in application code:

* ``ex_bookings_technician_no_overlap``: a technician can never have two overlapping confirmed
  bookings. Races, webhook retries, and a misbehaving model all hit the same wall.
* ``uq_bookings_customer_service_start``: at most one confirmed booking per customer, service,
  and window start. A retried ``create_booking`` finds the existing row instead of adding one.
* ``uq_messages_provider_sid``: a webhook delivered twice is stored once.
* ``uq_conversations_open_per_channel``: one open conversation per customer and channel, even
  when two messages from a new customer arrive at the same instant.
"""

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import (
    ARRAY,
    JSONB,
    TSTZRANGE,
    UUID,
    ExcludeConstraint,
    Range,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import (
    Actor,
    AgentTurnOutcome,
    BookingEventType,
    BookingStatus,
    Channel,
    ConversationMode,
    EscalationMode,
    EscalationReason,
    EscalationSource,
    EscalationStatus,
    Hazard,
    MessageAuthor,
    MessageDirection,
    MessageStatus,
    ToolCallStatus,
    Trade,
    Urgency,
    sql_in,
)

E164_REGEX = r"^\+[1-9][0-9]{6,14}$"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _finite_range_check(column: str) -> str:
    return f"NOT isempty({column}) AND NOT lower_inf({column}) AND NOT upper_inf({column})"


class Business(Base):
    __tablename__ = "businesses"
    __table_args__ = (
        CheckConstraint(f"sms_number ~ '{E164_REGEX}'", name="sms_number_e164"),
        CheckConstraint(f"owner_phone ~ '{E164_REGEX}'", name="owner_phone_e164"),
        CheckConstraint("min_lead_minutes >= 0", name="min_lead_nonnegative"),
        CheckConstraint("horizon_days BETWEEN 1 AND 60", name="horizon_days_range"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)  # IANA name, validated in app code
    sms_number: Mapped[str] = mapped_column(Text, unique=True)
    owner_name: Mapped[str] = mapped_column(Text)
    owner_phone: Mapped[str] = mapped_column(Text)
    min_lead_minutes: Mapped[int] = mapped_column(Integer, server_default=text("120"))
    horizon_days: Mapped[int] = mapped_column(Integer, server_default=text("14"))
    service_area_zips: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )
    created_at: Mapped[datetime] = _created_at()


class ServiceType(Base):
    __tablename__ = "service_types"
    __table_args__ = (
        UniqueConstraint("business_id", "code"),
        CheckConstraint(f"trade IN ({sql_in(Trade)})", name="trade_valid"),
        CheckConstraint("code ~ '^[a-z][a-z0-9_]*$'", name="code_format"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    trade: Mapped[str] = mapped_column(Text)
    # False for multi-window jobs (installs, panel upgrades): the agent routes them to the owner
    # as a quote instead of booking a single arrival window (D-007).
    bookable: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class WindowTemplate(Base):
    """A weekly arrival window in the business's local wall time, e.g. Monday 08:00-10:00."""

    __tablename__ = "window_templates"
    __table_args__ = (
        UniqueConstraint("business_id", "weekday", "start_local"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),  # 0 = Monday
        CheckConstraint("start_local < end_local", name="start_before_end"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(SmallInteger)
    start_local: Mapped[time] = mapped_column(Time)
    end_local: Mapped[time] = mapped_column(Time)


class Technician(Base):
    __tablename__ = "technicians"
    __table_args__ = (
        CheckConstraint(
            f"trades <@ ARRAY[{sql_in(Trade)}]::text[] AND cardinality(trades) > 0",
            name="trades_valid",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    trades: Mapped[list[str]] = mapped_column(ARRAY(Text))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class TechnicianTimeOff(Base):
    __tablename__ = "technician_time_off"
    __table_args__ = (CheckConstraint(_finite_range_check("period"), name="period_finite"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    technician_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("technicians.id", ondelete="CASCADE"), index=True
    )
    period: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    reason: Mapped[str | None] = mapped_column(Text)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("business_id", "phone_e164"),
        CheckConstraint(f"phone_e164 ~ '{E164_REGEX}'", name="phone_e164_format"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    phone_e164: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    zip: Mapped[str | None] = mapped_column(Text)
    opted_out_at: Mapped[datetime | None] = mapped_column()  # null = subscribed
    created_at: Mapped[datetime] = _created_at()


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("business_id", "ref"),
        CheckConstraint(f"status IN ({sql_in(BookingStatus)})", name="status_valid"),
        CheckConstraint(f"urgency IN ({sql_in(Urgency)})", name="urgency_valid"),
        CheckConstraint(_finite_range_check("time_window"), name="time_window_finite"),
        ExcludeConstraint(
            ("technician_id", "="),
            ("time_window", "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="ex_bookings_technician_no_overlap",
        ),
        Index(
            "uq_bookings_customer_service_start",
            "customer_id",
            "service_type_id",
            func.lower(text("time_window")),
            unique=True,
            postgresql_where=text("status = 'confirmed'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    ref: Mapped[str] = mapped_column(Text)  # short code the customer sees
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    technician_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("technicians.id"))
    service_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_types.id"))
    time_window: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    status: Mapped[str] = mapped_column(Text, server_default=text("'confirmed'"))
    urgency: Mapped[str] = mapped_column(Text, server_default=text("'routine'"))
    address: Mapped[str] = mapped_column(Text)
    zip: Mapped[str] = mapped_column(Text)
    problem_description: Mapped[str] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class BookingEvent(Base):
    """Append-only audit trail: who changed a booking, when, and from what to what."""

    __tablename__ = "booking_events"
    __table_args__ = (
        CheckConstraint(f"event_type IN ({sql_in(BookingEventType)})", name="event_type_valid"),
        CheckConstraint(f"actor IN ({sql_in(Actor)})", name="actor_valid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    old_window: Mapped[Range[datetime] | None] = mapped_column(TSTZRANGE)
    new_window: Mapped[Range[datetime] | None] = mapped_column(TSTZRANGE)
    old_technician_id: Mapped[uuid.UUID | None] = mapped_column()
    new_technician_id: Mapped[uuid.UUID | None] = mapped_column()
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class Conversation(Base):
    """One session with one customer on one channel. A new one starts after 24 h idle (D-021)."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(f"channel IN ({sql_in(Channel)})", name="channel_valid"),
        CheckConstraint(f"mode IN ({sql_in(ConversationMode)})", name="mode_valid"),
        Index(
            "uq_conversations_open_per_channel",
            "customer_id",
            "channel",
            unique=True,
            postgresql_where=text("mode <> 'closed'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    channel: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, server_default=text("'bot'"))
    started_at: Mapped[datetime] = mapped_column()
    last_activity_at: Mapped[datetime] = mapped_column()
    # Work lease (D-024): which worker is processing this conversation, and until when.
    lease_token: Mapped[uuid.UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(f"direction IN ({sql_in(MessageDirection)})", name="direction_valid"),
        CheckConstraint(f"author IN ({sql_in(MessageAuthor)})", name="author_valid"),
        CheckConstraint(f"status IN ({sql_in(MessageStatus)})", name="status_valid"),
        CheckConstraint(
            "(direction = 'in') = (author = 'customer')", name="author_matches_direction"
        ),
        CheckConstraint(
            "(direction = 'in' AND status IN ('received', 'processed', 'skipped'))"
            " OR (direction = 'out' AND status IN ('queued', 'sent', 'failed'))",
            name="status_matches_direction",
        ),
        # The work queue: conversations with inbound messages nobody has handled yet (D-012).
        Index(
            "ix_messages_pending",
            "conversation_id",
            postgresql_where=text("status = 'received'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Strict arrival order. Timestamps can tie (frozen clocks in evals), and UUIDs are random.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    provider_sid: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column()
    processed_at: Mapped[datetime | None] = mapped_column()
    # The agent turn that consumed this inbound message or wrote this reply. NULL for messages
    # the agent never saw as they happened (keyword replies, safety templates, owner texts);
    # the next turn shows those to the model as "since your last reply" events.
    agent_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_turns.id", ondelete="SET NULL"), index=True
    )


class Escalation(Base):
    __tablename__ = "escalations"
    __table_args__ = (
        CheckConstraint(f"reason IN ({sql_in(EscalationReason)})", name="reason_valid"),
        CheckConstraint(f"hazard IS NULL OR hazard IN ({sql_in(Hazard)})", name="hazard_valid"),
        CheckConstraint(f"mode IN ({sql_in(EscalationMode)})", name="mode_valid"),
        CheckConstraint(f"status IN ({sql_in(EscalationStatus)})", name="status_valid"),
        CheckConstraint(f"source IN ({sql_in(EscalationSource)})", name="source_valid"),
        CheckConstraint(
            "(reason = 'emergency') = (hazard IS NOT NULL)", name="hazard_iff_emergency"
        ),
        Index(
            "ix_escalations_unresolved",
            "conversation_id",
            postgresql_where=text("status <> 'resolved'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    reason: Mapped[str] = mapped_column(Text)
    hazard: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    source: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column()
    resolved_at: Mapped[datetime | None] = mapped_column()


class AgentTurn(Base):
    """One run of the agent loop over a batch of customer messages.

    ``messages`` holds exactly what this turn appended to the model's transcript (context note,
    customer text, tool calls, tool results, the reply). The next turn's prompt is the system
    prompt followed by every earlier turn's ``messages`` unchanged, so the prompt only ever grows
    at the end and the provider's prefix cache keeps hitting (D-015).
    """

    __tablename__ = "agent_turns"
    __table_args__ = (
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({sql_in(AgentTurnOutcome)})", name="outcome_valid"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Transcript order. started_at can tie under a frozen eval clock.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    trigger_message_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    # Every message up to this seq was either shown to this turn or already in the transcript.
    max_message_seq: Mapped[int] = mapped_column(BigInteger)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    started_at: Mapped[datetime] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class LLMCall(Base):
    """One request to the LLM provider, successful or not. Cost is computed at call time from the
    price table in ``app/agent/providers/pricing.py``."""

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_turns.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[int] = mapped_column(SmallInteger)  # 1-based LLM step within the turn
    attempt: Mapped[int] = mapped_column(SmallInteger)  # 2 = the retry after a failed call
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_hit_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reasoning_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 10), server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()  # real wall time: it decides peak pricing


class ToolCallRecord(Base):
    """One tool call the model asked for, with the arguments it sent and the result it got."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(ToolCallStatus)})", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_turns.id", ondelete="CASCADE"), index=True
    )
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("llm_calls.id", ondelete="SET NULL")
    )
    call_id: Mapped[str] = mapped_column(Text)  # the provider's id, echoed in the tool message
    name: Mapped[str] = mapped_column(Text)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()
