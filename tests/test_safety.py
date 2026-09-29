"""The emergency pre-filter (D-003, D-004).

This is a unit-level check that the patterns do what they say. Recall and false-positive rates
are *measured* by the eval harness (Phase 3) on the clear-emergency, subtle-emergency, and
look-alike categories; these lists are not that measurement.
"""

import pytest

from app.domain.enums import Hazard
from app.pipeline.safety import detect_hazards

MUST_DETECT = [
    # gas
    ("I smell gas in the kitchen", Hazard.GAS),
    ("smells like gas near the stove", Hazard.GAS),
    ("there’s a strong gas smell downstairs", Hazard.GAS),  # curly apostrophe from iPhones
    ("GAS LEAK", Hazard.GAS),
    ("i think the gas line is leaking", Hazard.GAS),
    ("house smells like rotten eggs", Hazard.GAS),
    ("sulfur smell by the furnace", Hazard.GAS),
    ("hissing noise from the gas meter", Hazard.GAS),
    ("smell propane in the basement", Hazard.GAS),
    # carbon monoxide
    ("carbon monoxide alarm is going off", Hazard.CO),
    ("CO detector beeping nonstop", Hazard.CO),
    ("co alarm went off twice", Hazard.CO),
    ("i think we have monoxide", Hazard.CO),
    # electrical
    ("outlet is sparking", Hazard.ELECTRICAL),
    ("sparks came out of the panel", Hazard.ELECTRICAL),
    ("burning smell from the breaker box", Hazard.ELECTRICAL),
    ("smells like something is burning in the wall", Hazard.ELECTRICAL),
    ("smoke coming out of the dryer outlet", Hazard.ELECTRICAL),
    ("the plug melted", Hazard.ELECTRICAL),
    ("scorch marks around the switch", Hazard.ELECTRICAL),
    ("smoke alarm is going off and I see haze", Hazard.ELECTRICAL),
    ("my son got a shock from the outlet", Hazard.ELECTRICAL),
    ("power line down in the yard", Hazard.ELECTRICAL),
    ("the fan motor caught fire", Hazard.ELECTRICAL),
    # flooding
    ("basement is flooding", Hazard.FLOODING),
    ("pipe burst in the wall", Hazard.FLOODING),
    ("water everywhere in the bathroom", Hazard.FLOODING),
    ("water pouring through the ceiling", Hazard.FLOODING),
    ("the pipe under the sink broke", Hazard.FLOODING),
    # no heat + cold
    ("no heat and it's freezing", Hazard.NO_HEAT),
    ("furnace stopped working, pipes might freeze", Hazard.NO_HEAT),
    ("heat is out, it's 45 degrees in here", Hazard.NO_HEAT),
    ("boiler died and we have a newborn", Hazard.NO_HEAT),
    ("can't get the heat to turn on, below zero tonight", Hazard.NO_HEAT),
]

MUST_NOT_DETECT = [
    # routine jobs that share words with emergencies
    "my gas water heater is leaking",  # a water leak, a plumbing visit
    "can you install a gas water heater",
    "can you install smoke detectors",
    "need a quote for a new gas furnace",
    "AC stopped working, can someone come Tuesday?",
    "furnace not working",  # no cold/at-risk signal: the agent handles urgency
    "heat is out, it's 68 in here",
    "leaky faucet in the kitchen",
    "toilet keeps running",
    "the fireplace needs cleaning",
    "water heater not making hot water",
    "can you come look at my panel? breakers trip sometimes",
    # subtle emergencies: deliberately left to the LLM (the eval set's "subtle" category)
    "the outlet by the crib is hot and smells like plastic",
]

# Known false positives, kept visible on purpose (D-004): the templates are conditional, so the
# customer gets sensible advice, and the look-alike eval category measures how often this bites.
KNOWN_FALSE_POSITIVES = [
    ("smelled burning last month, electrician fixed it", Hazard.ELECTRICAL),
    ("can you install a CO detector?", Hazard.CO),
    ("no gas smell anymore, thanks", Hazard.GAS),  # no negation handling, by design
]


@pytest.mark.parametrize(("text", "hazard"), MUST_DETECT)
def test_detects(text: str, hazard: Hazard) -> None:
    assert detect_hazards(text)[:1] == [hazard]


@pytest.mark.parametrize("text", MUST_NOT_DETECT)
def test_ignores(text: str) -> None:
    assert detect_hazards(text) == []


@pytest.mark.parametrize(("text", "hazard"), KNOWN_FALSE_POSITIVES)
def test_known_false_positives_still_fire(text: str, hazard: Hazard) -> None:
    assert hazard in detect_hazards(text)


def test_hazards_come_back_most_severe_first() -> None:
    assert detect_hazards("smell gas and the outlet is sparking") == [Hazard.GAS, Hazard.ELECTRICAL]


def test_matches_across_batched_messages() -> None:
    # Customers split thoughts across texts; the pipeline joins a batch with newlines.
    assert detect_hazards("i smell\ngas") == [Hazard.GAS]
