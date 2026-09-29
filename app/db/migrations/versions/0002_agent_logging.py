"""Agent logging: agent_turns, llm_calls, tool_calls, and messages.agent_turn_id.

These tables are the agent's audit log and the source of the eval harness's cost and latency
numbers (DESIGN section 9). ``agent_turns.messages`` also holds each turn's slice of the model's
transcript, which the next turn replays unchanged so the prompt stays append-only (D-015).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOMES = (
    "'replied', 'handed_off', 'guard_failed', 'step_limit', 'time_limit', 'llm_error', 'error'"
)
TOOL_STATUSES = "'ok', 'error', 'invalid', 'skipped', 'exception'"


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _fk(
    column: str, target: str, table: str, ondelete: str, nullable: bool = False
) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.UUID(),
        sa.ForeignKey(f"{target}.id", ondelete=ondelete, name=f"fk_{table}_{column}_{target}"),
        nullable=nullable,
    )


def _zero(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text("0"))


def upgrade() -> None:
    op.create_table(
        "agent_turns",
        _id(),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        _fk("conversation_id", "conversations", "agent_turns", "CASCADE"),
        sa.Column("trigger_message_ids", pg.ARRAY(sa.UUID()), nullable=False),
        sa.Column("max_message_seq", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("messages", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.UniqueConstraint("seq", name="uq_agent_turns_seq"),
        sa.CheckConstraint(
            f"outcome IS NULL OR outcome IN ({OUTCOMES})", name=op.f("ck_agent_turns_outcome_valid")
        ),
    )
    op.create_index("ix_agent_turns_conversation_id", "agent_turns", ["conversation_id"])

    op.create_table(
        "llm_calls",
        _id(),
        _fk("turn_id", "agent_turns", "llm_calls", "CASCADE"),
        sa.Column("step", sa.SmallInteger(), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        _zero("input_tokens"),
        _zero("cache_hit_tokens"),
        _zero("output_tokens"),
        _zero("reasoning_tokens"),
        sa.Column("cost_usd", sa.Numeric(14, 10), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("finish_reason", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_calls", pg.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _created_at(),
    )
    op.create_index("ix_llm_calls_turn_id", "llm_calls", ["turn_id"])

    op.create_table(
        "tool_calls",
        _id(),
        _fk("turn_id", "agent_turns", "tool_calls", "CASCADE"),
        _fk("llm_call_id", "llm_calls", "tool_calls", "SET NULL", nullable=True),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("arguments", pg.JSONB(), nullable=False),
        sa.Column("result", pg.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint(f"status IN ({TOOL_STATUSES})", name=op.f("ck_tool_calls_status_valid")),
    )
    op.create_index("ix_tool_calls_turn_id", "tool_calls", ["turn_id"])

    op.add_column("messages", _fk("agent_turn_id", "agent_turns", "messages", "SET NULL", True))
    op.create_index("ix_messages_agent_turn_id", "messages", ["agent_turn_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_agent_turn_id", table_name="messages")
    op.drop_column("messages", "agent_turn_id")
    for table in ("tool_calls", "llm_calls", "agent_turns"):
        op.drop_table(table)
