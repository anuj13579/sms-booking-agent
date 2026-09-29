"""Deterministic emergency pre-filter (D-003, D-004).

Tuned for **recall**. A false positive costs one conditional safety text ("*If* you smell gas...")
and an owner ping. A false negative can cost a life. So patterns are broad, there is no negation
handling ("no gas smell" still fires), and the false-positive rate is measured on the eval set's
"emergency look-alike" category instead of being engineered away here.

Two deliberate narrowings, each because the look-alike is a *routine* job for these trades and a
false alarm would hand the whole conversation to the owner:

* "gas water heater is leaking" is not a gas leak (a water leak from a gas appliance).
* "smoke detector" / "smoke alarm" alone is an install request; it fires only when the alarm is
  said to be going off.

The LLM is the second detector for hazards described without these words.
"""

import re

from app.domain.enums import Hazard


def _gap(max_words: int) -> str:
    """Separator between two terms allowing up to ``max_words`` words in between."""
    return rf"\W+(?:\w+\W+){{0,{max_words}}}?"


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


_GAS = _rx(
    rf"\b(?:smell\w*|smel|stink\w*|odou?r|whiff){_gap(3)}(?:gas|propane)\b",
    rf"\b(?:gas|propane)\b{_gap(2)}(?:smell\w*|odou?r|stink\w*)",
    r"\b(?:gas|propane)\W+(?!water\b)(?:\w+\W+){0,2}?leak\w*",
    rf"\bleak\w*{_gap(2)}(?:gas|propane)\b",
    r"\brotten\W+eggs?\b",
    r"\bsul(?:f|ph)ur\b",
    r"\bhiss\w*\b[^.!?\n]{0,40}\bgas\b|\bgas\b[^.!?\n]{0,40}\bhiss\w*",
)

_CO = _rx(
    r"\bcarbon\W+monoxide\b",
    r"\bmonoxide\b",
    r"\bco2?\W+(?:alarm|detector|monitor|sensor)s?\b",
)

_ELECTRICAL_PART = (
    r"(?:outlets?|plugs?|wires?|wiring|cords?|switch(?:es)?|panels?|breakers?|sockets?)"
)
_ELECTRICAL = _rx(
    r"\bspark\w*",
    r"\bsmok(?:e|ing|y)\b(?!\W+(?:detectors?|alarms?)\b)",
    rf"\bsmoke\W+(?:detectors?|alarms?)\b{_gap(3)}(?:going\W+off|went\W+off|sounding|beeping\W+non\W*stop|won'?t\W+stop)",
    rf"\b(?:burning|burnt|burned)\b{_gap(2)}(?:smell\w*|odou?r|plastic|rubber|wires?|wiring)",
    rf"\b(?:smell\w*|odou?r){_gap(3)}(?:burn\w*|smoke|electrical)",
    rf"\bmelt\w*{_gap(3)}{_ELECTRICAL_PART}",
    rf"\b{_ELECTRICAL_PART}{_gap(3)}(?:melt\w*|scorch\w*|smok\w*|sizzl\w*)",
    r"\bscorch\w*",
    r"\b(?:on\W+fire|caught\W+fire|electrical\W+fire|flames?|fire)\b",
    r"\belectrocut\w*|\b(?:got|getting|gets|get)\W+(?:a\W+)?shock(?:ed)?\b",
    r"\b(?:power|electric\w*)\W+lines?\W+(?:\w+\W+){0,2}?down\b|\bdowned\W+(?:power\W+)?lines?\b",
)

_FLOODING = _rx(
    r"\bflood\w*",
    r"\bburst\w*",
    rf"\bpipes?\b{_gap(3)}(?:broke|broken|split|exploded|blew)\b",
    rf"\bwater\b{_gap(3)}(?:everywhere|pouring|gushing|spraying|rushing|coming\W+through\W+the\W+ceiling)",
    rf"\b(?:gush\w*|spray\w*|pour\w*){_gap(3)}water\b",
    rf"\bceiling\b{_gap(3)}(?:collaps\w*|caving|falling\W+in)",
)

_NO_HEAT = _rx(
    r"\bno\W+heat\b",
    r"\b(?:heat|heater|heating|furnace|boiler)\b(?:\W+\w+){0,3}?\W+"
    r"(?:out|off|down|dead|died|broke\w*|stopped|not\W+working|isn'?t\W+working|"
    r"won'?t\W+(?:turn\W+on|start|work|come\W+on)|not\W+(?:turning|coming)\W+on)\b",
    r"\b(?:can'?t|cannot|won'?t)\W+(?:get|turn)\W+(?:the\W+)?(?:heat|furnace|heater|boiler)\b",
)
_COLD = _rx(
    r"\bfreezing\b|\bfrozen\b|\bfroze\b|\bfreeze\b",
    r"\bbelow\W+(?:zero|freezing)\b|\bsub\W*zero\b",
    r"\bhypotherm\w*",
    r"\b(?:baby|babies|infant|newborn|elderly)\b",
)
_TEMPERATURE = re.compile(r"(-?\d{1,2})\s*(?:degrees?|deg\b|°)", re.IGNORECASE)
COLD_THRESHOLD_F = 50


def _is_cold(text: str) -> bool:
    if _COLD.search(text):
        return True
    return any(int(m.group(1)) <= COLD_THRESHOLD_F for m in _TEMPERATURE.finditer(text))


def _normalise(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


def detect_hazards(text: str) -> list[Hazard]:
    """All hazards the text suggests, most severe first (``Hazard`` declaration order)."""
    text = _normalise(text)
    found = []
    if _GAS.search(text):
        found.append(Hazard.GAS)
    if _CO.search(text):
        found.append(Hazard.CO)
    if _ELECTRICAL.search(text):
        found.append(Hazard.ELECTRICAL)
    if _FLOODING.search(text):
        found.append(Hazard.FLOODING)
    if _NO_HEAT.search(text) and _is_cold(text):
        found.append(Hazard.NO_HEAT)
    return found
