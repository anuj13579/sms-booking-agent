"""Opt-out, opt-in, and HELP keywords, handled before the LLM ever sees a message (D-020).

Mirrors Twilio's default keyword lists (verified against Twilio's Advanced Opt-Out docs,
2026-09-29), plus natural phrasings, because consent rules expect "any reasonable means" of
revoking consent to be honoured.

* A keyword counts only as the **whole** message, case-insensitive. Surrounding whitespace and
  punctuation are ignored ("Stop.", " stop! "). When in doubt we over-honour an opt-out: texting
  someone who asked us to stop is the worse error.
* "CANCEL" alone is an opt-out, exactly as the carrier treats it. "Cancel my appointment" is not;
  it goes to the agent. Our confirmations invite that longer phrasing.
* Opt-in keywords only mean something to an opted-out customer. A subscribed customer answering
  "Yes" to "Does Tuesday 10-12 work?" is confirming a booking, not resubscribing.
"""

import re
from enum import StrEnum


class Keyword(StrEnum):
    OPT_OUT = "opt_out"
    OPT_IN = "opt_in"
    HELP = "help"


OPT_OUT_KEYWORDS = frozenset(
    {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
)
OPT_IN_KEYWORDS = frozenset({"START", "UNSTOP", "YES"})
HELP_KEYWORDS = frozenset({"HELP", "INFO"})

_EDGES = re.compile(r"^[\W_]+|[\W_]+$")

_NATURAL_OPT_OUT = re.compile(
    r"""
    \b(?:stop|quit|cease)\s+(?:texting|txting|messaging|contacting)\b
    | \bstop\s+(?:sending\s+(?:me\s+)?|the\s+)(?:texts|txts|messages)\b
    | \b(?:don'?t|do\s+not|dont)\s+(?:text|txt|message|contact)\s+(?:me|this\s+number)\b
    | \bunsubscribe\b
    | \bopt\s*-?\s*out\b
    | \b(?:remove|take)\s+me\s+(?:off|from)\b
    | \bno\s+more\s+(?:texts|messages|txts)\b
    | ^\s*stop\s+all\W*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _single_word(body: str) -> str:
    return _EDGES.sub("", body.strip()).upper()


def classify_keyword(body: str, *, opted_out: bool) -> Keyword | None:
    word = _single_word(body)
    if word in OPT_OUT_KEYWORDS:
        return Keyword.OPT_OUT
    if word in HELP_KEYWORDS:
        return Keyword.HELP
    if word in OPT_IN_KEYWORDS:
        return Keyword.OPT_IN if opted_out else None
    if _NATURAL_OPT_OUT.search(body.replace("’", "'")):
        return Keyword.OPT_OUT
    return None
