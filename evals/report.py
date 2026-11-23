"""Turn a run directory's results into ``summary.json`` and ``report.md``."""

import json
from pathlib import Path
from typing import Any

from evals.judge import RUBRICS
from evals.metrics import Rate, Summary, summarize
from evals.results import ScenarioRun
from evals.scenario import TARGET_COUNTS, Scenario

RESULTS = "results.jsonl"
SUMMARY = "summary.json"
REPORT = "report.md"
RUN_INFO = "run.json"

TARGETS = {
    "task_success": 0.85,
    "emergency_handling": 0.98,
    "escalation_correct": 0.90,
}


def load_runs(run_dir: Path) -> list[ScenarioRun]:
    path = run_dir / RESULTS
    if not path.exists():
        return []
    return [
        ScenarioRun.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value / 1000:.1f} s"


def _target(rate: Rate, target: float) -> str:
    if rate.value is None:
        return ""
    if rate.low >= target:
        return "meets target"
    if rate.high < target:
        return "below target"
    return "inconclusive: the interval spans the target"


def render(summary: Summary, info: dict[str, Any]) -> str:
    s = summary
    lines = [f"# Eval run {info.get('run_id', '')}", ""]
    rows = [
        ("Model", info.get("model")),
        ("Prompt version", info.get("prompt_version")),
        ("Split", info.get("split")),
        ("Milestone", info.get("milestone") or "none (dev iteration)"),
        ("Repeats per scenario", info.get("repeat")),
        ("Simulated customer", info.get("sim_model")),
        ("Judge", info.get("judge_model") or "off"),
        ("Git commit", info.get("git_commit")),
        ("Started", info.get("started_at")),
    ]
    lines += ["| | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows] + [""]
    lines += [
        f"{s.runs} runs: {s.graded} graded, {s.excluded_simulator} excluded because the "
        f"simulated customer broke character, {s.excluded_harness} harness errors.",
        "",
        "## Headline metrics",
        "",
        "| Metric | Result | Target |",
        "|---|---|---|",
        f"| Task success (booking, reschedule, cancel) | {s.task_success} | "
        f">= 85%, {_target(s.task_success, TARGETS['task_success'])} |",
        f"| Emergency handling | {s.emergency_handling} | "
        f">= 98%, {_target(s.emergency_handling, TARGETS['emergency_handling'])} |",
        f"| Correct escalation | {s.escalation_correct} | "
        f">= 90%, {_target(s.escalation_correct, TARGETS['escalation_correct'])} |",
        f"| Double bookings | {s.double_bookings} | 0 |",
        f"| Cost per conversation (agent) | mean {_money(s.cost_per_conversation['mean'])}, "
        f"p50 {_money(s.cost_per_conversation['p50'])}, "
        f"p95 {_money(s.cost_per_conversation['p95'])} | < $0.01 |",
        "",
        "## More metrics",
        "",
        f"- Overall pass rate: {s.overall}",
        f"- Escalation precision: {s.escalation_precision}; recall: {s.escalation_recall}",
        f"- Emergency false positives on look-alikes: {s.lookalike_false_positive}",
        f"- `slot_unavailable` tool results (lost races or stale offers): "
        f"{s.slot_unavailable_results}",
        "- Customer messages per conversation: "
        + (f"{s.avg_customer_messages:.1f}" if s.avg_customer_messages is not None else "n/a"),
        f"- Agent latency per turn: p50 {_ms(s.latency_per_turn_ms['p50'])}, "
        f"p95 {_ms(s.latency_per_turn_ms['p95'])}; per conversation: "
        f"p50 {_ms(s.latency_per_conversation_ms['p50'])}, "
        f"p95 {_ms(s.latency_per_conversation_ms['p95'])}",
        "- Prompt cache hit rate: "
        + (f"{s.cache_hit_rate:.0%}" if s.cache_hit_rate is not None else "n/a"),
        f"- Spend: agent {_money(s.cost_total['agent'])}, simulated customer "
        f"{_money(s.cost_total['simulated_customer'])}, judge {_money(s.cost_total['judge'])}, "
        f"total {_money(s.cost_total['all'])}",
        "",
        "## By category",
        "",
        "| Category | Pass rate | Scenarios in the full set |",
        "|---|---|---|",
    ]
    for category, rate in s.by_category.items():
        target = next((n for c, n in TARGET_COUNTS.items() if c.value == category), "")
        lines.append(f"| {category} | {rate} | {target} |")
    lines.append("")

    if s.emergency_misses:
        lines += ["## Emergency misses", ""] + [f"- {i}" for i in s.emergency_misses] + [""]
    if s.lookalike_false_positives:
        lines += ["## Look-alike false positives", ""]
        lines += [f"- {i}" for i in s.lookalike_false_positives] + [""]
    if s.judge:
        lines += [
            "## Judge (uncalibrated until checked against human labels)",
            "",
            "| Question | Yes rate |",
            "|---|---|",
        ]
        lines += [f"| {k}: {RUBRICS.get(k, '')} | {v} |" for k, v in s.judge.items()] + [""]
    if s.flaky:
        lines += ["## Flaky scenarios (mixed results across repeats)", ""]
        lines += [f"- {sid}: {result}" for sid, result in s.flaky.items()] + [""]
    if s.failures:
        lines += ["## Failures", ""]
        for run_id, problems in s.failures.items():
            lines.append(f"- **{run_id}**")
            lines += [f"  - {p}" for p in problems]
        lines.append("")
    return "\n".join(lines)


def write(run_dir: Path, scenarios: list[Scenario]) -> Summary:
    runs = load_runs(run_dir)
    info_path = run_dir / RUN_INFO
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    summary = summarize(runs, {s.id: s for s in scenarios})
    (run_dir / SUMMARY).write_text(
        json.dumps(summary.as_dict(), indent=2, default=str), encoding="utf-8"
    )
    (run_dir / REPORT).write_text(render(summary, info), encoding="utf-8")
    return summary
