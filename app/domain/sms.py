"""SMS encoding and length rules (D-027, D-029).

One character outside the GSM 03.38 alphabet switches the whole message to UCS-2, and a segment
drops from 160 to 70 characters. Long messages are also split into *concatenated* segments, and
each of those loses 7 characters to the header that stitches them together: 153 per segment in
GSM-7, 67 in UCS-2. So three segments hold 459 GSM-7 characters, not 3 x 160 = 480. Characters
from the GSM-7 extension table (``[]{}~|^\\`` and the euro sign) take two positions each.
"""

GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")

SINGLE_SEGMENT = 160
CONCAT_SEGMENT = 153
UCS2_SINGLE_SEGMENT = 70
UCS2_CONCAT_SEGMENT = 67
MAX_SEGMENTS = 3
# The longest text we ever send: three concatenated GSM-7 segments.
MAX_SMS_LENGTH = CONCAT_SEGMENT * MAX_SEGMENTS


def non_gsm7(text: str) -> set[str]:
    return {c for c in text if c not in GSM7_BASIC and c not in GSM7_EXTENSION}


def gsm7_length(text: str) -> int:
    """Length in GSM-7 septets: extension-table characters count twice."""
    return sum(2 if c in GSM7_EXTENSION else 1 for c in text)


def segments(text: str) -> int:
    """How many billed SMS segments ``text`` needs."""
    if non_gsm7(text):
        # UCS-2 counts UTF-16 code units: an emoji outside the BMP takes two.
        units = len(text.encode("utf-16-le")) // 2
        single, concat = UCS2_SINGLE_SEGMENT, UCS2_CONCAT_SEGMENT
    else:
        units = gsm7_length(text)
        single, concat = SINGLE_SEGMENT, CONCAT_SEGMENT
    if units <= single:
        return 1
    return -(-units // concat)


def fits(text: str) -> bool:
    """GSM-7 only, and at most three segments."""
    return not non_gsm7(text) and gsm7_length(text) <= MAX_SMS_LENGTH
