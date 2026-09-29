"""The OpenAI-compatible adapter, against a mocked HTTP transport (no network, no cost)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.agent.factory import make_provider
from app.agent.providers.base import LLMError, Usage
from app.agent.providers.openai_compat import OpenAICompatibleProvider, parse_usage
from app.agent.providers.pricing import cost_usd, is_peak
from app.config import Settings
from app.domain.clock import FixedClock

SUNDAY_NOON = datetime(2026, 9, 27, 12, tzinfo=UTC)  # off-peak
TUESDAY_2AM = datetime(2026, 9, 29, 2, tzinfo=UTC)  # peak
MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]


def completion(message: dict[str, Any], usage: dict[str, Any] | None = None, finish: str = "stop"):
    return {
        "id": "x",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage
        or {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"prompt_cache_hit_tokens": 800},
        },
    }


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def provider(recorder: Recorder, **kwargs: Any) -> OpenAICompatibleProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    options: dict[str, Any] = {
        "base_url": "https://api.deepseek.com",
        "api_key": "sk-test",
        "client": client,
        "wall_clock": FixedClock(SUNDAY_NOON),
    }
    options.update(kwargs)
    return OpenAICompatibleProvider(**options)


async def test_request_shape_and_text_reply() -> None:
    rec = Recorder(httpx.Response(200, json=completion({"role": "assistant", "content": "Hello"})))
    p = provider(
        rec, temperature=0.3, max_tokens=512, extra_body={"thinking": {"type": "disabled"}}
    )
    result = await p.complete(model="deepseek-flash", messages=MESSAGES, tools=TOOLS)

    request = rec.requests[0]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body == {
        "model": "deepseek-flash",
        "messages": MESSAGES,
        "stream": False,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.3,
        "max_tokens": 512,
        "thinking": {"type": "disabled"},
    }
    assert result.content == "Hello"
    assert result.tool_calls == ()
    assert result.usage == Usage(input_tokens=1000, cache_hit_tokens=800, output_tokens=50)
    # 800 hits * 0.003 + 200 misses * 0.15 + 50 out * 0.60, per million tokens
    assert result.cost_usd == Decimal("0.0000024") + Decimal("0.00003") + Decimal("0.00003")
    assert result.assistant_message() == {"role": "assistant", "content": "Hello"}


async def test_tool_calls_are_parsed_and_echoed_back() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_availability", "arguments": '{"service_type":"x"}'},
            }
        ],
    }
    rec = Recorder(httpx.Response(200, json=completion(message, finish="tool_calls")))
    result = await provider(rec).complete(model="deepseek-flash", messages=MESSAGES, tools=TOOLS)
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
        ("call_1", "get_availability", '{"service_type":"x"}')
    ]
    assert result.assistant_message() == {
        "role": "assistant",
        "content": "",
        "tool_calls": message["tool_calls"],
    }


async def test_reasoning_content_is_kept_for_thinking_mode() -> None:
    message = {"role": "assistant", "content": "Hi", "reasoning_content": "thinking..."}
    rec = Recorder(httpx.Response(200, json=completion(message)))
    result = await provider(rec).complete(model="deepseek-flash", messages=MESSAGES, tools=TOOLS)
    assert result.assistant_message()["reasoning_content"] == "thinking..."


async def test_no_tools_means_no_tool_choice() -> None:
    rec = Recorder(httpx.Response(200, json=completion({"role": "assistant", "content": "ok"})))
    await provider(rec).complete(model="deepseek-flash", messages=MESSAGES, tools=[])
    body = json.loads(rec.requests[0].content)
    assert "tools" not in body and "tool_choice" not in body


@pytest.mark.parametrize(
    ("response", "retryable"),
    [
        (httpx.Response(429, text="rate limited"), True),
        (httpx.Response(500, text="boom"), True),
        (httpx.Response(503, text="busy"), True),
        (httpx.Response(400, text="bad request"), False),
        (httpx.Response(401, text="bad key"), False),
        (httpx.Response(200, text="not json"), True),
        (httpx.ReadTimeout("slow"), True),
        (httpx.ConnectError("down"), True),
        (
            httpx.Response(
                200,
                json=completion(
                    {"role": "assistant", "content": ""}, finish="insufficient_system_resource"
                ),
            ),
            True,
        ),
    ],
)
async def test_errors_are_classified(response: httpx.Response | Exception, retryable: bool) -> None:
    with pytest.raises(LLMError) as caught:
        await provider(Recorder(response)).complete(
            model="deepseek-flash", messages=MESSAGES, tools=TOOLS
        )
    assert caught.value.retryable is retryable


async def test_missing_key_fails_clearly_without_a_request() -> None:
    rec = Recorder()
    p = provider(rec, api_key=None, require_key=True)
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY") as caught:
        await p.complete(model="deepseek-flash", messages=MESSAGES, tools=TOOLS)
    assert not caught.value.retryable
    assert rec.requests == []


def test_usage_field_variants() -> None:
    assert parse_usage({"prompt_tokens": 10, "prompt_cache_hit_tokens": 4}).cache_hit_tokens == 4
    assert (
        parse_usage(
            {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 6}}
        ).cache_hit_tokens
        == 6
    )
    usage = parse_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 9,
            "completion_tokens_details": {"reasoning_tokens": 7},
        }
    )
    assert (usage.output_tokens, usage.reasoning_tokens, usage.cache_miss_tokens) == (9, 7, 10)
    assert parse_usage(None) == Usage()


@pytest.mark.parametrize(
    ("at", "peak"),
    [
        (datetime(2026, 9, 28, 0, 59, tzinfo=UTC), False),  # Monday
        (datetime(2026, 9, 28, 1, 0, tzinfo=UTC), True),
        (datetime(2026, 9, 28, 3, 59, tzinfo=UTC), True),
        (datetime(2026, 9, 28, 4, 0, tzinfo=UTC), False),
        (datetime(2026, 9, 28, 6, 0, tzinfo=UTC), True),
        (datetime(2026, 9, 28, 10, 0, tzinfo=UTC), False),
        (datetime(2026, 10, 3, 2, 0, tzinfo=UTC), False),  # Saturday
        (SUNDAY_NOON, False),
    ],
)
def test_peak_hours(at: datetime, peak: bool) -> None:
    assert is_peak(at) is peak


def test_cost_per_model_and_peak_doubling() -> None:
    million = Usage(input_tokens=2_000_000, cache_hit_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("deepseek-flash", million, SUNDAY_NOON) == Decimal("0.753")
    assert cost_usd("deepseek-flash", million, TUESDAY_2AM) == Decimal("1.506")
    assert cost_usd("deepseek-v4-pro", million, SUNDAY_NOON) == Decimal("2.662")
    assert cost_usd("some-local-model", million, SUNDAY_NOON) == Decimal(0)


def test_factory_turns_thinking_off_for_deepseek_only() -> None:
    deepseek = make_provider(Settings(llm_api_key="sk-x", llm_base_url="https://api.deepseek.com"))
    body = deepseek.request_body("deepseek-flash", MESSAGES, TOOLS, "auto")
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.3

    thinking = make_provider(Settings(llm_api_key="sk-x", llm_thinking=True))
    assert thinking.request_body("deepseek-flash", MESSAGES, TOOLS, "auto")["thinking"] == {
        "type": "enabled"
    }

    local = make_provider(Settings(llm_base_url="http://localhost:8000/v1"))
    assert "thinking" not in local.request_body("m", MESSAGES, TOOLS, "auto")


def test_blank_key_counts_as_missing() -> None:
    assert Settings(llm_api_key="  ").llm_key is None
    assert Settings(llm_api_key="sk-x").llm_key == "sk-x"
