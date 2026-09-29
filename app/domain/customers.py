import re
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


class InvalidPhoneError(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    """Normalise to E.164. Bare 10-digit and 1+10-digit numbers are treated as US/Canada (NANP),
    the only market this product serves. Anything else must already carry a ``+`` prefix."""
    text = raw.strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        candidate = f"+{digits}"
    elif len(digits) == 10:
        candidate = f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = f"+{digits}"
    else:
        raise InvalidPhoneError(f"not a recognisable phone number: {raw!r}")
    if not _E164.match(candidate):
        raise InvalidPhoneError(f"not a recognisable phone number: {raw!r}")
    return candidate


async def get_or_create_customer(
    session: AsyncSession, business_id: uuid.UUID, phone: str
) -> Customer:
    """Race-safe: two first messages from a new number at the same instant yield one row."""
    phone_e164 = normalize_phone(phone)
    await session.execute(
        insert(Customer)
        .values(id=uuid.uuid4(), business_id=business_id, phone_e164=phone_e164)
        .on_conflict_do_nothing(index_elements=["business_id", "phone_e164"])
    )
    customer = await session.scalar(
        select(Customer).where(
            Customer.business_id == business_id, Customer.phone_e164 == phone_e164
        )
    )
    assert customer is not None
    return customer


async def set_opted_out(session: AsyncSession, customer: Customer, at: datetime) -> bool:
    """Returns True if this changed the customer's state."""
    if customer.opted_out_at is not None:
        return False
    customer.opted_out_at = at
    await session.flush()
    return True


async def set_opted_in(session: AsyncSession, customer: Customer) -> bool:
    if customer.opted_out_at is None:
        return False
    customer.opted_out_at = None
    await session.flush()
    return True
