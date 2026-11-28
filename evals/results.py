"""What one scenario run records. Stored as JSON lines, so a report can be rebuilt (and metric
definitions changed) without re-running anything."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

# Agent turn outcomes that mean the provider failed (an error after the retry, or the turn's time
# budget spent waiting on it), not that the agent decided badly.
INFRA_OUTCOMES = frozenset({"llm_error", "time_limit"})
SIM_CALL_FAILED = "simulator call failed"


class _Record(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Exchange(_Record):
    """One customer message and everything the business sent back before the next one."""

    customer: str
    replies: list[str]
    handled_by: str
    at: datetime  # scenario clock, local
    mode_after: str  # conversation mode once the turn finished: bot or handoff


class MessageRecord(_Record):
    direction: str
    author: str
    body: str
    status: str


class BookingRecord(_Record):
    ref: str
    label: str | None  # fixture label; None for bookings made during the conversation
    service: str
    trade: str
    start: datetime  # local
    end: datetime
    status: str
    urgency: str
    address: str
    zip: str
    original_start: datetime | None = None  # fixture bookings: where it started


class EscalationRecord(_Record):
    reason: str
    hazard: str | None
    source: str
    mode: str
    status: str
    summary: str


class ToolTrace(_Record):
    turn: int  # 0-based index of the agent turn
    name: str
    arguments: dict[str, Any]
    status: str
    error: str | None  # the error code in the result, if any


class TurnRecord(_Record):
    outcome: str | None
    latency_ms: int | None
    llm_calls: int
    cost_usd: Decimal
    input_tokens: int
    cache_hit_tokens: int
    output_tokens: int


class CheckResult(_Record):
    name: str
    passed: bool
    detail: str = ""


class JudgeAnswer(_Record):
    answer: bool
    reason: str = ""


class ScenarioRun(_Record):
    scenario_id: str
    category: str
    split: str
    title: str
    tags: list[str] = []
    repeat: int = 0
    customer_kind: str = "scripted"
    # How the conversation ended: script_end, done, max_turns, no_reply, broke, sim_error.
    end_reason: str = ""
    # Set when the simulated customer broke character; the run is excluded from metrics.
    simulator_broke: str | None = None
    # Set when the harness itself failed (fixture, crash); also excluded, and reported.
    harness_error: str | None = None
    # Set when the simulated customer's LLM call failed (network, provider). See ``outage``.
    infra_error: str | None = None

    exchanges: list[Exchange] = []
    messages: list[MessageRecord] = []
    bookings: list[BookingRecord] = []
    escalations: list[EscalationRecord] = []
    tool_calls: list[ToolTrace] = []
    turns: list[TurnRecord] = []
    opted_out: bool = False
    double_bookings: int = 0  # overlapping confirmed bookings on one technician: always 0

    sim_calls: int = 0
    sim_cost_usd: Decimal = Decimal(0)
    judge_cost_usd: Decimal = Decimal(0)
    wall_seconds: float = 0.0

    checks: list[CheckResult] = []
    judge: dict[str, JudgeAnswer] = {}
    judge_error: str | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def outage(self) -> str | None:
        """Why this run says nothing about the agent's quality because the provider or the
        network failed: the simulated customer's call failed, or an agent turn ended in an LLM
        error or ran out of time. Computed from what the run stored, so older runs (whose
        simulator failures were recorded as ``simulator_broke``) are classified too."""
        if self.infra_error:
            return self.infra_error
        if self.simulator_broke and self.simulator_broke.startswith(SIM_CALL_FAILED):
            return self.simulator_broke
        failed = [t.outcome for t in self.turns if t.outcome in INFRA_OUTCOMES]
        return f"agent turn ended in {failed[0]}" if failed else None

    @property
    def excluded(self) -> bool:
        return (
            self.simulator_broke is not None
            or self.harness_error is not None
            or self.outage is not None
        )

    @property
    def agent_cost_usd(self) -> Decimal:
        return sum((t.cost_usd for t in self.turns), Decimal(0))

    @property
    def agent_latency_ms(self) -> int:
        return sum(t.latency_ms or 0 for t in self.turns)

    @property
    def total_cost_usd(self) -> Decimal:
        return self.agent_cost_usd + self.sim_cost_usd + self.judge_cost_usd

    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]
