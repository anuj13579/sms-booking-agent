"""Deterministic checks on every model-written reply before it is sent (D-016, D-027, D-029).

1. **Normalise** what models habitually produce but SMS handles badly: curly quotes, en and em
   dashes, ellipses, non-breaking spaces, and markdown bold. These are rewritten, not rejected.
2. **Reject** what can't be fixed mechanically: a price or dollar amount (prices come only from
   the owner), characters still outside GSM-7 (emoji), more than three SMS segments, or nothing
   at all. The loop then asks the model once to rewrite; a second failure sends the fixed
   fallback text and hands the conversation to the owner.

The price check is a regex, so it can be fooled ("eighty-nine"). It catches the common forms
cheaply; the eval set's price-request category measures what it misses.
"""

import re
from dataclasses import dataclass, field

from app.domain.sms import MAX_SMS_LENGTH, gsm7_length, non_gsm7

_REPLACEMENTS = str.maketrans(
    {
        "‘": "'",  # left single quote
        "’": "'",  # right single quote / apostrophe
        "‚": "'",
        "‛": "'",
        "′": "'",  # prime
        "“": '"',
        "”": '"',
        "„": '"',
        "″": '"',
        "‐": "-",  # hyphen
        "‑": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        "–": "-",  # en dash
        "—": "-",  # em dash
        "−": "-",  # minus sign
        "…": "...",
        " ": " ",  # non-breaking space
        " ": " ",
        " ": " ",
        "​": "",  # zero-width space
        "•": "-",  # bullet
    }
)
_MARKDOWN_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")
_NUMBER_WORDS = (
    r"(?:a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|couple\W+hundred|few)"
)
_PRICE = re.compile(
    rf"""
    [$€£¥]\s?\d                                      # $89, $ 1,200
    | \d[\d,.]*\s?[$€£]                              # 89$
    | \b\d[\d,.]*\s*(?:k\s+)?(?:dollars?|bucks|usd|euros?)\b   # 89 dollars, 2k bucks
    | \busd\s?\d                                     # USD 89
    | \b{_NUMBER_WORDS}(?:[\s-]+{_NUMBER_WORDS})*\s+(?:dollars|bucks)\b   # ninety-nine bucks
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class GuardResult:
    text: str  # the normalised reply, which is what gets sent
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def normalise(text: str) -> str:
    text = text.translate(_REPLACEMENTS)
    text = _MARKDOWN_BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return _EXTRA_BLANK_LINES.sub("\n\n", text)


def mentions_price(text: str) -> bool:
    return bool(_PRICE.search(text))


def check_reply(raw: str | None) -> GuardResult:
    text = normalise(raw or "")
    problems = []
    if not text:
        problems.append("the reply was empty")
    if mentions_price(text):
        problems.append(
            "it mentions a price or dollar amount; never state prices, only the owner quotes them"
        )
    bad = non_gsm7(text)
    if bad:
        shown = " ".join(sorted(bad))
        problems.append(f"it uses characters SMS can't send cheaply ({shown}); use plain text only")
    length = gsm7_length(text)
    if length > MAX_SMS_LENGTH:
        problems.append(f"it is {length} characters long; the limit is {MAX_SMS_LENGTH}")
    return GuardResult(text, problems)


def feedback(problems: list[str]) -> str:
    """What the model is told when its reply is rejected."""
    return (
        "[Automatic check, not from the customer] Your last reply was NOT sent because "
        + "; and ".join(problems)
        + ". Write the reply again, fixing that. The customer hasn't seen the rejected version."
    )
