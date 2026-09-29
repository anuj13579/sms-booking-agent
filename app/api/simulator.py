"""The web-chat simulator: text the agent from a browser, and see what it did.

Development only. It lets anyone text the agent as any phone number and exposes the full agent
log, so it is off unless ``SIMULATOR_ENABLED`` is true and must never be reachable in a
deployment without authentication (Phase 6).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.runtime import Runtime, get_runtime
from app.channels.webchat import deliver_queued
from app.db.models import (
    AgentTurn,
    Booking,
    Business,
    Conversation,
    Customer,
    Escalation,
    LLMCall,
    Message,
    ServiceType,
    ToolCallRecord,
)
from app.domain.customers import InvalidPhoneError, normalize_phone
from app.domain.enums import Channel, EscalationStatus
from app.domain.escalation import resolve_escalation
from app.domain.localtime import window_label, zone
from app.pipeline.inbound import handle_inbound

router = APIRouter()
RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
PAGE = Path(__file__).resolve().parent / "simulator.html"


class PhoneBody(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        try:
            return normalize_phone(value)
        except InvalidPhoneError as exc:
            raise ValueError(str(exc)) from exc


class SendBody(PhoneBody):
    body: str = Field(min_length=1, max_length=1600)


async def _business(session: AsyncSession, runtime: Runtime) -> Business:
    business = await session.scalar(
        select(Business).where(Business.sms_number == runtime.settings.simulator_business_number)
    )
    if business is None:
        raise HTTPException(
            503,
            f"No business with SMS number {runtime.settings.simulator_business_number}. "
            "Run: uv run python -m app.seed",
        )
    return business


async def _conversation(
    session: AsyncSession, business: Business, phone: str
) -> tuple[Customer | None, Conversation | None]:
    customer = await session.scalar(
        select(Customer).where(Customer.business_id == business.id, Customer.phone_e164 == phone)
    )
    if customer is None:
        return None, None
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.customer_id == customer.id, Conversation.channel == Channel.WEBCHAT)
        .order_by(Conversation.started_at.desc(), Conversation.id)
        .limit(1)
    )
    return customer, conversation


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@router.get("/api/sim/info")
async def info(runtime: RuntimeDep) -> dict[str, Any]:
    async with runtime.sessionmaker() as session:
        business = await _business(session, runtime)
    return {
        "business": business.name,
        "owner": business.owner_name,
        "timezone": business.timezone,
        "model": runtime.agent_config.model,
        "prompt_version": runtime.prompt_version,
        "api_key_configured": runtime.api_key_configured,
    }


@router.post("/api/sim/messages")
async def send(body: SendBody, runtime: RuntimeDep) -> dict[str, Any]:
    async with runtime.sessionmaker() as session:
        business = await _business(session, runtime)
    outcome = await handle_inbound(
        runtime.sessionmaker,
        business.id,
        channel=Channel.WEBCHAT,
        from_number=body.phone,
        body=body.body,
        agent=runtime.agent(),
        notifier=runtime.notifier,
        clock=runtime.clock,
    )
    if outcome is None:
        return {"handled_by": "duplicate", "replies": []}
    async with runtime.sessionmaker() as session, session.begin():
        await deliver_queued(session, outcome.conversation_id, runtime.clock.now())
    return {
        "conversation_id": str(outcome.conversation_id),
        "handled_by": outcome.handled_by.value,
        "replies": outcome.all_replies,
    }


@router.post("/api/sim/resolve")
async def resolve(body: PhoneBody, runtime: RuntimeDep) -> dict[str, Any]:
    """Play the owner: resolve every open escalation, which hands the chat back to the bot."""
    async with runtime.sessionmaker() as session, session.begin():
        business = await _business(session, runtime)
        _, conversation = await _conversation(session, business, body.phone)
        if conversation is None:
            raise HTTPException(404, "No conversation for that number yet.")
        open_ids = list(
            await session.scalars(
                select(Escalation.id).where(
                    Escalation.conversation_id == conversation.id,
                    Escalation.status != EscalationStatus.RESOLVED,
                )
            )
        )
        for escalation_id in open_ids:
            await resolve_escalation(session, escalation_id, runtime.clock.now())
        await session.refresh(conversation)
        return {"resolved": len(open_ids), "mode": conversation.mode}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _money(value: Decimal) -> str:
    return f"{value:.6f}"


@router.get("/api/sim/state")
async def state(phone: str, runtime: RuntimeDep) -> dict[str, Any]:
    try:
        phone = normalize_phone(phone)
    except InvalidPhoneError as exc:
        raise HTTPException(422, str(exc)) from exc
    async with runtime.sessionmaker() as session, session.begin():
        business = await _business(session, runtime)
        customer, conversation = await _conversation(session, business, phone)
        if customer is None or conversation is None:
            return {"phone": phone, "customer": None, "conversation": None}
        await deliver_queued(session, conversation.id, runtime.clock.now())
        tz = zone(business.timezone)

        messages = list(
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.seq)
            )
        )
        escalations = list(
            await session.scalars(
                select(Escalation)
                .where(Escalation.conversation_id == conversation.id)
                .order_by(Escalation.created_at, Escalation.id)
            )
        )
        bookings = (
            await session.execute(
                select(Booking, ServiceType.label)
                .join(ServiceType, ServiceType.id == Booking.service_type_id)
                .where(Booking.customer_id == customer.id)
                .order_by(func.lower(Booking.time_window))
            )
        ).all()
        turns = list(
            await session.scalars(
                select(AgentTurn)
                .where(AgentTurn.conversation_id == conversation.id)
                .order_by(AgentTurn.seq)
            )
        )
        turn_ids = [t.id for t in turns]
        llm_calls = list(
            await session.scalars(
                select(LLMCall)
                .where(LLMCall.turn_id.in_(turn_ids))
                .order_by(LLMCall.step, LLMCall.attempt)
            )
        )
        tool_calls = list(
            await session.scalars(
                select(ToolCallRecord)
                .where(ToolCallRecord.turn_id.in_(turn_ids))
                .order_by(ToolCallRecord.created_at, ToolCallRecord.id)
            )
        )

    calls_by_turn: dict[uuid.UUID, list[LLMCall]] = {}
    for c in llm_calls:
        calls_by_turn.setdefault(c.turn_id, []).append(c)
    tools_by_turn: dict[uuid.UUID, list[ToolCallRecord]] = {}
    for t in tool_calls:
        tools_by_turn.setdefault(t.turn_id, []).append(t)

    def turn_json(turn: AgentTurn) -> dict[str, Any]:
        calls = calls_by_turn.get(turn.id, [])
        return {
            "id": str(turn.id),
            "outcome": turn.outcome,
            "error": turn.error,
            "model": turn.model,
            "prompt_version": turn.prompt_version,
            "latency_ms": turn.latency_ms,
            "cost_usd": _money(sum((c.cost_usd for c in calls), Decimal(0))),
            "input_tokens": sum(c.input_tokens for c in calls),
            "cache_hit_tokens": sum(c.cache_hit_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
            "llm_calls": [
                {
                    "step": c.step,
                    "attempt": c.attempt,
                    "input_tokens": c.input_tokens,
                    "cache_hit_tokens": c.cache_hit_tokens,
                    "output_tokens": c.output_tokens,
                    "cost_usd": _money(c.cost_usd),
                    "latency_ms": c.latency_ms,
                    "finish_reason": c.finish_reason,
                    "error": c.error,
                }
                for c in calls
            ],
            "tool_calls": [
                {
                    "name": t.name,
                    "status": t.status,
                    "arguments": t.arguments,
                    "result": t.result,
                    "latency_ms": t.latency_ms,
                }
                for t in tools_by_turn.get(turn.id, [])
            ],
        }

    return {
        "phone": phone,
        "customer": {
            "name": customer.name,
            "address": customer.address,
            "zip": customer.zip,
            "opted_out": customer.opted_out_at is not None,
        },
        "conversation": {
            "id": str(conversation.id),
            "mode": conversation.mode,
            "started_at": _iso(conversation.started_at),
        },
        "messages": [
            {
                "seq": m.seq,
                "direction": m.direction,
                "author": m.author,
                "body": m.body,
                "status": m.status,
                "created_at": _iso(m.created_at),
                "agent_turn_id": str(m.agent_turn_id) if m.agent_turn_id else None,
            }
            for m in messages
        ],
        "escalations": [
            {
                "reason": e.reason,
                "hazard": e.hazard,
                "mode": e.mode,
                "status": e.status,
                "source": e.source,
                "summary": e.summary,
                "created_at": _iso(e.created_at),
            }
            for e in escalations
        ],
        "bookings": [
            {
                "ref": b.ref,
                "service": label,
                "window": window_label(b.time_window.lower, b.time_window.upper, tz),
                "status": b.status,
                "address": b.address,
            }
            for b, label in bookings
            if b.time_window.lower is not None and b.time_window.upper is not None
        ],
        "turns": [turn_json(t) for t in turns],
        "totals": {
            "cost_usd": _money(sum((c.cost_usd for c in llm_calls), Decimal(0))),
            "llm_calls": len(llm_calls),
            "input_tokens": sum(c.input_tokens for c in llm_calls),
            "cache_hit_tokens": sum(c.cache_hit_tokens for c in llm_calls),
            "output_tokens": sum(c.output_tokens for c in llm_calls),
        },
    }
