"""Fixed customer texts must be GSM-7 and fit three SMS segments (480 characters)."""

import pytest

from app.domain import templates
from app.domain.enums import Hazard

# The GSM 03.38 basic character set. One character outside it switches the whole SMS to UCS-2,
# which cuts a segment from 160 to 70 characters.
GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
MAX_SMS_CHARS = 480
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
    assert set(text) <= GSM7, f"{name}: non-GSM-7 characters {set(text) - GSM7}"
    assert len(text) <= MAX_SMS_CHARS, f"{name}: {len(text)} chars"


def test_safety_templates_name_the_owner_and_say_911_where_it_matters() -> None:
    for hazard in Hazard:
        text = templates.safety_message(hazard, "Dana")
        assert "Dana" in text
    for hazard in (Hazard.GAS, Hazard.CO, Hazard.ELECTRICAL, Hazard.OTHER):
        assert "911" in templates.safety_message(hazard, "Dana")


def test_display_phone() -> None:
    assert templates.display_phone("+15555550101") == "(555) 555-0101"
    assert templates.display_phone("+442079460958") == "+442079460958"
