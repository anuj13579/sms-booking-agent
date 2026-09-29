"""The inbound pipeline end to end, with a scripted stand-in for the Phase 2 agent."""

from dataclasses import dataclass, field
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Conversation, Customer, Escalation, Message
from app.domain.clock import FixedClock
from app.domain.enums import (
    Channel,
    ConversationMode,
    EscalationReason,
    EscalationSource,
    Hazard,
    MessageAuthor,
    MessageStatus,
)
from app.domain.escalation import escalate
from app.domain.notifications import AlertKind, OwnerAlert, RecordingNotifier
from app.domain.templates import FALLBACK_MESSAGE, safety_message
from app.pipeline import queue
from app.pipeline.inbound import (
    DEFAULT_LEASE,
    AgentTurnInput,
    AgentTurnOutput,
    HandledBy,
    TurnOutcome,
    handle_inbound,
    process_conversation,
    receive_inbound,
)
from tests.factories import World, build_world, local

PHONE = "+15550000001"


@dataclass
class ScriptedAgent:
    replies: list[str | Exception] = field(default_factory=list)
    calls: list[AgentTurnInput] = field(default_factory=list)

    async def run_turn(self, turn: AgentTurnInput) -> AgentTurnOutput:
        self.calls.append(turn)
        reply = self.replies.pop(0) if self.replies else "Sure, what day works for you?"
        if isinstance(reply, Exception):
            raise reply
        return AgentTurnOutput(reply)


class Harness:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], world: World) -> None:
        self.sessionmaker = sessionmaker
        self.world = world
        self.clock = FixedClock(local(date(2026, 10, 5), 9))
        self.agent = ScriptedAgent()
        self.notifier = RecordingNotifier()

    async def send(self, body: str, sid: str | None = None, phone: str = PHONE) -> TurnOutcome:
        outcome = await handle_inbound(
            self.sessionmaker,
            self.world.business.id,
            channel=Channel.SMS,
            from_number=phone,
            body=body,
            agent=self.agent,
            notifier=self.notifier,
            clock=self.clock,
            provider_sid=sid,
        )
        assert outcome is not None
        return outcome

    async def receive_only(self, body: str) -> Conversation:
        async with self.sessionmaker() as session, session.begin():
            received = await receive_inbound(
                session,
                self.world.business,
                channel=Channel.SMS,
                from_number=PHONE,
                body=body,
                clock=self.clock,
            )
        async with self.sessionmaker() as session:
            conversation = await session.get(Conversation, received.conversation_id)
            assert conversation is not None
            return conversation

    async def process(self, conversation_id: object) -> TurnOutcome:
        return await process_conversation(
            self.sessionmaker,
            conversation_id,
            agent=self.agent,  # type: ignore[arg-type]
            notifier=self.notifier,
            clock=self.clock,
        )

    async def customer(self) -> Customer:
        async with self.sessionmaker() as session:
            customer = await session.scalar(select(Customer).where(Customer.phone_e164 == PHONE))
            assert customer is not None
            return customer

    async def conversation(self) -> Conversation:
        async with self.sessionmaker() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.mode != ConversationMode.CLOSED)
            )
            assert conversation is not None
            return conversation

    async def messages(self) -> list[Message]:
        async with self.sessionmaker() as session:
            return list(await session.scalars(select(Message).order_by(Message.seq)))

    async def escalations(self) -> list[Escalation]:
        async with self.sessionmaker() as session:
            return list(await session.scalars(select(Escalation).order_by(Escalation.created_at)))

    def alerts(self, kind: AlertKind) -> list[OwnerAlert]:
        return [a for a in self.notifier.alerts if a.kind is kind]


@pytest.fixture
async def h(sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    async with sessionmaker() as session, session.begin():
        world = await build_world(session)
    return Harness(sessionmaker, world)


class TestAgentPath:
    async def test_message_goes_to_agent_and_reply_is_queued(self, h: Harness) -> None:
        outcome = await h.send("AC stopped working, can someone come Tuesday?")

        assert outcome.handled_by is HandledBy.AGENT
        assert outcome.replies == ["Sure, what day works for you?"]
        assert h.agent.calls[0].texts == ("AC stopped working, can someone come Tuesday?",)
        inbound, outbound = await h.messages()
        assert inbound.status == MessageStatus.PROCESSED
        assert (outbound.author, outbound.status) == (MessageAuthor.AGENT, MessageStatus.QUEUED)

    async def test_rapid_texts_are_one_turn(self, h: Harness) -> None:
        await h.receive_only("hi")
        await h.receive_only("my furnace is making a banging noise")
        conversation = await h.receive_only("can someone come tomorrow")

        outcome = await h.process(conversation.id)

        assert outcome.handled_by is HandledBy.AGENT
        assert len(h.agent.calls) == 1
        assert h.agent.calls[0].texts == (
            "hi",
            "my furnace is making a banging noise",
            "can someone come tomorrow",
        )

    async def test_nothing_pending_is_a_no_op(self, h: Harness) -> None:
        await h.send("hi")
        outcome = await h.process((await h.conversation()).id)
        assert outcome.handled_by is HandledBy.NOTHING
        assert len(h.agent.calls) == 1

    async def test_duplicate_delivery(self, h: Harness) -> None:
        await h.send("hi", sid="SM1")
        again = await handle_inbound(
            h.sessionmaker,
            h.world.business.id,
            channel=Channel.SMS,
            from_number=PHONE,
            body="hi",
            agent=h.agent,
            notifier=h.notifier,
            clock=h.clock,
            provider_sid="SM1",
        )
        assert again is None
        assert len(h.agent.calls) == 1

    async def test_agent_failure_gets_fallback_and_hands_off(self, h: Harness) -> None:
        h.agent.replies = [RuntimeError("LLM timeout")]

        outcome = await h.send("need my AC looked at")

        assert outcome.handled_by is HandledBy.AGENT_FAILED
        assert outcome.replies == [FALLBACK_MESSAGE]
        [escalation] = await h.escalations()
        assert (escalation.reason, escalation.source) == ("system_error", "system")
        assert (await h.conversation()).mode == ConversationMode.HANDOFF
        assert h.alerts(AlertKind.ESCALATION)
        assert all(m.status != MessageStatus.RECEIVED for m in await h.messages())

    async def test_stop_while_agent_is_thinking_suppresses_the_reply(self, h: Harness) -> None:
        sessionmaker = h.sessionmaker

        class OptsOutMidTurn(ScriptedAgent):
            async def run_turn(self, turn: AgentTurnInput) -> AgentTurnOutput:
                async with sessionmaker() as session, session.begin():
                    customer = await session.get(Customer, turn.customer_id)
                    assert customer is not None
                    customer.opted_out_at = h.clock.now()
                return AgentTurnOutput("Here are some times...")

        h.agent = OptsOutMidTurn()
        outcome = await h.send("what times do you have")

        assert outcome.replies == []
        assert [m.direction for m in await h.messages()] == ["in"]

    async def test_busy_while_another_worker_holds_the_lease(self, h: Harness) -> None:
        conversation = await h.receive_only("hello")
        async with h.sessionmaker() as session, session.begin():
            token = await queue.claim(session, conversation.id, h.clock.now(), DEFAULT_LEASE)
        assert token is not None

        assert (await h.process(conversation.id)).handled_by is HandledBy.BUSY
        assert h.agent.calls == []

        async with h.sessionmaker() as session, session.begin():
            await queue.release(session, conversation.id, token)
        assert (await h.process(conversation.id)).handled_by is HandledBy.AGENT

    async def test_failing_notifier_does_not_fail_the_turn(self, h: Harness) -> None:
        class Broken(RecordingNotifier):
            async def notify(self, alert: OwnerAlert) -> None:
                raise ConnectionError("SMS provider down")

        h.notifier = Broken()
        outcome = await h.send("I smell gas")
        assert outcome.handled_by is HandledBy.EMERGENCY
        assert outcome.replies  # the customer still got the safety text


class TestKeywords:
    async def test_stop_opts_out_and_silences_the_bot(self, h: Harness) -> None:
        stop = await h.send("STOP")
        assert stop.handled_by is HandledBy.KEYWORDS
        assert "unsubscribed" in stop.replies[0]
        assert (await h.customer()).opted_out_at is not None

        later = await h.send("actually my AC is broken")
        assert later.handled_by is HandledBy.OPTED_OUT
        assert later.replies == []
        assert h.agent.calls == []
        assert h.alerts(AlertKind.MESSAGE_FROM_OPTED_OUT)
        assert (await h.messages())[-1].status == MessageStatus.SKIPPED

    async def test_repeated_stop_confirms_once(self, h: Harness) -> None:
        await h.send("STOP")
        again = await h.send("stop")
        assert again.replies == []

    async def test_start_resubscribes(self, h: Harness) -> None:
        await h.send("STOP")
        start = await h.send("START")
        assert "resubscribed" in start.replies[0]
        assert (await h.customer()).opted_out_at is None
        assert (await h.send("need a plumber")).handled_by is HandledBy.AGENT

    async def test_yes_from_a_subscribed_customer_is_for_the_agent(self, h: Harness) -> None:
        outcome = await h.send("Yes")
        assert outcome.handled_by is HandledBy.AGENT
        assert h.agent.calls[0].texts == ("Yes",)

    async def test_help(self, h: Harness) -> None:
        outcome = await h.send("HELP")
        assert "(555) 555-0101" in outcome.replies[0]
        assert "STOP" in outcome.replies[0]
        assert h.agent.calls == []

    async def test_bare_cancel_opts_out_but_cancel_my_appointment_does_not(
        self, h: Harness
    ) -> None:
        assert (await h.send("Cancel my appointment")).handled_by is HandledBy.AGENT
        assert (await h.send("Cancel")).handled_by is HandledBy.KEYWORDS
        assert (await h.customer()).opted_out_at is not None

    async def test_keyword_and_request_in_one_batch(self, h: Harness) -> None:
        await h.receive_only("HELP")
        conversation = await h.receive_only("also my AC is broken")

        outcome = await h.process(conversation.id)

        assert outcome.handled_by is HandledBy.AGENT
        assert h.agent.calls[0].texts == ("also my AC is broken",)
        assert "STOP" in outcome.replies[0]  # help text first, then the agent's reply
        assert outcome.replies[1] == "Sure, what day works for you?"

    async def test_stop_then_request_in_one_batch_stays_silent(self, h: Harness) -> None:
        await h.receive_only("STOP")
        conversation = await h.receive_only("wait, my AC")

        outcome = await h.process(conversation.id)

        assert outcome.handled_by is HandledBy.OPTED_OUT
        assert len(outcome.replies) == 1  # only the opt-out confirmation
        assert h.agent.calls == []


class TestEmergencies:
    async def test_prefilter_sends_template_escalates_and_hands_off(self, h: Harness) -> None:
        outcome = await h.send("I smell gas in the kitchen")

        assert outcome.handled_by is HandledBy.EMERGENCY
        assert outcome.replies == [safety_message(Hazard.GAS, "Dana")]
        assert h.agent.calls == []  # the LLM never saw it
        [escalation] = await h.escalations()
        assert (escalation.reason, escalation.hazard, escalation.source, escalation.mode) == (
            "emergency",
            "gas",
            "prefilter",
            "handoff",
        )
        assert (await h.conversation()).mode == ConversationMode.HANDOFF
        [alert] = h.alerts(AlertKind.ESCALATION)
        assert alert.escalation_id == escalation.id

    async def test_bot_stays_silent_after_handoff(self, h: Harness) -> None:
        await h.send("I smell gas in the kitchen")
        outcome = await h.send("ok we're all outside now")

        assert outcome.handled_by is HandledBy.HANDOFF
        assert outcome.replies == []
        assert h.agent.calls == []
        assert h.alerts(AlertKind.MESSAGE_WHILE_HANDED_OFF)[-1].text == "ok we're all outside now"

    async def test_same_hazard_again_does_not_resend_the_template(self, h: Harness) -> None:
        await h.send("I smell gas in the kitchen")
        outcome = await h.send("the gas smell is getting stronger")

        assert outcome.replies == []
        assert len(await h.escalations()) == 1
        assert h.alerts(AlertKind.MESSAGE_WHILE_HANDED_OFF)

    async def test_emergency_beats_an_existing_handoff(self, h: Harness) -> None:
        """D-025: a human owning the chat must not delay the safety text."""
        await h.send("hello")
        async with h.sessionmaker() as session, session.begin():
            conversation = await session.get(Conversation, (await h.conversation()).id)
            assert conversation is not None
            await escalate(
                session,
                h.world.business,
                conversation,
                reason=EscalationReason.UPSET_CUSTOMER,
                summary="angry",
                source=EscalationSource.AGENT,
                now=h.clock.now(),
            )

        outcome = await h.send("and now there are sparks coming from the outlet")

        assert outcome.handled_by is HandledBy.EMERGENCY
        assert outcome.replies == [safety_message(Hazard.ELECTRICAL, "Dana")]

    async def test_opted_out_customer_gets_no_text_but_owner_is_paged(self, h: Harness) -> None:
        await h.send("STOP")
        outcome = await h.send("the basement is flooding")

        assert outcome.handled_by is HandledBy.EMERGENCY
        assert outcome.replies == []
        assert [e.hazard for e in await h.escalations()] == ["flooding"]
        assert h.alerts(AlertKind.ESCALATION)
