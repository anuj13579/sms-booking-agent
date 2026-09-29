"""Phase 1 walkthrough against your local database, with no LLM involved.

    docker compose up -d
    uv run alembic upgrade head
    uv run python scripts/demo.py

Shows availability, a booking with its race-safe technician assignment, and the deterministic
pipeline answering an emergency and an opt-out. The booking is rolled back; the pipeline part
leaves a demo conversation behind under a random 555 number.
"""

import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import make_engine, make_sessionmaker
from app.domain.availability import find_available_slots, get_service, load_calendar
from app.domain.booking import BookingOk, NewBooking, create_booking
from app.domain.clock import SystemClock
from app.domain.customers import get_or_create_customer
from app.domain.enums import Channel
from app.domain.notifications import RecordingNotifier
from app.pipeline.inbound import AgentTurnInput, AgentTurnOutput, handle_inbound
from app.seed import seed_brightwater


class NoAgentYet:
    async def run_turn(self, turn: AgentTurnInput) -> AgentTurnOutput:
        return AgentTurnOutput("(Phase 2: the agent would answer here)")


async def main() -> None:
    engine = make_engine()
    sessionmaker = make_sessionmaker(engine)
    clock = SystemClock()

    async with sessionmaker() as session, session.begin():
        business = await seed_brightwater(session)
    print(f"{business.name}, {business.timezone}\n")

    async with sessionmaker() as session, session.begin():
        cal = await load_calendar(session, business)
        service = await get_service(session, business.id, "ac_repair")
        assert service is not None
        slots = await find_available_slots(session, cal, service, clock.now())
        print("Next AC repair windows:")
        for slot in slots:
            print(f"  {slot.label:26s} slot_id={slot.slot_id}")

        customer = await get_or_create_customer(session, business.id, "(555) 010-0123")
        result = await create_booking(
            session,
            business,
            customer,
            NewBooking(
                service_code="ac_repair",
                slot_start=slots[0].start,
                customer_name="Demo Customer",
                address="1 Demo St, Hoboken NJ",
                zip="07030",
                problem_description="AC blows warm",
            ),
            clock,
        )
        assert isinstance(result, BookingOk)
        print(f"\nBooked {result.booking.slot.label}, ref {result.booking.ref} ({result.outcome})")
        await session.rollback()
        print("(booking rolled back)\n")

    phone = f"+1555010{random.randint(1000, 9999)}"
    notifier = RecordingNotifier()
    for text in ["Hi, can someone look at my AC this week?", "I smell gas in the kitchen", "STOP"]:
        outcome = await handle_inbound(
            sessionmaker,
            business.id,
            channel=Channel.WEBCHAT,
            from_number=phone,
            body=text,
            agent=NoAgentYet(),
            notifier=notifier,
            clock=clock,
        )
        assert outcome is not None
        print(f"Customer: {text}")
        print(f"  handled by: {outcome.handled_by}")
        for reply in outcome.replies:
            print(f"  reply: {reply}")
    print(f"\nOwner alerts: {[a.kind.value for a in notifier.alerts]}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
