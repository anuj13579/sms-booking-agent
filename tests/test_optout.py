"""Opt-out handling mirrors the carrier's (D-020)."""

import pytest

from app.pipeline.optout import (
    HELP_KEYWORDS,
    OPT_IN_KEYWORDS,
    OPT_OUT_KEYWORDS,
    Keyword,
    classify_keyword,
)


def test_keyword_lists_match_twilio_defaults() -> None:
    # Twilio Advanced Opt-Out docs, checked 2026-09-29. REVOKE and OPTOUT were added 2025-05-13.
    assert {
        "STOP",
        "STOPALL",
        "UNSUBSCRIBE",
        "CANCEL",
        "END",
        "QUIT",
        "OPTOUT",
        "REVOKE",
    } == OPT_OUT_KEYWORDS
    assert {"START", "UNSTOP", "YES"} == OPT_IN_KEYWORDS
    assert {"HELP", "INFO"} == HELP_KEYWORDS


@pytest.mark.parametrize(
    "body",
    [
        "STOP",
        "stop",
        " Stop ",
        "Stop.",
        "stop!!",
        "CANCEL",
        "cancel",
        "Quit",
        "end",
        "OptOut",
        "revoke",
        "unsubscribe",
        "STOPALL",
    ],
)
def test_single_word_opt_out(body: str) -> None:
    assert classify_keyword(body, opted_out=False) is Keyword.OPT_OUT
    assert classify_keyword(body, opted_out=True) is Keyword.OPT_OUT


@pytest.mark.parametrize(
    "body",
    [
        "stop texting me",
        "Please stop messaging this number",
        "don't text me again",
        "Do not contact me",
        "unsubscribe me please",
        "take me off your list",
        "remove me from this list",
        "no more texts",
        "opt out",
        "stop sending me messages",
        "STOP ALL",
        "please stop texting me",
    ],
)
def test_natural_opt_out(body: str) -> None:
    assert classify_keyword(body, opted_out=False) is Keyword.OPT_OUT


@pytest.mark.parametrize(
    "body",
    [
        "Cancel my appointment",  # D-020: goes to the agent
        "can I cancel tuesday?",
        "the water won't stop running",
        "please stop sending the technician, I fixed it",
        "the leak finally stopped",
        "At the end of the day works",
        "I quit smoking so no smell from me lol",
        "stopcock is stuck",
        "Help me pick a time",
        "yes please",
    ],
)
def test_not_a_keyword(body: str) -> None:
    assert classify_keyword(body, opted_out=False) is None


def test_yes_only_resubscribes_an_opted_out_customer() -> None:
    assert classify_keyword("Yes", opted_out=False) is None  # confirming "Does Tuesday work?"
    assert classify_keyword("Yes", opted_out=True) is Keyword.OPT_IN
    assert classify_keyword("START", opted_out=False) is None
    assert classify_keyword("start", opted_out=True) is Keyword.OPT_IN
    assert classify_keyword("unstop", opted_out=True) is Keyword.OPT_IN


def test_help_always_answers() -> None:
    assert classify_keyword("HELP", opted_out=False) is Keyword.HELP
    assert classify_keyword("info", opted_out=True) is Keyword.HELP
