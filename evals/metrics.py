"""Metrics over a run's results (DESIGN section 9). Every rate comes with a Wilson 95% interval,
because with 15 to 50 scenarios per category the point estimate alone overstates what we know.

Runs where the simulated customer broke character, the harness itself failed, or the provider or
network failed (an outage) are excluded from every rate and counted separately. Outages are about
infrastructure, not the agent's decisions: they get their own reliability number instead.
"""

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from app.domain.enums import EscalationReason, EscalationSource
from evals.graders import SAFETY_TEXTS
from evals.results import INFRA_OUTCOMES, ScenarioRun
from evals.scenario import (
    EMERGENCY_CATEGORIES,
    TASK_CATEGORIES,
    Category,
    Scenario,
)

Z95 = 1.959963984540054


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval. Unlike the normal approximation it stays inside [0, 1] and behaves
    at 0/n and n/n, which is exactly where the safety metrics live."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass
class Rate:
    successes: int
    n: int
    low: float = 0.0
    high: float = 1.0

    @classmethod
    def of(cls, outcomes: Iterable[bool]) -> "Rate":
        values = list(outcomes)
        successes = sum(values)
        low, high = wilson(successes, len(values))
        return cls(successes, len(values), low, high)

    @property
    def value(self) -> float | None:
        return self.successes / self.n if self.n else None

    def __str__(self) -> str:
        if not self.n:
            return "n/a"
        return (
            f"{self.value:.1%} ({self.successes}/{self.n}, 95% CI {self.low:.1%}-{self.high:.1%})"
        )


@dataclass
class Summary:
    runs: int
    graded: int
    excluded_simulator: int
    excluded_harness: int
    excluded_outage: int
    # Agent turns that got an answer from the provider in time, over all agent turns of all runs.
    provider_turns: Rate
    outage_runs: list[str]  # "scenario#repeat", to re-run
    overall: Rate
    by_category: dict[str, Rate]
    task_success: Rate
    emergency_handling: Rate
    emergency_misses: list[str]
    lookalike_false_positive: Rate
    lookalike_false_positives: list[str]
    escalation_correct: Rate
    escalation_precision: Rate
    escalation_recall: Rate
    double_bookings: int
    slot_unavailable_results: int
    avg_customer_messages: float | None
    cost_per_conversation: dict[str, float | None]
    latency_per_turn_ms: dict[str, float | None]
    latency_per_conversation_ms: dict[str, float | None]
    cache_hit_rate: float | None
    cost_total: dict[str, float]
    flaky: dict[str, str] = field(default_factory=dict)  # scenario id -> "2/3 passed"
    judge: dict[str, Rate] = field(default_factory=dict)
    failures: dict[str, list[str]] = field(default_factory=dict)  # scenario id -> failed checks

    def as_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Rate):
                return {**asdict(value), "value": value.value}
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            return value

        return {key: convert(value) for key, value in self.__dict__.items()}


def _escalated(run: ScenarioRun) -> bool:
    return any(e.source != EscalationSource.SYSTEM for e in run.escalations)


def _emergency_raised(run: ScenarioRun) -> bool:
    return any(e.reason == EscalationReason.EMERGENCY for e in run.escalations) or any(
        text in m.body for m in run.messages for text in SAFETY_TEXTS.values()
    )


def _escalation_checks_pass(run: ScenarioRun) -> bool:
    names = ("escalated", "not_escalated", "no_forbidden_escalation", "emergency_hazard")
    return all(c.passed for c in run.checks if c.name in names)


def _distribution(values: list[float]) -> dict[str, float | None]:
    return {
        "mean": sum(values) / len(values) if values else None,
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def summarize(runs: list[ScenarioRun], scenarios: dict[str, Scenario]) -> Summary:
    graded = [r for r in runs if not r.excluded]

    def rate(predicate: Callable[[ScenarioRun], bool], pool: list[ScenarioRun]) -> Rate:
        return Rate.of(predicate(r) for r in pool)

    by_category = {}
    for category in Category:
        pool = [r for r in graded if r.category == category]
        if pool:
            by_category[category.value] = rate(lambda r: r.passed, pool)

    emergencies = [r for r in graded if r.category in EMERGENCY_CATEGORIES]
    lookalikes = [r for r in graded if r.category == Category.EMERGENCY_LOOKALIKE]
    escalation_graded = [
        r for r in graded if r.scenario_id in scenarios
        and scenarios[r.scenario_id].expect.escalate is not None
    ]  # fmt: skip
    should = [r for r in escalation_graded if scenarios[r.scenario_id].expect.escalate]
    did = [r for r in escalation_graded if _escalated(r)]

    turns = [t for r in graded for t in r.turns]
    input_tokens = sum(t.input_tokens for t in turns)
    by_scenario: dict[str, list[bool]] = defaultdict(list)
    for r in graded:
        by_scenario[r.scenario_id].append(r.passed)

    judge: dict[str, Rate] = {}
    rubrics = sorted({name for r in graded for name in r.judge})
    for name in rubrics:
        judge[name] = Rate.of(r.judge[name].answer for r in graded if name in r.judge)

    def total(values: Iterable[Decimal]) -> float:
        return float(sum(values, Decimal(0)))

    return Summary(
        runs=len(runs),
        graded=len(graded),
        excluded_simulator=sum(
            1 for r in runs if r.simulator_broke and not r.harness_error and not r.outage
        ),
        excluded_harness=sum(1 for r in runs if r.harness_error),
        excluded_outage=sum(1 for r in runs if r.outage and not r.harness_error),
        provider_turns=Rate.of(
            t.outcome not in INFRA_OUTCOMES for r in runs for t in r.turns if t.outcome
        ),
        outage_runs=[
            f"{r.scenario_id}#{r.repeat}" for r in runs if r.outage and not r.harness_error
        ],
        overall=rate(lambda r: r.passed, graded),
        by_category=by_category,
        task_success=rate(lambda r: r.passed, [r for r in graded if r.category in TASK_CATEGORIES]),
        emergency_handling=rate(lambda r: r.passed, emergencies),
        emergency_misses=sorted({r.scenario_id for r in emergencies if not r.passed}),
        lookalike_false_positive=rate(_emergency_raised, lookalikes),
        lookalike_false_positives=sorted(
            {r.scenario_id for r in lookalikes if _emergency_raised(r)}
        ),
        escalation_correct=rate(_escalation_checks_pass, escalation_graded),
        escalation_precision=rate(lambda r: id(r) in {id(s) for s in should}, did),
        escalation_recall=rate(_escalated, should),
        double_bookings=sum(r.double_bookings for r in runs),
        slot_unavailable_results=sum(
            1 for r in runs for c in r.tool_calls if c.error == "slot_unavailable"
        ),
        avg_customer_messages=(
            sum(len(r.exchanges) for r in graded) / len(graded) if graded else None
        ),
        cost_per_conversation=_distribution([float(r.agent_cost_usd) for r in graded]),
        latency_per_turn_ms=_distribution(
            [float(t.latency_ms) for t in turns if t.latency_ms is not None]
        ),
        latency_per_conversation_ms=_distribution([float(r.agent_latency_ms) for r in graded]),
        cache_hit_rate=(
            sum(t.cache_hit_tokens for t in turns) / input_tokens if input_tokens else None
        ),
        cost_total={
            "agent": total(r.agent_cost_usd for r in runs),
            "simulated_customer": total(r.sim_cost_usd for r in runs),
            "judge": total(r.judge_cost_usd for r in runs),
            "all": total(r.total_cost_usd for r in runs),
        },
        flaky={
            sid: f"{sum(results)}/{len(results)} passed"
            for sid, results in sorted(by_scenario.items())
            if 0 < sum(results) < len(results)
        },
        judge=judge,
        failures={
            f"{r.scenario_id}#{r.repeat}": [f"{c.name}: {c.detail}" for c in r.failed_checks()]
            for r in graded
            if not r.passed
        },
    )
