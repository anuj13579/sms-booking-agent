"""The web-chat simulator API and the queue worker, with a FakeProvider behind the real app."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.loop import Agent, AgentConfig
from app.agent.providers.fake import FakeProvider, reply, tool
from app.config import Settings
from app.db.models import Message
from app.domain.clock import FixedClock
from app.domain.enums import Channel, MessageStatus
from app.domain.notifications import RecordingNotifier
from app.main import create_app
from app.pipeline.inbound import HandledBy, process_next, receive_inbound
from app.worker import run_worker
from tests.factories import World, build_world, local

PHONE = "+15550100001"


@dataclass
class Sim:
    client: httpx.AsyncClient
    provider: FakeProvider
    world: World
    sessionmaker: async_sessionmaker[AsyncSession]


def settings_for(world: World, **overrides: object) -> Settings:
    options: dict[str, object] = {
        "simulator_business_number": world.business.sms_number,
        "worker_enabled": False,
        "llm_api_key": "sk-test",
    }
    options.update(overrides)
    return Settings(**options)  # type: ignore[arg-type]


@pytest.fixture
async def sim(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Sim]:
    async with sessionmaker() as session, session.begin():
        world = await build_world(session)
    provider = FakeProvider()
    app = create_app(
        settings_for(world),
        sessionmaker=sessionmaker,
        provider=provider,
        clock=FixedClock(local(date(2026, 10, 5), 9)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        yield Sim(client, provider, world, sessionmaker)


async def test_page_and_info(sim: Sim) -> None:
    page = await sim.client.get("/")
    assert page.status_code == 200 and "SMS simulator" in page.text
    info = (await sim.client.get("/api/sim/info")).json()
    assert info["business"] == "Brightwater Test Co"
    assert info["model"] == "deepseek-flash"
    assert info["prompt_version"].startswith("system_v1+")
    assert info["api_key_configured"] is True
    assert (await sim.client.get("/healthz")).json() == {"ok": True}


async def test_send_and_inspect_a_conversation(sim: Sim) -> None:
    sim.provider.add(
        tool("get_availability", {"service_type": "ac_repair"}),
        reply("I can do Mon Oct 5, 1-3pm. Does that work?"),
    )
    sent = await sim.client.post(
        "/api/sim/messages", json={"phone": "(555) 010-0001", "body": "AC is out"}
    )
    assert sent.status_code == 200
    assert sent.json()["handled_by"] == HandledBy.AGENT
    assert sent.json()["replies"] == ["I can do Mon Oct 5, 1-3pm. Does that work?"]

    state = (await sim.client.get("/api/sim/state", params={"phone": PHONE})).json()
    assert [(m["direction"], m["status"]) for m in state["messages"]] == [
        ("in", "processed"),
        ("out", "sent"),  # web-chat delivery: the page has it
    ]
    assert state["conversation"]["mode"] == "bot"
    [turn] = state["turns"]
    assert turn["outcome"] == "replied"
    assert [c["step"] for c in turn["llm_calls"]] == [1, 2]
    assert turn["tool_calls"][0]["name"] == "get_availability"
    assert turn["tool_calls"][0]["result"]["windows"][0]["label"] == "Mon Oct 5, 1-3pm"
    assert float(state["totals"]["cost_usd"]) > 0
    assert state["totals"]["llm_calls"] == 2


async def test_owner_resolution_hands_back_to_the_agent(sim: Sim) -> None:
    sim.provider.add(
        tool("escalate_to_owner", {"reason": "human_requested", "summary": "wants a person"})
    )
    await sim.client.post("/api/sim/messages", json={"phone": PHONE, "body": "a human please"})
    state = (await sim.client.get("/api/sim/state", params={"phone": PHONE})).json()
    assert state["conversation"]["mode"] == "handoff"
    assert state["escalations"][0]["reason"] == "human_requested"

    resolved = (await sim.client.post("/api/sim/resolve", json={"phone": PHONE})).json()
    assert resolved == {"resolved": 1, "mode": "bot"}

    sim.provider.add(reply("Hi again! How can I help?"))
    sent = await sim.client.post("/api/sim/messages", json={"phone": PHONE, "body": "hello"})
    assert sent.json()["replies"] == ["Hi again! How can I help?"]


async def test_bookings_show_in_state(sim: Sim) -> None:
    sim.provider.add(
        tool(
            "create_booking",
            {
                "service_type": "ac_repair",
                "slot_id": "2026-10-06T08:00-04:00",
                "customer_name": "Pat Lee",
                "address": "12 River St, Hoboken",
                "zip": "07030",
                "problem_description": "warm air",
            },
        ),
        reply("Booked!"),
    )
    await sim.client.post("/api/sim/messages", json={"phone": PHONE, "body": "tue 8am"})
    state = (await sim.client.get("/api/sim/state", params={"phone": PHONE})).json()
    [booking] = state["bookings"]
    assert booking["window"] == "Tue Oct 6, 8-10am" and booking["status"] == "confirmed"
    assert state["customer"]["name"] == "Pat Lee"


async def test_bad_input(sim: Sim) -> None:
    bad = await sim.client.post("/api/sim/messages", json={"phone": "12", "body": "hi"})
    assert bad.status_code == 422
    empty = await sim.client.post("/api/sim/messages", json={"phone": PHONE, "body": ""})
    assert empty.status_code == 422
    assert (await sim.client.get("/api/sim/state", params={"phone": "x"})).status_code == 422
    unknown = (await sim.client.get("/api/sim/state", params={"phone": PHONE})).json()
    assert unknown["conversation"] is None
    missing = await sim.client.post("/api/sim/resolve", json={"phone": PHONE})
    assert missing.status_code == 404


async def test_missing_business_says_how_to_seed(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        Settings(simulator_business_number="+15559999999", worker_enabled=False),
        sessionmaker=sessionmaker,
        provider=FakeProvider(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://s") as c:
        response = await c.get("/api/sim/info")
    assert response.status_code == 503 and "app.seed" in response.json()["detail"]


async def test_simulator_can_be_switched_off(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        Settings(simulator_enabled=False, worker_enabled=False),
        sessionmaker=sessionmaker,
        provider=FakeProvider(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://s") as c:
        assert (await c.get("/")).status_code == 404
        assert (await c.get("/healthz")).status_code == 200


# --------------------------------------------------------------------------------------------
# the queue worker


async def _receive(
    sessionmaker: async_sessionmaker[AsyncSession], world: World, clock: FixedClock
) -> None:
    async with sessionmaker() as session, session.begin():
        await receive_inbound(
            session, world.business, channel=Channel.SMS, from_number=PHONE, body="hi", clock=clock
        )


async def test_process_next_answers_what_was_left_behind(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        world = await build_world(session)
    clock = FixedClock(local(date(2026, 10, 5), 9))
    provider = FakeProvider([reply("Hello from the worker")])
    agent = Agent(sessionmaker, provider, clock, AgentConfig())
    notifier = RecordingNotifier()

    await _receive(sessionmaker, world, clock)  # stored, but nobody processed it
    outcome = await process_next(sessionmaker, agent=agent, notifier=notifier, clock=clock)
    assert outcome is not None and outcome.replies == ["Hello from the worker"]
    assert await process_next(sessionmaker, agent=agent, notifier=notifier, clock=clock) is None


async def test_worker_loop_runs_until_stopped(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        world = await build_world(session)
    clock = FixedClock(local(date(2026, 10, 5), 9))
    agent = Agent(sessionmaker, FakeProvider([reply("worker reply")]), clock, AgentConfig())
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_worker(
            sessionmaker,
            agent=agent,
            notifier=RecordingNotifier(),
            clock=clock,
            stop=stop,
            poll_seconds=0.05,
        )
    )
    await _receive(sessionmaker, world, clock)
    for _ in range(100):
        async with sessionmaker() as session:
            statuses = list(await session.scalars(select(Message.status).order_by(Message.seq)))
        if statuses == [MessageStatus.PROCESSED, MessageStatus.QUEUED]:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert statuses == [MessageStatus.PROCESSED, MessageStatus.QUEUED]


async def test_lifespan_starts_and_stops_the_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session, session.begin():
        world = await build_world(session)
    clock = FixedClock(local(date(2026, 10, 5), 9))
    await _receive(sessionmaker, world, clock)
    app = create_app(
        settings_for(world, worker_enabled=True, worker_poll_seconds=0.05),
        sessionmaker=sessionmaker,
        provider=FakeProvider([reply("picked up by the app's worker")]),
        clock=clock,
    )
    async with app.router.lifespan_context(app):
        for _ in range(100):
            async with sessionmaker() as session:
                bodies = list(await session.scalars(select(Message.body).order_by(Message.seq)))
            if len(bodies) == 2:
                break
            await asyncio.sleep(0.05)
    assert bodies == ["hi", "picked up by the app's worker"]
