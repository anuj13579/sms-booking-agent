"""Conversation sessions and the message log.

* One open conversation per customer and channel (a partial unique index enforces it).
* A bot-mode conversation closes after 24 h idle and the next message opens a fresh one, so the
  model never sees stale offers from weeks ago (D-021).
* A conversation in **handoff** never auto-closes: a human owns it until the owner resolves the
  escalation. Otherwise a customer who waits a day for the owner would quietly land back with
  the bot (D-025).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message
from app.domain.enums import (
    Channel,
    ConversationMode,
    MessageAuthor,
    MessageDirection,
    MessageStatus,
)

SESSION_IDLE_TIMEOUT = timedelta(hours=24)


async def open_conversation(
    session: AsyncSession,
    business_id: uuid.UUID,
    customer_id: uuid.UUID,
    channel: Channel,
    now: datetime,
) -> Conversation:
    """Return the customer's current conversation on this channel, starting a new one if there
    is none or the current one has been idle for 24 h (bot mode only)."""
    current = await _current(session, customer_id, channel, lock=True)
    if current is not None:
        stale = now - current.last_activity_at >= SESSION_IDLE_TIMEOUT
        if not (stale and current.mode == ConversationMode.BOT):
            return current
        current.mode = ConversationMode.CLOSED
        await session.flush()

    await session.execute(
        insert(Conversation)
        .values(
            id=uuid.uuid4(),
            business_id=business_id,
            customer_id=customer_id,
            channel=channel,
            mode=ConversationMode.BOT,
            started_at=now,
            last_activity_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["customer_id", "channel"], index_where=text("mode <> 'closed'")
        )
    )
    opened = await _current(session, customer_id, channel, lock=False)
    assert opened is not None
    return opened


async def _current(
    session: AsyncSession, customer_id: uuid.UUID, channel: Channel, *, lock: bool
) -> Conversation | None:
    stmt = select(Conversation).where(
        Conversation.customer_id == customer_id,
        Conversation.channel == channel,
        Conversation.mode != ConversationMode.CLOSED,
    )
    if lock:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt.execution_options(populate_existing=True))


def touch(conversation: Conversation, now: datetime) -> None:
    conversation.last_activity_at = max(conversation.last_activity_at, now)


async def is_duplicate(session: AsyncSession, provider_sid: str | None) -> bool:
    if provider_sid is None:
        return False
    found = await session.scalar(select(Message.id).where(Message.provider_sid == provider_sid))
    return found is not None


async def add_inbound(
    session: AsyncSession,
    conversation: Conversation,
    body: str,
    now: datetime,
    provider_sid: str | None = None,
) -> Message | None:
    """Store a customer message for processing. Returns ``None`` if ``provider_sid`` was already
    stored: a webhook retry is a no-op, decided by the unique index, not by a prior SELECT."""
    message_id = await session.scalar(
        insert(Message)
        .values(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            direction=MessageDirection.IN,
            author=MessageAuthor.CUSTOMER,
            body=body,
            provider_sid=provider_sid,
            status=MessageStatus.RECEIVED,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["provider_sid"])
        .returning(Message.id)
    )
    if message_id is None:
        return None
    touch(conversation, now)
    return await session.get(Message, message_id)


async def add_outbound(
    session: AsyncSession,
    conversation: Conversation,
    body: str,
    author: MessageAuthor,
    now: datetime,
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        direction=MessageDirection.OUT,
        author=author,
        body=body,
        status=MessageStatus.QUEUED,
        created_at=now,
    )
    session.add(message)
    touch(conversation, now)
    await session.flush()
    return message


async def pending_inbound(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    result = await session.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
            Message.status == MessageStatus.RECEIVED,
        )
        .order_by(Message.seq)
    )
    return list(result)


async def mark_inbound(
    session: AsyncSession, message_ids: Sequence[uuid.UUID], status: MessageStatus, now: datetime
) -> None:
    if not message_ids:
        return
    await session.execute(
        update(Message)
        .where(Message.id.in_(message_ids), Message.status == MessageStatus.RECEIVED)
        .values(status=status, processed_at=now)
        .execution_options(synchronize_session=False)
    )


async def transcript(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    result = await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return list(result)
