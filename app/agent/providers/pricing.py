"""Token prices and the cost of one call (DESIGN section 8).

Source: DeepSeek's "Models & Pricing" page, checked 2026-09-26 and again 2026-09-29. Prices are
USD per 1M tokens, off-peak. Peak is double: 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday.
(DeepSeek also treats Chinese public holidays as off-peak; ignoring that overstates cost on those
days, which is the safe direction for a budget.) Re-check the page before trusting a cost report.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.agent.providers.base import Usage

MILLION = Decimal(1_000_000)
PEAK_MULTIPLIER = Decimal(2)


@dataclass(frozen=True)
class Price:
    cache_hit: Decimal
    cache_miss: Decimal
    output: Decimal


PRICES: dict[str, Price] = {
    "deepseek-flash": Price(Decimal("0.003"), Decimal("0.15"), Decimal("0.60")),
    "deepseek-v4-pro": Price(Decimal("0.022"), Decimal("0.66"), Decimal("1.98")),
}


def is_peak(at: datetime) -> bool:
    if at.tzinfo is None:
        raise ValueError("naive datetime: pass a timezone-aware value")
    utc = at.utctimetuple()
    if utc.tm_wday >= 5:
        return False
    return 1 <= utc.tm_hour < 4 or 6 <= utc.tm_hour < 10


def cost_usd(model: str, usage: Usage, at: datetime) -> Decimal:
    """Cost of one call made at ``at``. Unknown models (a local vLLM, say) cost 0."""
    price = PRICES.get(model)
    if price is None:
        return Decimal(0)
    total = (
        usage.cache_hit_tokens * price.cache_hit
        + usage.cache_miss_tokens * price.cache_miss
        + usage.output_tokens * price.output
    ) / MILLION
    return total * PEAK_MULTIPLIER if is_peak(at) else total
