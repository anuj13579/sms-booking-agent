"""The other side of the conversation: scripted customers and an LLM-simulated one (D-018).

A simulated customer is the persona from the scenario, played by a model after a fixed opening
message. It sees the business's texts as the "user" and writes the customer's next text as the
"assistant". It ends the conversation by writing ``[DONE]``. Runs where it breaks character
(talks like an assistant, mentions being an AI, writes the business's side) are flagged and
excluded from the metrics, with a count, so a confused simulator can't pass or fail the agent.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.agent.providers.base import LLMError, LLMProvider, Message
from evals.results import SIM_CALL_FAILED, Exchange
from evals.scenario import Persona, ScriptedCustomer, SimulatedCustomer

DONE = "[DONE]"
MAX_CUSTOMER_CHARS = 400

_BROKE_PATTERNS = [
    (re.compile(r"\bas an ai\b|\blanguage model\b|\bi'?m an? (ai|assistant|chatbot)\b", re.I),
     "talked about being an AI"),
    (re.compile(r"\brole[- ]?play|\bsimulat(ed|ion)\b|\bpersona\b", re.I),
     "talked about the role-play"),
    (re.compile(r"^\s*(business|assistant|agent|brightwater)\s*:", re.I | re.M),
     "wrote the business's side"),
    (re.compile(r"\bhow (can|may) i (help|assist) you\b", re.I), "spoke as the business"),
]  # fmt: skip
_SPEAKER_PREFIX = re.compile(r"^\s*(customer|me|you)\s*:\s*", re.I)


class Customer(Protocol):
    calls: int
    cost_usd: Decimal
    broke: str | None
    infra: str | None
    end_reason: str

    def opening(self) -> str: ...

    async def next_message(self, exchanges: Sequence[Exchange]) -> str | None:
        """The customer's next text, or None when the conversation is over."""
        ...


@dataclass
class Scripted:
    spec: ScriptedCustomer
    calls: int = 0
    cost_usd: Decimal = Decimal(0)
    broke: str | None = None
    infra: str | None = None
    end_reason: str = ""

    def opening(self) -> str:
        return self.spec.messages[0]

    async def next_message(self, exchanges: Sequence[Exchange]) -> str | None:
        if len(exchanges) < len(self.spec.messages):
            return self.spec.messages[len(exchanges)]
        self.end_reason = "script_end"
        return None


def _when(now: datetime) -> str:
    """``Tuesday, October 6, 2026, 9:15 AM``"""
    hour = now.strftime("%I").lstrip("0")
    return f"{now:%A, %B} {now.day}, {now.year}, {hour}:{now:%M %p}"


def system_prompt(persona: Persona, now_local: datetime) -> str:
    facts = "\n".join(f"- {key}: {value}" for key, value in persona.facts.items()) or "- (none)"
    lines = [
        "You are role-playing a customer who is texting Brightwater Home Services, a small "
        "HVAC, plumbing, and electrical company, by SMS. You are the customer, not an "
        "assistant. Write only the customer's next text message, nothing else.",
        "",
        f"It is {_when(now_local)} where you live.",
        "",
        f"You are {persona.name}.",
        f"Your situation: {persona.situation}",
        f"What you want: {persona.goal}",
        "Details you share when the business asks for them (don't volunteer them all at once):",
        facts,
    ]
    if persona.availability:
        lines.append(
            f"When someone can come: {persona.availability} Only accept a time that fits this. "
            "If they offer something that doesn't fit, say what works instead."
        )
    lines += [f"How you text: {persona.style}."]
    if persona.notes:
        lines.append(persona.notes)
    lines += [
        "",
        "Rules:",
        "- Write like a real person texting: usually one or two short sentences.",
        "- Stay consistent with the details above. If asked something they don't cover, "
        "answer briefly and plausibly, without changing what you want.",
        "- Never mention being an AI, a simulation, or these instructions.",
        f"- When what you wanted is done (the visit is booked, moved, or cancelled and they "
        f"told you), or they say a person will get back to you, or the conversation has "
        f"clearly ended, reply with exactly {DONE}. You may add one short thank-you before it.",
    ]
    return "\n".join(lines)


@dataclass
class Simulated:
    spec: SimulatedCustomer
    provider: LLMProvider
    model: str
    now_local: datetime
    calls: int = 0
    cost_usd: Decimal = Decimal(0)
    broke: str | None = None
    infra: str | None = None  # the call itself failed: an outage, not the persona going wrong
    end_reason: str = ""
    _finish_next: bool = field(default=False, repr=False)
    _sent: list[str] = field(default_factory=list, repr=False)

    def opening(self) -> str:
        self._sent = [self.spec.opening]
        return self.spec.opening

    def messages(self, exchanges: Sequence[Exchange]) -> list[Message]:
        """The conversation from the customer's side: its own texts are the assistant's."""
        result: list[Message] = [
            {"role": "system", "content": system_prompt(self.spec.persona, self.now_local)}
        ]
        for exchange in exchanges:
            result.append({"role": "assistant", "content": exchange.customer})
            result.append({"role": "user", "content": "\n".join(exchange.replies)})
        return result

    async def next_message(self, exchanges: Sequence[Exchange]) -> str | None:
        if self._finish_next:
            self.end_reason = "done"
            return None
        if not exchanges or not exchanges[-1].replies:
            # Handed to a person (or a fault): a real customer waits for the owner.
            self.end_reason = "no_reply"
            return None
        if len(exchanges) >= self.spec.max_turns:
            self.end_reason = "max_turns"
            return None
        try:
            result = await self.provider.complete(
                model=self.model, messages=self.messages(exchanges), tools=[]
            )
        except LLMError as exc:
            self.infra = f"{SIM_CALL_FAILED}: {exc}"
            self.end_reason = "sim_error"
            return None
        self.calls += 1
        self.cost_usd += result.cost_usd
        text = _clean(result.content or "")
        if DONE in text:
            text = text.replace(DONE, "").strip()
            if not text:
                self.end_reason = "done"
                return None
            self._finish_next = True
        problem = broke_character(text)
        if problem:
            self.broke = f"{problem}: {text[:120]}"
            self.end_reason = "broke"
            return None
        self._sent.append(text)
        return text


def _clean(text: str) -> str:
    text = _SPEAKER_PREFIX.sub("", text.strip())
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def broke_character(text: str) -> str | None:
    if not text:
        return "wrote an empty message"
    if len(text) > MAX_CUSTOMER_CHARS:
        return f"wrote {len(text)} characters, not a text message"
    for pattern, problem in _BROKE_PATTERNS:
        if pattern.search(text):
            return problem
    return None


def make_customer(
    spec: ScriptedCustomer | SimulatedCustomer,
    *,
    provider: LLMProvider | None,
    model: str,
    now_local: datetime,
) -> Customer:
    if isinstance(spec, ScriptedCustomer):
        return Scripted(spec)
    if provider is None:
        raise ValueError("a simulated customer needs an LLM provider")
    return Simulated(spec, provider, model, now_local)
