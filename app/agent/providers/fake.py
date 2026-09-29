"""A scripted provider: deterministic, free, and offline (D-014, D-019).

Each ``complete()`` call consumes the next scripted item: a result, an exception to raise, or a
function of the request (for replies that depend on what the agent sent). Every request is
recorded, so tests can assert on the exact prompt, including that it only grew at the end.
"""

import copy
import itertools
import json
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.agent.providers.base import LLMResult, Message, ToolCall, ToolChoice, Usage
from app.agent.providers.pricing import cost_usd

# A Sunday: off-peak all day, so scripted costs don't depend on when the tests run.
_OFF_PEAK = datetime(2026, 9, 27, 12, tzinfo=UTC)
_ids = itertools.count(1)


@dataclass(frozen=True)
class FakeRequest:
    model: str
    messages: list[Message]
    tools: list[dict[str, Any]]
    tool_choice: ToolChoice


Scripted = LLMResult | BaseException | Callable[[FakeRequest], LLMResult]


class FakeProvider:
    def __init__(self, script: Iterable[Scripted] = ()) -> None:
        self.script: deque[Scripted] = deque(script)
        self.requests: list[FakeRequest] = []

    def add(self, *items: Scripted) -> "FakeProvider":
        self.script.extend(items)
        return self

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        tool_choice: ToolChoice = "auto",
    ) -> LLMResult:
        request = FakeRequest(
            model, copy.deepcopy(list(messages)), copy.deepcopy(list(tools)), tool_choice
        )
        self.requests.append(request)
        if not self.script:
            raise AssertionError("FakeProvider ran out of scripted responses")
        item = self.script.popleft()
        if isinstance(item, BaseException):
            raise item
        result = item(request) if callable(item) else item
        # Re-stamp model and cost for the model actually requested.
        return LLMResult(
            model=model,
            content=result.content,
            tool_calls=result.tool_calls,
            finish_reason=result.finish_reason,
            usage=result.usage,
            latency_ms=result.latency_ms,
            cost_usd=cost_usd(model, result.usage, _OFF_PEAK),
            reasoning_content=result.reasoning_content,
        )


DEFAULT_USAGE = Usage(input_tokens=2000, cache_hit_tokens=1500, output_tokens=60)


def reply(text: str, usage: Usage = DEFAULT_USAGE) -> LLMResult:
    return LLMResult(
        model="fake",
        content=text,
        tool_calls=(),
        finish_reason="stop",
        usage=usage,
        latency_ms=5,
        cost_usd=Decimal(0),
    )


def call(name: str, arguments: dict[str, Any] | str, call_id: str | None = None) -> ToolCall:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCall(call_id or f"call_{next(_ids)}", name, raw)


def tools(*calls: ToolCall, usage: Usage = DEFAULT_USAGE, content: str | None = None) -> LLMResult:
    return LLMResult(
        model="fake",
        content=content,
        tool_calls=calls,
        finish_reason="tool_calls",
        usage=usage,
        latency_ms=5,
        cost_usd=Decimal(0),
    )


def tool(name: str, arguments: dict[str, Any] | str, call_id: str | None = None) -> LLMResult:
    return tools(call(name, arguments, call_id))
