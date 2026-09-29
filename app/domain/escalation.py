"""Escalation to the business owner (D-011).

The caller (the emergency pre-filter, or later the LLM via ``escalate_to_owner``) only says *why*.
This module decides what happens: the policy table maps each reason to a mode, and the mode
decides whether the bot keeps talking. The owner can change the policy without touching prompts.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business, Conversation, Escalation
from app.domain.enums import (
    ConversationMode,
    EscalationMode,
    EscalationReason,
    EscalationSource,
    EscalationStatus,
    Hazard,
)
from app.domain.templates import FALLBACK_MESSAGE, handoff_message, safety_message

ESCALATION_POLICY: Mapping[EscalationReason, EscalationMode] = MappingProxyType(
    {
        EscalationReason.EMERGENCY: EscalationMode.HANDOFF,
        EscalationReason.UPSET_CUSTOMER: EscalationMode.HANDOFF,
        EscalationReason.HUMAN_REQUESTED: EscalationMode.HANDOFF,
        EscalationReason.UNCERTAIN: EscalationMode.HANDOFF,
        EscalationReason.OUT_OF_SCOPE: EscalationMode.HANDOFF,
        EscalationReason.SYSTEM_ERROR: EscalationMode.HANDOFF,
        EscalationReason.PRICE_QUOTE: EscalationMode.NOTIFY,
        EscalationReason.URGENT_NO_AVAILABILITY: EscalationMode.NOTIFY,
    }
)

_UNRESOLVED = (EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED)


@dataclass(frozen=True)
class EscalationResult:
    escalation: Escalation
    created: bool  # False: an identical unresolved escalation already existed; nothing new done
    mode: EscalationMode
    # Fixed text for the customer, or None when the bot carries on (notify mode) or when this
    # was a repeat of an escalation the customer has already been answered for.
    customer_message: str | None


def _customer_message(
    business: Business, reason: EscalationReason, hazard: Hazard | None
) -> str | None:
    if reason is EscalationReason.EMERGENCY:
        assert hazard is not None
        return safety_message(hazard, business.owner_name)
    if reason is EscalationReason.SYSTEM_ERROR:
        return FALLBACK_MESSAGE
    if ESCALATION_POLICY[reason] is EscalationMode.HANDOFF:
        return handoff_message(business.owner_name)
    return None


async def escalate(
    session: AsyncSession,
    business: Business,
    conversation: Conversation,
    *,
    reason: EscalationReason,
    summary: str,
    source: EscalationSource,
    now: datetime,
    hazard: Hazard | None = None,
) -> EscalationResult:
    if reason is EscalationReason.EMERGENCY:
        hazard = hazard or Hazard.OTHER
    else:
        hazard = None
    mode = ESCALATION_POLICY[reason]

    # One unresolved escalation per (reason, hazard) per conversation: a customer who keeps
    # texting about the same gas smell doesn't re-trigger the template or re-page the owner.
    # A *different* hazard is a new escalation.
    existing = await session.scalar(
        select(Escalation).where(
            Escalation.conversation_id == conversation.id,
            Escalation.reason == reason,
            Escalation.hazard.is_(None) if hazard is None else Escalation.hazard == hazard,
            Escalation.status.in_(_UNRESOLVED),
        )
    )
    if existing is not None:
        return EscalationResult(existing, created=False, mode=mode, customer_message=None)

    escalation = Escalation(
        conversation_id=conversation.id,
        reason=reason,
        hazard=hazard,
        mode=mode,
        status=EscalationStatus.OPEN,
        source=source,
        summary=summary.strip()[:2000],
        created_at=now,
    )
    session.add(escalation)
    if mode is EscalationMode.HANDOFF:
        conversation.mode = ConversationMode.HANDOFF
    await session.flush()
    return EscalationResult(
        escalation,
        created=True,
        mode=mode,
        customer_message=_customer_message(business, reason, hazard),
    )


async def acknowledge_escalation(session: AsyncSession, escalation_id: uuid.UUID) -> Escalation:
    escalation = await session.get(Escalation, escalation_id, with_for_update=True)
    if escalation is None:
        raise LookupError(f"no escalation {escalation_id}")
    if escalation.status == EscalationStatus.OPEN:
        escalation.status = EscalationStatus.ACKNOWLEDGED
        await session.flush()
    return escalation


async def resolve_escalation(
    session: AsyncSession, escalation_id: uuid.UUID, now: datetime
) -> Escalation:
    """Owner marks an escalation handled. When no handoff-mode escalation remains unresolved,
    the conversation goes back to the bot."""
    escalation = await session.get(Escalation, escalation_id, with_for_update=True)
    if escalation is None:
        raise LookupError(f"no escalation {escalation_id}")
    if escalation.status != EscalationStatus.RESOLVED:
        escalation.status = EscalationStatus.RESOLVED
        escalation.resolved_at = now
        await session.flush()

    conversation = await session.get(Conversation, escalation.conversation_id, with_for_update=True)
    assert conversation is not None
    still_handed_off = await session.scalar(
        select(Escalation.id)
        .where(
            Escalation.conversation_id == conversation.id,
            Escalation.mode == EscalationMode.HANDOFF,
            Escalation.status.in_(_UNRESOLVED),
        )
        .limit(1)
    )
    if conversation.mode == ConversationMode.HANDOFF and still_handed_off is None:
        conversation.mode = ConversationMode.BOT
        await session.flush()
    return escalation


async def unresolved_escalations(
    session: AsyncSession, conversation_id: uuid.UUID
) -> list[Escalation]:
    result = await session.scalars(
        select(Escalation)
        .where(Escalation.conversation_id == conversation_id, Escalation.status.in_(_UNRESOLVED))
        .order_by(Escalation.created_at)
    )
    return list(result)
