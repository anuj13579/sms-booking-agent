"""The work queue is the ``messages`` table itself (D-012), with a per-conversation lease (D-024).

A conversation needs work when it has inbound messages in status ``received``. A worker claims
it by writing a lease (token + expiry) in a short transaction, then processes it **without**
holding a transaction open, because an agent turn can spend up to 45 s waiting on the LLM. At
the end the worker releases the lease. If the worker dies, the lease simply expires and another
worker picks the conversation up; its messages are still ``received``.

* ``FOR UPDATE SKIP LOCKED`` lets several workers poll at once without blocking each other.
* The lease makes processing serial per conversation: two workers never answer the same thread.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_CLAIM_NEXT = text(
    """
    SELECT c.id
    FROM messages m
    JOIN conversations c ON c.id = m.conversation_id
    WHERE m.status = 'received'
      AND (c.lease_until IS NULL OR c.lease_until <= :now)
    ORDER BY m.seq
    LIMIT 1
    FOR UPDATE OF c SKIP LOCKED
    """
)

_TAKE_LEASE = text(
    """
    UPDATE conversations
    SET lease_token = :token, lease_until = :until
    WHERE id = :id AND (lease_until IS NULL OR lease_until <= :now)
    RETURNING lease_token
    """
)

_RELEASE = text(
    """
    UPDATE conversations SET lease_token = NULL, lease_until = NULL
    WHERE id = :id AND lease_token = :token
    """
)


async def claim_next(
    session: AsyncSession, now: datetime, lease: timedelta
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Claim the conversation holding the oldest unprocessed message. Returns
    ``(conversation_id, lease_token)``, or None when there is nothing to do."""
    conversation_id = await session.scalar(_CLAIM_NEXT, {"now": now})
    if conversation_id is None:
        return None
    token = await claim(session, conversation_id, now, lease)
    assert token is not None  # we hold the row lock, and the WHERE above saw the lease free
    return conversation_id, token


async def claim(
    session: AsyncSession, conversation_id: uuid.UUID, now: datetime, lease: timedelta
) -> uuid.UUID | None:
    """Claim one specific conversation. None if another worker holds a live lease."""
    token: uuid.UUID | None = await session.scalar(
        _TAKE_LEASE,
        {"id": conversation_id, "token": uuid.uuid4(), "until": now + lease, "now": now},
    )
    return token


async def release(session: AsyncSession, conversation_id: uuid.UUID, token: uuid.UUID) -> bool:
    """Give the lease back. False if it had already expired and someone else took it."""
    result = await session.execute(_RELEASE, {"id": conversation_id, "token": token})
    return bool(result.rowcount)  # type: ignore[attr-defined]
