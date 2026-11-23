"""Deterministic checks on every model-written reply before it is sent (D-016, D-027, D-029).

1. **Normalise** what models habitually produce but SMS handles badly: curly quotes, en and em
   dashes, ellipses, non-breaking spaces, and markdown bold. These are rewritten, not rejected.
2. **Reject** what can't be fixed mechanically: a price or dollar amount (prices come only from
   the owner), characters still outside GSM-7 (emoji), more than three SMS segments, or nothing
   at all. The loop then asks the model once to rewrite; a second failure sends the fixed
   fallback text and hands the conversation to the owner.
3. **Unbacked follow-ups.** A reply saying the owner will follow up ("I'll have Dana reach out")
   is rejected unless something in the conversation was actually escalated: otherwise nobody
   tells the owner, and the customer waits for a call that never comes (eval scenarios la-001,
   la-013). The loop supplies whether an escalation exists; this module only spots the promise.

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


def promises_owner_follow_up(text: str, owner: str) -> bool:
    """Spot a promise that the owner will follow up: "Dana will reach out", "Dana'll be in
    touch", "I'll have Dana call you", "I've passed this to Dana". Not "Dana can't" or "the
    technician will text you"."""
    o = re.escape(owner)
    return bool(
        re.search(
            rf"\b{o}\s+(?:will|is\s+going\s+to|is\s+gonna)\b"
            rf"|\b{o}'ll\b"
            rf"|\b(?:have|ask|get)\s+{o}\s+(?:to\s+)?(?:reach|contact|call|text|follow|get|be)\b"
            rf"|\b(?:passed|passing|pass|forwarded|sent|flagged)\s+(?:this|it|that|your\s+\w+)"
            rf"\s+(?:along\s+|on\s+)?to\s+{o}\b",
            text,
            re.IGNORECASE,
        )
    )


def check_reply(
    raw: str | None, *, owner: str = "the owner", follow_up_backed: bool = True
) -> GuardResult:
    """``follow_up_backed`` is False when the conversation has no escalation, so a promise that
    the owner will follow up would be empty."""
    text = normalise(raw or "")
    problems = []
    if not text:
        problems.append("the reply was empty")
    if not follow_up_backed and promises_owner_follow_up(text, owner):
        problems.append(
            f"it says {owner} will follow up, but nothing has been passed to {owner}. If the "
            f"customer needs {owner} (a quote, an estimate, a callback), call escalate_to_owner "
            "first; otherwise don't promise a follow-up"
        )
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
