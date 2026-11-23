"""The eval runner end to end against real Postgres, with a scripted FakeProvider standing in for
the model (and for the simulated customer). This is the harness testing itself at $0: the
pipeline, tools, domain, fixtures, trace collection, and grading are all real."""

import json
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.loop import AgentConfig
from app.agent.providers.base import LLMResult
from app.agent.providers.fake import FakeProvider, FakeRequest, reply, tool
from app.db.models import Booking, Business
from evals.graders import grade
from evals.runner import EvalEnv, run_scenario
from evals.scenario import Scenario

CONFIG = AgentConfig(model="deepseek-flash", retry_backoff_seconds=0)


def env(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: FakeProvider,
    sim: FakeProvider | None = None,
) -> EvalEnv:
    return EvalEnv(sessionmaker, provider, CONFIG, sim_provider=sim)


def make(**data: Any) -> Scenario:
    base: dict[str, Any] = {
        "id": "t-1",
        "category": "normal_booking",
        "split": "dev",
        "title": "test",
        "clock": "2026-10-06T09:15",  # Tuesday
        "expect": {},
    }
    return Scenario.model_validate(base | data)


def failed(run: Any, scenario: Scenario) -> list[str]:
    return [f"{c.name}: {c.detail}" for c in grade(run, scenario) if not c.passed]


def last_windows(request: FakeRequest) -> list[dict[str, Any]]:
    for message in reversed(request.messages):
        if message["role"] == "tool":
            content = json.loads(message["content"])
            if content.get("windows"):
                return list(content["windows"])
    raise AssertionError("no availability result in the prompt")


def offer(request: FakeRequest) -> LLMResult:
    windows = last_windows(request)
    return reply(f"I can do {windows[0]['label']} or {windows[1]['label']}. Which works?")


def book_first(request: FakeRequest) -> LLMResult:
    return tool(
        "create_booking",
        {
            "service_type": "ac_repair",
            "slot_id": last_windows(request)[0]["slot_id"],
            "customer_name": "Pat Lee",
            "address": "12 River St, Hoboken",
            "zip": "07030",
            "problem_description": "AC blowing warm air",
        },
    )


BOOKING_MESSAGES = [
    "My AC is blowing warm air. Can someone come tomorrow morning?",
    "The first one. Pat Lee, 12 River St, Hoboken 07030",
]
BOOKING_EXPECT = {
    "escalate": [],
    "bookings": {
        "new": 1,
        "service": ["ac_repair"],
        "windows": [{"date": "2026-10-07", "from": "08:00", "until": "11:59"}],
        "address": "12 River",
        "zip": "07030",
    },
}


def booking_agent() -> FakeProvider:
    return FakeProvider(
        [
            tool(
                "get_availability",
                {
                    "service_type": "ac_repair",
                    "earliest_date": "2026-10-07",
                    "latest_date": "2026-10-07",
                    "part_of_day": "morning",
                },
            ),
            offer,
            book_first,
            reply("You're booked for Wed Oct 7, 8-10am. Reply cancel my appointment to cancel."),
        ]
    )


async def test_a_booking_conversation_is_recorded_and_passes(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = make(
        customer={"kind": "scripted", "messages": BOOKING_MESSAGES}, expect=BOOKING_EXPECT
    )
    run = await run_scenario(env(sessionmaker, booking_agent()), scenario)

    assert run.harness_error is None
    assert failed(run, scenario) == []
    assert [e.handled_by for e in run.exchanges] == ["agent", "agent"]
    assert [b.start.hour for b in run.bookings] == [8]
    assert [c.name for c in run.tool_calls] == ["get_availability", "create_booking"]
    assert [t.llm_calls for t in run.turns] == [2, 2]
    assert run.agent_cost_usd > 0
    assert run.end_reason == "script_end"
    assert [m.direction for m in run.messages] == ["in", "out", "in", "out"]


async def test_grading_catches_a_booking_in_the_wrong_window(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    afternoon = {
        **BOOKING_EXPECT,
        "bookings": {"new": 1, "windows": [{"date": "2026-10-07", "from": "12:00"}]},
    }
    scenario = make(customer={"kind": "scripted", "messages": BOOKING_MESSAGES}, expect=afternoon)
    run = await run_scenario(env(sessionmaker, booking_agent()), scenario)
    assert [f.split(":")[0] for f in failed(run, scenario)] == ["booking_window"]


async def test_each_run_gets_its_own_business(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = make(
        customer={"kind": "scripted", "messages": BOOKING_MESSAGES}, expect=BOOKING_EXPECT
    )
    shared = env(sessionmaker, booking_agent())
    first = await run_scenario(shared, scenario)
    shared.agent_provider = booking_agent()
    second = await run_scenario(shared, scenario, repeat=1)
    assert failed(first, scenario) == failed(second, scenario) == []
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(Business.id))) == 2


async def test_a_clear_emergency_never_reaches_the_model(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = make(
        category="clear_emergency",
        customer={"kind": "scripted", "messages": ["I smell gas in my kitchen", "hello??"]},
        expect={
            "escalate": ["emergency"],
            "hazards": ["gas"],
            "safety_reply": {"at": 0, "hazards": ["gas"]},
            "bookings": {"created": 0},
        },
    )
    provider = FakeProvider()  # raises if the agent calls it
    run = await run_scenario(env(sessionmaker, provider), scenario)
    assert failed(run, scenario) == []
    assert provider.requests == []
    assert run.escalations[0].source == "prefilter"
    assert run.exchanges[0].mode_after == "handoff"
    assert run.exchanges[1].replies == []  # the owner has it now; the bot stays silent


def reschedule_to_first(request: FakeRequest) -> LLMResult:
    notes = " ".join(
        m["content"]
        for m in request.messages
        if m["role"] == "user" and isinstance(m["content"], str)
    )
    match = re.search(r"ref ([A-Z0-9]{6})", notes)
    assert match, "no booking ref in the context note"
    return tool(
        "reschedule_booking",
        {"booking_ref": match.group(1), "new_slot_id": last_windows(request)[0]["slot_id"]},
    )


async def test_fixture_bookings_are_labelled_and_reschedules_are_graded(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = make(
        category="reschedule",
        fixture={
            "customer": {"name": "Pat Lee", "address": "12 River St, Hoboken", "zip": "07030"},
            "bookings": [{"label": "b1", "service": "ac_repair", "start": "2026-10-08T10:00"}],
        },
        customer={"kind": "scripted", "messages": ["Can you move my AC visit to Friday morning?"]},
        expect={
            "escalate": [],
            "bookings": {"new": 0},
            "existing": {
                "b1": {
                    "state": "rescheduled",
                    "windows": [{"date": "2026-10-09", "from": "08:00", "until": "11:59"}],
                }
            },
        },
    )
    provider = FakeProvider(
        [
            tool(
                "get_availability",
                {
                    "service_type": "ac_repair",
                    "earliest_date": "2026-10-09",
                    "latest_date": "2026-10-09",
                    "part_of_day": "morning",
                },
            ),
            reschedule_to_first,
            reply("Done, moved to Fri Oct 9, 8-10am."),
        ]
    )
    run = await run_scenario(env(sessionmaker, provider), scenario)
    assert failed(run, scenario) == []
    (b1,) = run.bookings
    assert b1.label == "b1"
    assert b1.original_start is not None and b1.original_start.day == 8
    assert b1.start.day == 9


async def test_busy_fixture_books_out_every_technician_of_the_trade(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = make(
        fixture={"busy": [{"trade": "hvac", "start": "2026-10-07T08:00"}]},
        customer={"kind": "scripted", "messages": ["HELP"]},
    )
    run = await run_scenario(env(sessionmaker, FakeProvider()), scenario)
    assert run.harness_error is None
    async with sessionmaker() as session:
        filled = await session.scalar(select(func.count(Booking.id)))
    assert filled == 3  # Marcus, Luis, and Sam all do HVAC
    assert run.bookings == []  # other customers' bookings are not the texting customer's


async def test_an_opted_out_customer_gets_no_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = make(
        category="opt_out",
        fixture={
            "customer": {"name": "Rae", "address": "4 Hudson Pl", "zip": "07030", "opted_out": True}
        },
        customer={"kind": "scripted", "messages": ["Hi, I need a plumber"]},
        expect={"opted_out": True, "bookings": {"new": 0}, "escalate": []},
    )
    run = await run_scenario(env(sessionmaker, FakeProvider()), scenario)
    assert failed(run, scenario) == []
    assert run.exchanges[0].handled_by == "opted_out"
    assert run.exchanges[0].replies == []


async def test_a_simulated_customer_drives_the_conversation(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = make(
        customer={
            "kind": "simulated",
            "opening": "Is anyone there?",
            "persona": {"name": "Pat", "situation": "Curious", "goal": "Say hi"},
        },
        expect={"escalate": [], "bookings": {"new": 0}},
    )
    agent = FakeProvider([reply("Hi! How can I help?"), reply("Anytime. Text us if you need us.")])
    sim = FakeProvider([reply("Just checking you're real, thanks"), reply("[DONE]")])
    run = await run_scenario(env(sessionmaker, agent, sim), scenario)
    assert failed(run, scenario) == []
    assert [e.customer for e in run.exchanges] == [
        "Is anyone there?",
        "Just checking you're real, thanks",
    ]
    assert run.end_reason == "done"
    assert run.sim_calls == 2


async def test_a_broken_fixture_is_a_harness_error_not_an_agent_failure(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = make(
        fixture={
            "customer": {"name": "Pat", "address": "1 A St", "zip": "07030"},
            # Today 10am is inside the 2-hour lead time, so the domain refuses it.
            "bookings": [{"label": "b1", "service": "ac_repair", "start": "2026-10-06T10:00"}],
        },
        customer={"kind": "scripted", "messages": ["hi"]},
    )
    run = await run_scenario(env(sessionmaker, FakeProvider()), scenario)
    assert run.harness_error and "fixture booking b1" in run.harness_error
    assert run.excluded
    assert [c.name for c in grade(run, scenario)] == ["harness"]
