"""The web-chat channel, used by the simulator.

Inbound goes through the same ``handle_inbound`` as SMS (D-013). Delivery is the browser reading
the message rows, so "sending" a queued reply just marks it sent once the page has it. Unlike
SMS there is no encoding cost here, which is why the output guard enforces GSM-7 on every
channel: the simulator must not hide what SMS would bill for (D-027).
"""

import uuid
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message
from app.domain.enums import MessageDirection, MessageStatus


async def deliver_queued(session: AsyncSession, conversation_id: uuid.UUID, now: datetime) -> int:
    result = await session.execute(
        update(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUT,
            Message.status == MessageStatus.QUEUED,
        )
        .values(status=MessageStatus.SENT, processed_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
