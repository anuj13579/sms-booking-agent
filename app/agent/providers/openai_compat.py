"""One adapter for every OpenAI-compatible chat-completions endpoint (D-014).

DeepSeek is the default. Pointing ``LLM_BASE_URL`` at OpenRouter, a US host of DeepSeek's open
weights, or a local vLLM server is a config change, which is the escape hatch for the data
residency question in DESIGN section 10.

Plain httpx rather than the ``openai`` SDK: the whole protocol surface we use is one POST, and
owning it means every field that reaches the provider is visible in this file.
"""

import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from app.agent.providers.base import LLMError, LLMResult, Message, ToolCall, ToolChoice, Usage
from app.agent.providers.pricing import cost_usd
from app.domain.clock import Clock, SystemClock

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
# DeepSeek returns 200 with this finish reason when it is overloaded mid-generation.
RETRYABLE_FINISH = frozenset({"insufficient_system_resource"})


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        transport_timeout: float = 30.0,
        require_key: bool = False,
        client: httpx.AsyncClient | None = None,
        wall_clock: Clock | None = None,
    ) -> None:
        # A hosted API without a key fails every call; say why instead of returning 401s.
        self._missing_key = require_key and not api_key
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=transport_timeout
        )
        if client is not None:
            self._client.base_url = httpx.URL(base_url)
            self._client.headers.update(headers)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._extra_body = dict(extra_body or {})
        # Real time, not the conversation's clock: it decides peak vs off-peak billing, and eval
        # scenarios freeze the conversation clock on arbitrary dates.
        self._wall_clock = wall_clock or SystemClock()

    async def aclose(self) -> None:
        await self._client.aclose()

    def request_body(
        self,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        tool_choice: ToolChoice,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": list(messages), "stream": False}
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = tool_choice
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens
        body.update(self._extra_body)
        return body

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        tool_choice: ToolChoice = "auto",
    ) -> LLMResult:
        if self._missing_key:
            raise LLMError("no API key configured: set DEEPSEEK_API_KEY in .env", retryable=False)
        body = self.request_body(model, messages, tools, tool_choice)
        called_at = self._wall_clock.now()
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return round((time.perf_counter() - started) * 1000)

        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise LLMError("transport timeout", retryable=True, latency_ms=elapsed_ms()) from exc
        except httpx.TransportError as exc:
            raise LLMError(
                f"transport error: {exc!r}", retryable=True, latency_ms=elapsed_ms()
            ) from exc

        latency = elapsed_ms()
        if response.status_code >= 400:
            raise LLMError(
                f"HTTP {response.status_code}: {response.text[:500]}",
                retryable=response.status_code in RETRYABLE_STATUS or response.status_code >= 500,
                latency_ms=latency,
            )
        try:
            data = response.json()
            return parse_response(data, requested_model=model, latency_ms=latency, at=called_at)
        except LLMError:
            raise
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"malformed response: {response.text[:500]}", retryable=True, latency_ms=latency
            ) from exc


def parse_usage(raw: dict[str, Any] | None) -> Usage:
    """DeepSeek reports cache hits as ``prompt_cache_hit_tokens``, either at the top level of
    ``usage`` or under ``prompt_tokens_details``; OpenAI calls them ``cached_tokens``."""
    usage = raw or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    hit = (
        prompt_details.get("prompt_cache_hit_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or prompt_details.get("cached_tokens")
        or 0
    )
    return Usage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        cache_hit_tokens=int(hit),
        output_tokens=int(usage.get("completion_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
    )


def parse_response(
    data: dict[str, Any], *, requested_model: str, latency_ms: int, at: datetime
) -> LLMResult:
    choice = data["choices"][0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")
    if finish_reason in RETRYABLE_FINISH:
        raise LLMError(f"provider finish_reason={finish_reason}", retryable=True)
    tool_calls = tuple(
        ToolCall(
            id=str(call.get("id") or f"call_{index}"),
            name=str(call["function"]["name"]),
            arguments=str(call["function"].get("arguments") or "{}"),
        )
        for index, call in enumerate(message.get("tool_calls") or [])
    )
    usage = parse_usage(data.get("usage"))
    # Price by the model we asked for: some providers echo a dated snapshot name instead.
    return LLMResult(
        model=requested_model,
        content=message.get("content"),
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        latency_ms=latency_ms,
        cost_usd=cost_usd(requested_model, usage, at),
        reasoning_content=message.get("reasoning_content"),
        raw=data,
    )
