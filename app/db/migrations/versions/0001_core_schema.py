"""Core schema: business setup, customers, bookings, conversations, messages, escalations.

The agent's logging tables (agent_turns, llm_calls, tool_calls) arrive with the agent loop in
Phase 2.

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

E164 = r"^\+[1-9][0-9]{6,14}$"


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _finite(col: str) -> str:
    return f"NOT isempty({col}) AND NOT lower_inf({col}) AND NOT upper_inf({col})"


def upgrade() -> None:
    # btree_gist lets a GiST index combine "=" on a UUID with "&&" on a range: the exclusion
    # constraint on bookings needs it. It is a trusted extension, so the DB owner can create it.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "businesses",
        _id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("sms_number", sa.Text(), nullable=False),
        sa.Column("owner_name", sa.Text(), nullable=False),
        sa.Column("owner_phone", sa.Text(), nullable=False),
        sa.Column("min_lead_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column(
            "service_area_zips",
            pg.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        _created_at(),
        sa.UniqueConstraint("sms_number", name="uq_businesses_sms_number"),
        sa.CheckConstraint(f"sms_number ~ '{E164}'", name=op.f("ck_businesses_sms_number_e164")),
        sa.CheckConstraint(f"owner_phone ~ '{E164}'", name=op.f("ck_businesses_owner_phone_e164")),
        sa.CheckConstraint(
            "min_lead_minutes >= 0", name=op.f("ck_businesses_min_lead_nonnegative")
        ),
        sa.CheckConstraint(
            "horizon_days BETWEEN 1 AND 60", name=op.f("ck_businesses_horizon_days_range")
        ),
    )

    op.create_table(
        "service_types",
        _id(),
        sa.Column(
            "business_id",
            sa.UUID(),
            sa.ForeignKey(
                "businesses.id", ondelete="CASCADE", name="fk_service_types_business_id_businesses"
            ),
            nullable=False,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("trade", sa.Text(), nullable=False),
        sa.Column("bookable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("business_id", "code", name="uq_service_types_business_id_code"),
        sa.CheckConstraint(
            "trade IN ('hvac', 'plumbing', 'electrical')", name=op.f("ck_service_types_trade_valid")
        ),
        sa.CheckConstraint("code ~ '^[a-z][a-z0-9_]*$'", name=op.f("ck_service_types_code_format")),
    )
    op.create_index("ix_service_types_business_id", "service_types", ["business_id"])

    op.create_table(
        "window_templates",
        _id(),
        sa.Column(
            "business_id",
            sa.UUID(),
            sa.ForeignKey(
                "businesses.id",
                ondelete="CASCADE",
                name="fk_window_templates_business_id_businesses",
            ),
            nullable=False,
        ),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("start_local", sa.Time(), nullable=False),
        sa.Column("end_local", sa.Time(), nullable=False),
        sa.UniqueConstraint(
            "business_id",
            "weekday",
            "start_local",
            name="uq_window_templates_business_id_weekday_start_local",
        ),
        sa.CheckConstraint(
            "weekday BETWEEN 0 AND 6", name=op.f("ck_window_templates_weekday_range")
        ),
        sa.CheckConstraint(
            "start_local < end_local", name=op.f("ck_window_templates_start_before_end")
        ),
    )
    op.create_index("ix_window_templates_business_id", "window_templates", ["business_id"])

    op.create_table(
        "technicians",
        _id(),
        sa.Column(
            "business_id",
            sa.UUID(),
            sa.ForeignKey(
                "businesses.id", ondelete="CASCADE", name="fk_technicians_business_id_businesses"
            ),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("trades", pg.ARRAY(sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint(
            "trades <@ ARRAY['hvac', 'plumbing', 'electrical']::text[] AND cardinality(trades) > 0",
            name=op.f("ck_technicians_trades_valid"),
        ),
    )
    op.create_index("ix_technicians_business_id", "technicians", ["business_id"])

    op.create_table(
        "technician_time_off",
        _id(),
        sa.Column(
            "technician_id",
            sa.UUID(),
            sa.ForeignKey(
                "technicians.id",
                ondelete="CASCADE",
                name="fk_technician_time_off_technician_id_technicians",
            ),
            nullable=False,
        ),
        sa.Column("period", pg.TSTZRANGE(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(_finite("period"), name=op.f("ck_technician_time_off_period_finite")),
    )
    op.create_index(
        "ix_technician_time_off_technician_id", "technician_time_off", ["technician_id"]
    )

    op.create_table(
        "customers",
        _id(),
        sa.Column(
            "business_id",
            sa.UUID(),
            sa.ForeignKey(
                "businesses.id", ondelete="CASCADE", name="fk_customers_business_id_businesses"
            ),
            nullable=False,
        ),
        sa.Column("phone_e164", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("zip", sa.Text(), nullable=True),
        sa.Column("opted_out_at", sa.TIMESTAMP(timezone=True), nullable=True),
        _created_at(),
        sa.UniqueConstraint(
            "business_id", "phone_e164", name="uq_customers_business_id_phone_e164"
        ),
        sa.CheckConstraint(f"phone_e164 ~ '{E164}'", name=op.f("ck_customers_phone_e164_format")),
    )

    op.create_table(
        "bookings",
        _id(),
        sa.Column(
            "business_id",
            sa.UUID(),
            sa.ForeignKey(
                "businesses.id", ondelete="CASCADE", name="fk_bookings_business_id_businesses"
            ),
            nullable=False,
        ),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column(
            "customer_id",
            sa.UUID(),
            sa.ForeignKey("customers.id", name="fk_bookings_customer_id_customers"),
            nullable=False,
        ),
        sa.Column(
            "technician_id",
            sa.UUID(),
            sa.ForeignKey("technicians.id", name="fk_bookings_technician_id_technicians"),
            nullable=False,
        ),
        sa.Column(
            "service_type_id",
            sa.UUID(),
            sa.ForeignKey("service_types.id", name="fk_bookings_service_type_id_service_types"),
            nullable=False,
        ),
        sa.Column("time_window", pg.TSTZRANGE(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'confirmed'")),
        sa.Column("urgency", sa.Text(), nullable=False, server_default=sa.text("'routine'")),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("zip", sa.Text(), nullable=False),
        sa.Column("problem_description", sa.Text(), nullable=False),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("business_id", "ref", name="uq_bookings_business_id_ref"),
        sa.CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'completed')",
            name=op.f("ck_bookings_status_valid"),
        ),
        sa.CheckConstraint(
            "urgency IN ('routine', 'urgent')", name=op.f("ck_bookings_urgency_valid")
        ),
        sa.CheckConstraint(_finite("time_window"), name=op.f("ck_bookings_time_window_finite")),
    )
    op.create_index("ix_bookings_customer_id", "bookings", ["customer_id"])
    # The core guarantee (D-006): no technician ever has two overlapping confirmed bookings.
    op.execute(
        """
        ALTER TABLE bookings ADD CONSTRAINT ex_bookings_technician_no_overlap
        EXCLUDE USING gist (technician_id WITH =, time_window WITH &&)
        WHERE (status = 'confirmed')
        """
    )
    # Idempotency by natural key: a retried create_booking finds the existing row.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_bookings_customer_service_start
        ON bookings (customer_id, service_type_id, lower(time_window))
        WHERE status = 'confirmed'
        """
    )

    op.create_table(
        "booking_events",
        _id(),
        sa.Column(
            "booking_id",
            sa.UUID(),
            sa.ForeignKey(
                "bookings.id", ondelete="CASCADE", name="fk_booking_events_booking_id_bookings"
            ),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("old_window", pg.TSTZRANGE(), nullable=True),
        sa.Column("new_window", pg.TSTZRANGE(), nullable=True),
        sa.Column("old_technician_id", sa.UUID(), nullable=True),
        sa.Column("new_technician_id", sa.UUID(), nullable=True),
        sa.Column("detail", pg.JSONB(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "event_type IN ('created', 'rescheduled', 'cancelled', 'completed')",
            name=op.f("ck_booking_events_event_type_valid"),
        ),
        sa.CheckConstraint(
            "actor IN ('agent', 'owner', 'system')", name=op.f("ck_booking_events_actor_valid")
        ),
    )
    op.create_index("ix_booking_events_booking_id", "booking_events", ["booking_id"])

    op.create_table(
        "conversations",
        _id(),
        sa.Column(
            "business_id",
            sa.UUID(),
            sa.ForeignKey(
                "businesses.id", ondelete="CASCADE", name="fk_conversations_business_id_businesses"
            ),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.UUID(),
            sa.ForeignKey("customers.id", name="fk_conversations_customer_id_customers"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False, server_default=sa.text("'bot'")),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('sms', 'webchat', 'eval')", name=op.f("ck_conversations_channel_valid")
        ),
        sa.CheckConstraint(
            "mode IN ('bot', 'handoff', 'closed')", name=op.f("ck_conversations_mode_valid")
        ),
    )
    op.create_index("ix_conversations_business_id", "conversations", ["business_id"])
    op.create_index(
        "uq_conversations_open_per_channel",
        "conversations",
        ["customer_id", "channel"],
        unique=True,
        postgresql_where=sa.text("mode <> 'closed'"),
    )

    op.create_table(
        "messages",
        _id(),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "conversation_id",
            sa.UUID(),
            sa.ForeignKey(
                "conversations.id",
                ondelete="CASCADE",
                name="fk_messages_conversation_id_conversations",
            ),
            nullable=False,
        ),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider_sid", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("seq", name="uq_messages_seq"),
        sa.UniqueConstraint("provider_sid", name="uq_messages_provider_sid"),
        sa.CheckConstraint("direction IN ('in', 'out')", name=op.f("ck_messages_direction_valid")),
        sa.CheckConstraint(
            "author IN ('customer', 'agent', 'owner', 'system')",
            name=op.f("ck_messages_author_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'skipped', 'queued', 'sent', 'failed')",
            name=op.f("ck_messages_status_valid"),
        ),
        sa.CheckConstraint(
            "(direction = 'in') = (author = 'customer')",
            name=op.f("ck_messages_author_matches_direction"),
        ),
        sa.CheckConstraint(
            "(direction = 'in' AND status IN ('received', 'processed', 'skipped'))"
            " OR (direction = 'out' AND status IN ('queued', 'sent', 'failed'))",
            name=op.f("ck_messages_status_matches_direction"),
        ),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index(
        "ix_messages_pending",
        "messages",
        ["conversation_id"],
        postgresql_where=sa.text("status = 'received'"),
    )

    op.create_table(
        "escalations",
        _id(),
        sa.Column(
            "conversation_id",
            sa.UUID(),
            sa.ForeignKey(
                "conversations.id",
                ondelete="CASCADE",
                name="fk_escalations_conversation_id_conversations",
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("hazard", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reason IN ('emergency', 'upset_customer', 'human_requested', 'uncertain',"
            " 'out_of_scope', 'price_quote', 'urgent_no_availability', 'system_error')",
            name=op.f("ck_escalations_reason_valid"),
        ),
        sa.CheckConstraint(
            "hazard IS NULL OR hazard IN ('gas', 'co', 'electrical', 'flooding', 'no_heat', 'other')",
            name=op.f("ck_escalations_hazard_valid"),
        ),
        sa.CheckConstraint("mode IN ('handoff', 'notify')", name=op.f("ck_escalations_mode_valid")),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')",
            name=op.f("ck_escalations_status_valid"),
        ),
        sa.CheckConstraint(
            "source IN ('prefilter', 'agent', 'system')", name=op.f("ck_escalations_source_valid")
        ),
        sa.CheckConstraint(
            "(reason = 'emergency') = (hazard IS NOT NULL)",
            name=op.f("ck_escalations_hazard_iff_emergency"),
        ),
    )
    op.create_index(
        "ix_escalations_unresolved",
        "escalations",
        ["conversation_id"],
        postgresql_where=sa.text("status <> 'resolved'"),
    )


def downgrade() -> None:
    for table in (
        "escalations",
        "messages",
        "conversations",
        "booking_events",
        "bookings",
        "customers",
        "technician_time_off",
        "technicians",
        "window_templates",
        "service_types",
        "businesses",
    ):
        op.drop_table(table)
    # btree_gist is left installed: other objects in the database may use it.
