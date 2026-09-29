from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation
from app.domain.conversations import open_conversation
from app.domain.enums import (
    Channel,
    ConversationMode,
    EscalationMode,
    EscalationReason,
    EscalationSource,
    EscalationStatus,
    Hazard,
)
from app.domain.escalation import (
    ESCALATION_POLICY,
    acknowledge_escalation,
    escalate,
    resolve_escalation,
)
from app.domain.templates import FALLBACK_MESSAGE, handoff_message, safety_message
from tests.factories import World, add_customer, build_world, local

NOW = local(date(2026, 10, 5), 9)


@pytest.fixture
async def world(session: AsyncSession) -> World:
    world = await build_world(session)
    await add_customer(session, world, "+15550000001", "pat")
    return world


@pytest.fixture
async def conversation(session: AsyncSession, world: World) -> Conversation:
    return await open_conversation(
        session, world.business.id, world.customers["pat"].id, Channel.SMS, NOW
    )


def test_policy_covers_every_reason() -> None:
    assert set(ESCALATION_POLICY) == set(EscalationReason)


@pytest.mark.parametrize("reason", list(EscalationReason))
async def test_policy_decides_mode_and_customer_text(
    session: AsyncSession, world: World, conversation: Conversation, reason: EscalationReason
) -> None:
    result = await escalate(
        session,
        world.business,
        conversation,
        reason=reason,
        summary="s",
        source=EscalationSource.AGENT,
        now=NOW,
        hazard=Hazard.GAS if reason is EscalationReason.EMERGENCY else None,
    )

    assert result.created
    assert result.mode is ESCALATION_POLICY[reason]
    if result.mode is EscalationMode.HANDOFF:
        assert conversation.mode == ConversationMode.HANDOFF
        expected = {
            EscalationReason.EMERGENCY: safety_message(Hazard.GAS, "Dana"),
            EscalationReason.SYSTEM_ERROR: FALLBACK_MESSAGE,
        }.get(reason, handoff_message("Dana"))
        assert result.customer_message == expected
    else:
        assert conversation.mode == ConversationMode.BOT  # bot keeps going
        assert result.customer_message is None


async def test_emergency_without_hazard_gets_the_generic_template(
    session: AsyncSession, world: World, conversation: Conversation
) -> None:
    result = await escalate(
        session,
        world.business,
        conversation,
        reason=EscalationReason.EMERGENCY,
        summary="outlet by the crib is hot",
        source=EscalationSource.AGENT,
        now=NOW,
    )
    assert result.escalation.hazard == Hazard.OTHER
    assert result.customer_message == safety_message(Hazard.OTHER, "Dana")


async def test_hazard_is_dropped_for_non_emergencies(
    session: AsyncSession, world: World, conversation: Conversation
) -> None:
    result = await escalate(
        session,
        world.business,
        conversation,
        reason=EscalationReason.PRICE_QUOTE,
        summary="how much",
        source=EscalationSource.AGENT,
        now=NOW,
        hazard=Hazard.GAS,
    )
    assert result.escalation.hazard is None


async def test_repeat_escalation_is_not_duplicated(
    session: AsyncSession, world: World, conversation: Conversation
) -> None:
    kwargs = {
        "reason": EscalationReason.EMERGENCY,
        "source": EscalationSource.PREFILTER,
        "now": NOW,
    }
    first = await escalate(
        session, world.business, conversation, summary="gas", hazard=Hazard.GAS, **kwargs
    )
    again = await escalate(
        session, world.business, conversation, summary="still gas", hazard=Hazard.GAS, **kwargs
    )
    other = await escalate(
        session, world.business, conversation, summary="and CO", hazard=Hazard.CO, **kwargs
    )

    assert first.created and not again.created and other.created
    assert again.escalation.id == first.escalation.id
    assert again.customer_message is None  # don't resend the template
    assert other.customer_message == safety_message(Hazard.CO, "Dana")


async def test_conversation_returns_to_bot_when_last_handoff_is_resolved(
    session: AsyncSession, world: World, conversation: Conversation
) -> None:
    upset = await escalate(
        session,
        world.business,
        conversation,
        reason=EscalationReason.UPSET_CUSTOMER,
        summary="angry",
        source=EscalationSource.AGENT,
        now=NOW,
    )
    human = await escalate(
        session,
        world.business,
        conversation,
        reason=EscalationReason.HUMAN_REQUESTED,
        summary="wants a person",
        source=EscalationSource.AGENT,
        now=NOW,
    )
    price = await escalate(
        session,
        world.business,
        conversation,
        reason=EscalationReason.PRICE_QUOTE,
        summary="price",
        source=EscalationSource.AGENT,
        now=NOW,
    )

    await acknowledge_escalation(session, upset.escalation.id)
    assert upset.escalation.status == EscalationStatus.ACKNOWLEDGED

    await resolve_escalation(session, upset.escalation.id, NOW)
    assert conversation.mode == ConversationMode.HANDOFF  # "human_requested" still open

    await resolve_escalation(session, human.escalation.id, NOW)
    assert conversation.mode == ConversationMode.BOT  # an open notify-mode one doesn't count
    assert price.escalation.status == EscalationStatus.OPEN
