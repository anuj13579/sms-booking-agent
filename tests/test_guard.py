"""The output guard: normalise what can be fixed, reject what can't (D-016, D-027, D-029)."""

import pytest

from app.agent.guard import check_reply, feedback, mentions_price, normalise
from app.domain.sms import MAX_SMS_LENGTH


@pytest.mark.parametrize(
    "text",
    [
        "A diagnostic visit is $89.",
        "It's usually $ 150 or so",
        "around 200 dollars",
        "about 2k bucks",
        "Roughly ninety-nine bucks for the visit",
        "a couple hundred dollars",
        "USD 120",
        "The fee is 75$",
        "€50 call-out",
    ],
)
def test_price_mentions_are_caught(text: str) -> None:
    assert mentions_price(text)


@pytest.mark.parametrize(
    "text",
    [
        "Dana will follow up about pricing.",
        "Your ref is ACD34K, Tue Oct 6, 8-10am.",
        "We can't quote prices by text, but Dana can.",
        "Is the unit on the 2nd floor?",
        "Call us at (555) 555-0101",
        "The job at 12 River St is booked.",
    ],
)
def test_ordinary_replies_pass_the_price_check(text: str) -> None:
    assert not mentions_price(text)


def test_normalises_typography_instead_of_rejecting() -> None:
    raw = "You’re booked for “Tue Oct 6, 8–10am” — see you then… **Ref ACD34K**"
    result = check_reply(raw)
    assert result.ok
    assert result.text == 'You\'re booked for "Tue Oct 6, 8-10am" - see you then... Ref ACD34K'


def test_rejects_emoji_and_names_the_character() -> None:
    result = check_reply("You're all set! \U0001f44d")
    assert not result.ok
    assert "\U0001f44d" in result.problems[0]


def test_rejects_long_and_empty_replies() -> None:
    assert check_reply("x" * MAX_SMS_LENGTH).ok
    too_long = check_reply("x" * (MAX_SMS_LENGTH + 1))
    assert not too_long.ok and "459" in too_long.problems[0]
    assert check_reply("   ").problems == ["the reply was empty"]
    assert check_reply(None).problems == ["the reply was empty"]


def test_collapses_blank_lines_and_trailing_spaces() -> None:
    assert normalise("Hi   \n\n\n\nThere  ") == "Hi\n\nThere"


def test_feedback_says_it_is_not_from_the_customer() -> None:
    text = feedback(["it mentions a price"])
    assert text.startswith("[Automatic check, not from the customer]")
    assert "it mentions a price" in text
