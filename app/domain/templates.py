"""Fixed customer-facing texts. None of these is ever written by the LLM (D-003).

Every template must be GSM-7 and fit the 480-character cap; tests/test_templates.py enforces
both. The safety wording is an engineer's draft and must be reviewed before real use
(DESIGN section 10, risk 7).
"""

from app.domain.enums import Hazard

# Conditional wording on purpose (D-004): a false positive still reads as sensible advice.
SAFETY_TEMPLATES: dict[Hazard, str] = {
    Hazard.GAS: (
        "If you smell gas, leave the building now. Don't flip switches, use phones inside, or "
        "light anything. Once outside, call 911 or your gas utility's emergency line. "
        "I'm alerting {owner} right now."
    ),
    Hazard.CO: (
        "If your carbon monoxide alarm is going off, get everyone, pets included, outside to "
        "fresh air now and call 911. Don't go back in until responders say it's safe. "
        "I'm alerting {owner} right now."
    ),
    Hazard.ELECTRICAL: (
        "If you see sparks or smoke or smell burning, stay away from it. If you can safely reach "
        "your breaker panel, switch off that circuit. If there's any fire or smoke, get out and "
        "call 911. I'm alerting {owner} right now."
    ),
    Hazard.FLOODING: (
        "If water is flooding, shut off your main water valve if you can do it safely, and keep "
        "away from outlets or electrical equipment near the water. I'm alerting {owner} right now."
    ),
    Hazard.NO_HEAT: (
        "If anyone is at risk from the cold, please go somewhere warm. Don't use an oven or grill "
        "to heat your home. I'm alerting {owner} right now."
    ),
    Hazard.OTHER: (
        "If anyone is in danger, get to a safe place and call 911. I'm alerting {owner} right now."
    ),
}

HANDOFF_MESSAGE = "Thanks. I've passed this to {owner}, who will text you back as soon as possible."

# Opt-out keyword replies (D-020). On SMS, Twilio's own opt-out handling may send its reply
# instead; the Twilio adapter (Phase 5) decides whether these go out on that channel.
OPT_OUT_CONFIRMATION = (
    "{business}: You're unsubscribed and won't get more texts from us. Reply START to resubscribe."
)
OPT_IN_CONFIRMATION = "{business}: You're resubscribed. Text us any time to book a service visit."
HELP_MESSAGE = (
    "{business}: We book HVAC, plumbing, and electrical visits by text. For help call "
    "{owner_phone}. Reply STOP to unsubscribe."
)

# Sent when the agent can't finish a turn (loop limits, output guard failing twice).
FALLBACK_MESSAGE = "Thanks, we got your message and someone will follow up shortly."


def display_phone(e164: str) -> str:
    """``+15555550101`` -> ``(555) 555-0101`` for NANP numbers; anything else unchanged."""
    if len(e164) == 12 and e164.startswith("+1"):
        return f"({e164[2:5]}) {e164[5:8]}-{e164[8:]}"
    return e164


def safety_message(hazard: Hazard, owner: str) -> str:
    return SAFETY_TEMPLATES[hazard].format(owner=owner)


def handoff_message(owner: str) -> str:
    return HANDOFF_MESSAGE.format(owner=owner)
