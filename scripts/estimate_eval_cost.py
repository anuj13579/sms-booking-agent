"""Back-of-envelope eval cost estimate used in docs/DESIGN.md section 8.

The token assumptions are guesses until Phase 3 logs real usage. At that point,
replace them with measured values from the llm_calls table.

Prices: DeepSeek pricing page, fetched 2026-09-26, USD per 1M tokens.
Off-peak is half of peak. Peak = 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri.
"""

PRICES = {
    "flash_peak": {"hit": 0.006, "miss": 0.30, "out": 1.20},
    "flash_offpeak": {"hit": 0.003, "miss": 0.15, "out": 0.60},
    "pro_peak": {"hit": 0.044, "miss": 1.32, "out": 3.96},
    "pro_offpeak": {"hit": 0.022, "miss": 0.66, "out": 1.98},
}

# Per-conversation token assumptions: (input tokens, cache-hit fraction, output tokens)
AGENT = (13 * 4000, 0.75, 13 * 150)  # ~7 customer turns, ~1.8 LLM calls per turn
SIMULATED_CUSTOMER = (7 * 1500, 0.60, 7 * 60)
JUDGE = (3000, 0.0, 300)

DEV_SPLIT, FULL_SUITE = 60, 250


def call_cost(price: dict, input_tokens: int, hit_fraction: float, output_tokens: int) -> float:
    hit = input_tokens * hit_fraction * price["hit"]
    miss = input_tokens * (1 - hit_fraction) * price["miss"]
    out = output_tokens * price["out"]
    return (hit + miss + out) / 1_000_000


def per_conversation(agent_price_key: str) -> float:
    offpeak = agent_price_key.endswith("offpeak")
    helper = PRICES["flash_offpeak" if offpeak else "flash_peak"]  # simulator + judge always Flash
    return (
        call_cost(PRICES[agent_price_key], *AGENT)
        + call_cost(helper, *SIMULATED_CUSTOMER)
        + call_cost(helper, *JUDGE)
    )


if __name__ == "__main__":
    print(f"{'agent model':15s} {'per conv':>9s} {'dev (60)':>9s} {'full (250)':>11s}")
    for key in PRICES:
        c = per_conversation(key)
        print(f"{key:15s} ${c:8.4f} ${c * DEV_SPLIT:8.2f} ${c * FULL_SUITE:10.2f}")
