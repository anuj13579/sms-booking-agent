"""The provider interface (D-014): one ``complete()`` call, in and out in OpenAI chat format.

OpenAI's chat-completions format is the lingua franca here because DeepSeek, OpenRouter, US hosts
of DeepSeek's open weights, and a local vLLM server all speak it. Messages stay plain dicts in
that format end to end, which is also what ``agent_turns.messages`` stores, so a transcript can be
replayed to any of them unchanged.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

Message = dict[str, Any]
ToolChoice = Literal["auto", "none"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text, exactly as the model produced it (it may be invalid)

    def as_message_part(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cache_hit_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens, which are billed as output
    reasoning_tokens: int = 0

    @property
    def cache_miss_tokens(self) -> int:
        return max(self.input_tokens - self.cache_hit_tokens, 0)


@dataclass(frozen=True)
class LLMResult:
    model: str
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    usage: Usage
    latency_ms: int
    cost_usd: Decimal
    # Thinking mode only. DeepSeek requires it to be sent back on every later request that
    # carries tools, so it is kept on the assistant message.
    reasoning_content: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def assistant_message(self) -> Message:
        """The message to append to the transcript for this response."""
        message: Message = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [call.as_message_part() for call in self.tool_calls]
        if self.reasoning_content:
            message["reasoning_content"] = self.reasoning_content
        return message


class LLMError(Exception):
    """A failed provider call. ``retryable`` covers timeouts, rate limits, and 5xx responses;
    a 4xx means the request itself is wrong and retrying would fail the same way."""

    def __init__(self, message: str, *, retryable: bool, latency_ms: int = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.latency_ms = latency_ms


class LLMProvider(Protocol):
    """Callers bound each call with ``asyncio.timeout`` (the agent loop owns the per-call and
    per-turn budgets); adapters may also keep their own transport timeout as a backstop."""

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        tool_choice: ToolChoice = "auto",
    ) -> LLMResult: ...
