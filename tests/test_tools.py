"""Tool schemas and argument parsing (D-001, D-010)."""

import json
from typing import Any

import pytest

from app.agent import prompts
from app.agent.executor import parse_call
from app.agent.providers.fake import call
from app.agent.tools import TOOLS, tool_specs


def walk(node: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(walk(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(walk(value))
    return found


def test_five_tools_with_plain_schemas() -> None:
    specs = tool_specs(["ac_repair", "leak_repair"])
    assert [s["function"]["name"] for s in specs] == [
        "get_availability",
        "create_booking",
        "reschedule_booking",
        "cancel_booking",
        "escalate_to_owner",
    ]
    for spec in specs:
        params = spec["function"]["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        for node in walk(params):
            assert not {"title", "anyOf", "$ref", "$defs", "default"} & set(node)
    create = specs[1]["function"]["parameters"]
    assert create["properties"]["service_type"]["enum"] == ["ac_repair", "leak_repair"]
    assert set(create["required"]) == {
        "service_type",
        "slot_id",
        "customer_name",
        "address",
        "zip",
        "problem_description",
    }
    escalate = specs[4]["function"]["parameters"]
    assert "system_error" not in escalate["properties"]["reason"]["enum"]


def test_no_tool_lets_the_model_name_a_customer_or_business() -> None:
    """D-010: identity comes from the session, so no argument can point anywhere else."""
    for spec in tool_specs():
        for name in spec["function"]["parameters"]["properties"]:
            assert not any(word in name for word in ("phone", "customer_id", "business_id"))


def test_parse_call_reports_problems_as_data() -> None:
    assert parse_call(call("get_availability", "{not json")).error == {
        "error": "invalid_arguments",
        "message": "Arguments were not valid JSON.",
    }
    assert parse_call(call("get_availability", "[1, 2]")).error is not None
    assert parse_call(call("book_it", {})).error == {
        "error": "unknown_tool",
        "message": "There is no tool named 'book_it'.",
    }
    missing = parse_call(call("create_booking", {"service_type": "ac_repair"}))
    assert missing.error is not None and "slot_id" in missing.error["message"]


def test_parse_call_ignores_extra_keys_and_strips_whitespace() -> None:
    parsed = parse_call(call("cancel_booking", {"booking_ref": "  ACD34K ", "note": "extra key"}))
    assert parsed.error is None
    assert parsed.args.booking_ref == "ACD34K"


def test_only_handoff_escalations_take_priority() -> None:
    emergency = parse_call(
        call("escalate_to_owner", {"reason": "emergency", "summary": "hot outlet"})
    )
    price = parse_call(call("escalate_to_owner", {"reason": "price_quote", "summary": "cost?"}))
    assert emergency.is_handoff_escalation
    assert not price.is_handoff_escalation
    assert not parse_call(call("get_availability", {"service_type": "x"})).is_handoff_escalation


def test_prompt_version_tracks_template_and_tool_descriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = prompts.prompt_version()
    name = prompts.SYSTEM_PROMPT_NAME
    assert version.startswith(f"{name}+") and len(version) == len(name) + 9

    prompts.prompt_version.cache_clear()
    original = TOOLS["cancel_booking"]
    monkeypatch.setitem(TOOLS, "cancel_booking", (original[0], original[1] + " Changed."))
    try:
        assert prompts.prompt_version() != version
    finally:
        prompts.prompt_version.cache_clear()


def test_tool_specs_are_json_serialisable() -> None:
    json.dumps(tool_specs(["a"]))
