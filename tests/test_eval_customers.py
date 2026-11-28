"""Simulated and scripted customers, and the judge, against a scripted FakeProvider. No DB."""

import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.agent.providers.base import LLMError
from app.agent.providers.fake import FakeProvider, reply
from evals.customers import Scripted, Simulated, broke_character, system_prompt
from evals.judge import RUBRICS, SIMULATOR_RUBRIC, Judge, parse, render_transcript
from evals.results import Exchange, MessageRecord, ScenarioRun
from evals.scenario import Persona, ScriptedCustomer, SimulatedCustomer

NOW = datetime(2026, 10, 6, 9, 15, tzinfo=ZoneInfo("America/New_York"))
PERSONA = Persona(
    name="Pat Lee",
    situation="AC blows warm air",
    goal="Book a repair",
    facts={"address": "12 River St, Hoboken", "zip": "07030"},
    availability="Tomorrow morning.",
)


def simulated(provider: FakeProvider, max_turns: int = 8) -> Simulated:
    spec = SimulatedCustomer(kind="simulated", opening="My AC is broken", persona=PERSONA,
                             max_turns=max_turns)  # fmt: skip
    return Simulated(spec, provider, "deepseek-flash", NOW)


def ex(customer: str, *replies: str) -> Exchange:
    return Exchange(customer=customer, replies=list(replies), handled_by="agent", at=NOW,
                    mode_after="bot")  # fmt: skip


async def test_scripted_customer_sends_its_messages_in_order() -> None:
    c = Scripted(ScriptedCustomer(kind="scripted", messages=["a", "b"]))
    assert c.opening() == "a"
    assert await c.next_message([ex("a", "ok")]) == "b"
    assert await c.next_message([ex("a", "ok"), ex("b", "ok")]) is None
    assert c.end_reason == "script_end"


def test_prompt_carries_the_date_and_the_persona() -> None:
    prompt = system_prompt(PERSONA, NOW)
    assert "Tuesday, October 6, 2026, 9:15 AM" in prompt
    assert "12 River St, Hoboken" in prompt
    assert "Tomorrow morning." in prompt
    assert "[DONE]" in prompt


async def test_simulated_customer_sees_the_business_as_the_user() -> None:
    provider = FakeProvider([reply("Tomorrow at 8 works")])
    c = simulated(provider)
    c.opening()
    text = await c.next_message([ex("My AC is broken", "I can do Wed 8-10am. Which works?")])
    assert text == "Tomorrow at 8 works"
    sent = provider.requests[0].messages
    assert [m["role"] for m in sent] == ["system", "assistant", "user"]
    assert sent[2]["content"] == "I can do Wed 8-10am. Which works?"
    assert c.calls == 1


async def test_done_ends_the_conversation() -> None:
    c = simulated(FakeProvider([reply("[DONE]")]))
    c.opening()
    assert await c.next_message([ex("hi", "You're booked, ref ABC123")]) is None
    assert c.end_reason == "done"


async def test_a_thank_you_before_done_is_sent_then_it_ends() -> None:
    c = simulated(FakeProvider([reply("Great, thanks! [DONE]")]))
    c.opening()
    history = [ex("hi", "You're booked")]
    assert await c.next_message(history) == "Great, thanks!"
    assert await c.next_message([*history, ex("Great, thanks!", "You're welcome")]) is None
    assert c.end_reason == "done"


async def test_no_reply_means_the_customer_waits() -> None:
    c = simulated(FakeProvider())
    c.opening()
    assert await c.next_message([ex("I want a manager")]) is None
    assert c.end_reason == "no_reply"


async def test_max_turns() -> None:
    c = simulated(FakeProvider(), max_turns=1)
    c.opening()
    assert await c.next_message([ex("hi", "hello")]) is None
    assert c.end_reason == "max_turns"


async def test_breaking_character_is_flagged() -> None:
    c = simulated(FakeProvider([reply("As an AI language model, I can't have an AC.")]))
    c.opening()
    assert await c.next_message([ex("hi", "What's wrong?")]) is None
    assert c.end_reason == "broke"
    assert c.broke and "AI" in c.broke


async def test_a_failed_simulator_call_is_an_outage_not_a_broken_persona() -> None:
    c = simulated(FakeProvider([LLMError("timeout", retryable=True)]))
    c.opening()
    assert await c.next_message([ex("hi", "What's wrong?")]) is None
    assert c.end_reason == "sim_error"
    assert c.infra and c.infra.startswith("simulator call failed")
    assert c.broke is None


def test_break_detection() -> None:
    assert broke_character("Tuesday works for me") is None
    assert broke_character("Business: How can I help you today?")
    assert broke_character("this is a role-play right?")
    assert broke_character("x" * 500)
    assert broke_character("")


# --------------------------------------------------------------------------------------------
# judge


def judged_run() -> ScenarioRun:
    return ScenarioRun(
        scenario_id="j-1",
        category="normal_booking",
        split="dev",
        title="t",
        messages=[
            MessageRecord(direction="in", author="customer", body="hi", status="processed"),
            MessageRecord(direction="out", author="agent", body="Hello!", status="sent"),
            MessageRecord(direction="out", author="system", body="Passing to Dana", status="sent"),
        ],
    )


def answers(**overrides: str) -> str:
    data = {key: {"answer": "yes", "reason": "fine"} for key in RUBRICS}
    for key, value in overrides.items():
        data[key] = {"answer": value, "reason": "because"}
    return json.dumps({"answers": data})


def test_transcript_marks_fixed_texts() -> None:
    assert render_transcript(judged_run()).splitlines() == [
        "Customer: hi",
        "Business: Hello!",
        "Business [fixed text]: Passing to Dana",
    ]


def test_parse_accepts_a_fenced_reply() -> None:
    result = parse("```json\n" + answers(polite="no") + "\n```", RUBRICS)
    assert result["polite"].answer is False
    assert result["sms_style"].answer is True


async def test_judge_retries_a_malformed_reply_once() -> None:
    provider = FakeProvider([reply("not json"), reply(answers())])
    r = judged_run()
    await Judge(provider, "deepseek-flash").grade(r)
    assert r.judge_error is None
    assert set(r.judge) == set(RUBRICS) - {SIMULATOR_RUBRIC}  # a scripted run
    assert len(provider.requests) == 2
    assert r.judge_cost_usd > Decimal(0)


async def test_judge_gives_up_after_two_bad_replies() -> None:
    r = judged_run()
    await Judge(FakeProvider([reply("nope"), reply("{}")]), "deepseek-flash").grade(r)
    assert r.judge == {}
    assert r.judge_error and "unparseable" in r.judge_error


async def test_judge_flags_a_simulator_that_broke_character() -> None:
    r = judged_run()
    r.customer_kind = "simulated"
    await Judge(FakeProvider([reply(answers(**{SIMULATOR_RUBRIC: "no"}))]), "m").grade(r)
    assert r.simulator_broke and r.simulator_broke.startswith("judge:")
    assert r.excluded


async def test_scripted_customers_are_never_asked_about_staying_in_character() -> None:
    """An injection scenario's customer is adversarial on purpose; that's not a simulator fault.
    (Found in the first live run: ot-011 was wrongly excluded.)"""
    provider = FakeProvider([reply(answers(**{SIMULATOR_RUBRIC: "no"}))])
    r = judged_run()  # scripted by default
    await Judge(provider, "m").grade(r)
    assert SIMULATOR_RUBRIC not in provider.requests[0].messages[1]["content"]
    assert SIMULATOR_RUBRIC not in r.judge
    assert not r.excluded
