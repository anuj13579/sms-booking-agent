"""Fixed customer texts must be GSM-7 and fit three concatenated SMS segments (459 characters)."""

import pytest

from app.domain import templates
from app.domain.enums import Hazard
from app.domain.sms import MAX_SMS_LENGTH, gsm7_length, non_gsm7, segments

LONG_OWNER = "Dana Brightwater-Montgomery"
LONG_BUSINESS = "Brightwater Home Services of Hudson County"


def rendered() -> dict[str, str]:
    texts = {f"safety:{h}": templates.safety_message(h, LONG_OWNER) for h in Hazard}
    texts["handoff"] = templates.handoff_message(LONG_OWNER)
    texts["opt_out"] = templates.OPT_OUT_CONFIRMATION.format(business=LONG_BUSINESS)
    texts["opt_in"] = templates.OPT_IN_CONFIRMATION.format(business=LONG_BUSINESS)
    texts["help"] = templates.HELP_MESSAGE.format(
        business=LONG_BUSINESS, owner_phone=templates.display_phone("+15555550101")
    )
    texts["fallback"] = templates.FALLBACK_MESSAGE
    return texts


def test_every_hazard_has_a_template() -> None:
    assert set(templates.SAFETY_TEMPLATES) == set(Hazard)


@pytest.mark.parametrize(("name", "text"), sorted(rendered().items()))
def test_template_is_gsm7_and_short(name: str, text: str) -> None:
    assert not non_gsm7(text), f"{name}: non-GSM-7 characters {non_gsm7(text)}"
    assert gsm7_length(text) <= MAX_SMS_LENGTH, f"{name}: {gsm7_length(text)} chars"
    assert segments(text) <= 3


def test_safety_templates_name_the_owner_and_say_911_where_it_matters() -> None:
    for hazard in Hazard:
        text = templates.safety_message(hazard, "Dana")
        assert "Dana" in text
    for hazard in (Hazard.GAS, Hazard.CO, Hazard.ELECTRICAL, Hazard.OTHER):
        assert "911" in templates.safety_message(hazard, "Dana")


def test_display_phone() -> None:
    assert templates.display_phone("+15555550101") == "(555) 555-0101"
    assert templates.display_phone("+442079460958") == "+442079460958"


def test_segment_math() -> None:
    assert segments("a" * 160) == 1
    assert segments("a" * 161) == 2  # concatenated segments carry 153 characters, not 160
    assert segments("a" * 306) == 2
    assert segments("a" * 307) == 3
    assert segments("a" * 459) == 3
    assert segments("a" * 460) == 4
    assert gsm7_length("[x]") == 5  # extension characters take two positions
    assert segments("a" * 69 + "\u2013") == 1  # an en dash forces UCS-2: 70 per segment
    assert segments("a" * 70 + "\u2013") == 2
    assert MAX_SMS_LENGTH == 459
