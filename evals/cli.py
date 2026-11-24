"""Command line for the eval harness. See ``python -m evals --help``."""

import argparse
import asyncio
import json
import random
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from app.agent.factory import agent_config, is_deepseek, make_provider
from app.agent.prompts import prompt_version
from app.agent.providers.base import LLMError, LLMProvider
from app.agent.providers.openai_compat import OpenAICompatibleProvider
from app.config import Settings, get_settings
from app.db.session import make_engine, make_sessionmaker
from evals import db, report
from evals.graders import grade
from evals.judge import RUBRICS, SIMULATOR_RUBRIC, Judge, render_transcript
from evals.metrics import Summary
from evals.results import ScenarioRun
from evals.runner import EvalEnv, run_scenario
from evals.scenario import (
    TARGET_COUNTS,
    Category,
    Scenario,
    ScenarioError,
    load_all,
    select,
    validate,
)

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "evals" / "runs"
LABELS_DIR = ROOT / "evals" / "labels"
# Upper bound on the all-in cost of one conversation (agent + simulated customer + judge). Five
# full Flash runs (2026-11-23/24) cost $0.00062-0.00065 each; this is about 3x that. Pro is 3x
# Flash, as in DESIGN section 8. Only used to refuse runs that would obviously blow the budget;
# the live spend check is what actually stops a run (D-042).
ESTIMATE_PER_CONVERSATION = {
    "deepseek-flash": Decimal("0.002"),
    "deepseek-v4-pro": Decimal("0.006"),
}


# --------------------------------------------------------------------------------------------
# validate


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        scenarios = load_all()
    except ScenarioError as exc:
        print(f"Invalid scenario file: {exc}")
        return 1
    problems = validate(scenarios)
    print(f"{'category':<22}{'dev':>5}{'test':>6}{'total':>7}{'target':>8}")
    for category in Category:
        members = [s for s in scenarios if s.category == category]
        dev = sum(1 for s in members if s.split == "dev")
        print(
            f"{category:<22}{dev:>5}{len(members) - dev:>6}{len(members):>7}"
            f"{TARGET_COUNTS[category]:>8}"
        )
    print(f"{'all':<22}{'':>5}{'':>6}{len(scenarios):>7}{sum(TARGET_COUNTS.values()):>8}")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if not problems:
        print("All scenarios are consistent with the seed business.")
    return 1 if problems else 0


# --------------------------------------------------------------------------------------------
# run


@dataclass
class RunPlan:
    scenarios: list[Scenario]
    repeat: int
    settings: Settings
    sim_model: str
    judge_model: str | None
    concurrency: int
    max_cost: Decimal
    run_dir: Path


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _side_provider(settings: Settings, temperature: float) -> OpenAICompatibleProvider:
    """For the simulated customer and the judge: same endpoint, own temperature, no thinking."""
    extra: dict[str, Any] = {}
    if is_deepseek(settings.llm_base_url):
        extra["thinking"] = {"type": "disabled"}
    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_key,
        temperature=temperature,
        max_tokens=600,
        extra_body=extra,
        require_key=is_deepseek(settings.llm_base_url),
    )


async def _preflight(provider: LLMProvider, model: str) -> str | None:
    try:
        await provider.complete(
            model=model, messages=[{"role": "user", "content": "Reply with the word ok."}], tools=[]
        )
    except LLMError as exc:
        return str(exc)
    return None


def _status_line(index: int, total: int, run: ScenarioRun) -> str:
    if run.harness_error:
        status = "HARNESS ERROR " + run.harness_error.splitlines()[0][:100]
    elif run.simulator_broke:
        status = "EXCLUDED (simulator) " + run.simulator_broke[:80]
    elif run.passed:
        status = "PASS"
    else:
        first = run.failed_checks()[0]
        status = f"FAIL {first.name}: {first.detail[:90]}"
    return (
        f"[{index:>3}/{total}] {run.scenario_id}#{run.repeat:<2} {status}  "
        f"({len(run.exchanges)} msgs, ${run.total_cost_usd:.4f}, {run.wall_seconds:.0f}s)"
    )


async def execute(plan: RunPlan) -> list[ScenarioRun]:
    settings = plan.settings
    engine = make_engine(settings.database_url, pool_size=plan.concurrency * 2, max_overflow=10)
    sessionmaker = make_sessionmaker(engine)
    agent_provider = make_provider(settings)
    sim_provider = _side_provider(settings, temperature=0.7)
    judge_provider = _side_provider(settings, temperature=0.0)
    judge = Judge(judge_provider, plan.judge_model) if plan.judge_model else None
    env = EvalEnv(
        sessionmaker=sessionmaker,
        agent_provider=agent_provider,
        agent_config=agent_config(settings),
        sim_provider=sim_provider,
        sim_model=plan.sim_model,
    )
    jobs = [(s, k) for k in range(plan.repeat) for s in plan.scenarios]
    results: list[ScenarioRun] = []
    spent = Decimal(0)
    stopped = False
    lock = asyncio.Lock()
    gate = asyncio.Semaphore(plan.concurrency)
    out = (plan.run_dir / report.RESULTS).open("a", encoding="utf-8")

    async def one(scenario: Scenario, repeat: int) -> None:
        nonlocal spent, stopped
        async with gate:
            if spent >= plan.max_cost:
                stopped = True
                return
            run = await run_scenario(env, scenario, repeat)
            run.checks = grade(run, scenario)
            if judge is not None and run.harness_error is None:
                await judge.grade(run)
            async with lock:
                spent += run.total_cost_usd
                results.append(run)
                out.write(run.model_dump_json() + "\n")
                out.flush()
                print(_status_line(len(results), len(jobs), run), flush=True)

    try:
        await asyncio.gather(*(one(s, k) for s, k in jobs))
    finally:
        out.close()
        for provider in (agent_provider, sim_provider, judge_provider):
            await provider.aclose()
        await engine.dispose()
    if stopped:
        print(f"Stopped early: spent ${spent:.4f}, the cap is ${plan.max_cost}.")
    return results


def cmd_run(args: argparse.Namespace) -> int:
    try:
        scenarios = load_all()
    except ScenarioError as exc:
        print(f"Invalid scenario file: {exc}")
        return 1
    problems = validate(scenarios)
    if problems:
        print("Fix the scenario files first (python -m evals validate):")
        print("\n".join(f"  {p}" for p in problems[:20]))
        return 1
    if args.split == "test" and not args.milestone:
        print(
            "The test split runs only at milestones, so prompts aren't tuned against it (D-018). "
            "Pass --milestone NAME to confirm this is one."
        )
        return 2
    chosen = select(scenarios, split=args.split, categories=args.category, ids=args.id)
    if not chosen:
        print("No scenarios match.")
        return 2

    settings = get_settings().model_copy(
        update={"database_url": db.eval_database_url()}
        | ({"llm_model": args.model} if args.model else {})
    )
    if settings.llm_key is None and is_deepseek(settings.llm_base_url):
        print("DEEPSEEK_API_KEY is not set (put it in .env).")
        return 2
    sim_model = args.sim_model or settings.llm_model
    judge_model = None if args.no_judge else (args.judge_model or settings.llm_model)

    conversations = len(chosen) * args.repeat
    per = ESTIMATE_PER_CONVERSATION.get(settings.llm_model, Decimal("0.03"))
    estimate = per * conversations
    max_cost = Decimal(str(args.max_cost))
    print(
        f"{conversations} conversations ({len(chosen)} scenarios x {args.repeat}) on "
        f"{settings.llm_model}; estimated at most ${estimate:.2f}, cap ${max_cost}."
    )
    if estimate > max_cost:
        print("The estimate is over the cap. Raise --max-cost or run fewer scenarios.")
        return 2

    started = datetime.now(UTC)
    run_id = started.strftime("%Y%m%d-%H%M%S") + (f"-{args.name}" if args.name else "")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    info: dict[str, Any] = {
        "run_id": run_id,
        "model": settings.llm_model,
        "temperature": settings.llm_temperature,
        "thinking": settings.llm_thinking,
        "prompt_version": prompt_version(),
        "split": "ids" if args.id else args.split,
        "categories": args.category or [],
        "milestone": args.milestone,
        "repeat": args.repeat,
        "sim_model": sim_model,
        "judge_model": judge_model,
        "concurrency": args.concurrency,
        "max_cost_usd": str(max_cost),
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "started_at": started.isoformat(timespec="seconds"),
        "scenarios": [s.id for s in chosen],
    }
    (run_dir / report.RUN_INFO).write_text(json.dumps(info, indent=2), encoding="utf-8")

    print(f"Resetting the eval database ({db.eval_database_url().rsplit('/', 1)[-1]}) ...")
    db.reset(settings.database_url)

    async def go() -> list[ScenarioRun]:
        probe = make_provider(settings)
        try:
            problem = await _preflight(probe, settings.llm_model)
        finally:
            await probe.aclose()
        if problem:
            raise SystemExit(f"Can't reach the LLM at {settings.llm_base_url}: {problem}")
        return await execute(
            RunPlan(
                scenarios=chosen,
                repeat=args.repeat,
                settings=settings,
                sim_model=sim_model,
                judge_model=judge_model,
                concurrency=args.concurrency,
                max_cost=max_cost,
                run_dir=run_dir,
            )
        )

    asyncio.run(go())
    info["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    (run_dir / report.RUN_INFO).write_text(json.dumps(info, indent=2), encoding="utf-8")
    summary = report.write(run_dir, scenarios)
    print_headline(summary)
    print(f"Report: {(run_dir / report.REPORT).relative_to(ROOT)}")
    return 0


def print_headline(s: Summary) -> None:
    print()
    print(f"Task success        {s.task_success}")
    print(f"Emergency handling  {s.emergency_handling}")
    print(f"Correct escalation  {s.escalation_correct}")
    print(f"Double bookings     {s.double_bookings}")
    mean = s.cost_per_conversation["mean"]
    print(f"Cost/conversation   {'n/a' if mean is None else f'${mean:.4f}'} (agent, mean)")
    print(f"Overall             {s.overall}")
    if s.excluded_simulator or s.excluded_harness:
        print(f"Excluded            {s.excluded_simulator} simulator, {s.excluded_harness} harness")


# --------------------------------------------------------------------------------------------
# report, label, calibrate


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    scenarios = load_all()
    if args.regrade:
        if not args.note:
            print("--regrade needs --note saying why (which grader was wrong).")
            return 2
        note = (
            f"{datetime.now(UTC):%Y-%m-%d} at {_git('rev-parse', '--short', 'HEAD')}: {args.note}"
        )
        changed = report.regrade(run_dir, scenarios, note)
        print(f"Regraded: {', '.join(changed) or 'no run changed'}")
    summary = report.write(run_dir, scenarios)
    print_headline(summary)
    print(f"Report: {run_dir / report.REPORT}")
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    """Sample judged transcripts for a person to label, stratified by category. The file
    deliberately leaves out the judge's answers, so they can't anchor the labels."""
    run_dir = Path(args.run_dir)
    runs = [r for r in report.load_runs(run_dir) if r.judge and not r.harness_error]
    by_category: dict[str, list[ScenarioRun]] = {}
    for r in runs:
        by_category.setdefault(r.category, []).append(r)
    rng = random.Random(args.seed)
    for members in by_category.values():
        rng.shuffle(members)
    sample: list[ScenarioRun] = []
    while len(sample) < args.n and any(by_category.values()):
        for members in by_category.values():
            if members and len(sample) < args.n:
                sample.append(members.pop())
    items = [
        {
            "run": f"{r.scenario_id}#{r.repeat}",
            "transcript": render_transcript(r).splitlines(),
            "labels": dict.fromkeys(RUBRICS),
        }
        for r in sample
    ]
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else LABELS_DIR / f"{run_dir.name}.yaml"
    document = {
        "run_dir": str(run_dir),
        "instructions": "Answer every label yes or no from the transcript alone.",
        "questions": RUBRICS,
        "items": items,
    }
    out.write_text(yaml.safe_dump(document, sort_keys=False, width=100), encoding="utf-8")
    print(f"Wrote {len(items)} transcripts to label: {out}")
    return 0


def kappa(pairs: Sequence[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa for two raters on yes/no answers."""
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(a == b for a, b in pairs) / n
    yes_a = sum(a for a, _ in pairs) / n
    yes_b = sum(b for _, b in pairs) / n
    expected = yes_a * yes_b + (1 - yes_a) * (1 - yes_b)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def cmd_calibrate(args: argparse.Namespace) -> int:
    document = yaml.safe_load(Path(args.labels).read_text(encoding="utf-8"))
    run_dir = Path(document["run_dir"])
    judged = {f"{r.scenario_id}#{r.repeat}": r for r in report.load_runs(run_dir)}
    pairs: dict[str, list[tuple[bool, bool]]] = {key: [] for key in RUBRICS}
    for item in document["items"]:
        run = judged.get(item["run"])
        if run is None:
            continue
        for key, human in (item.get("labels") or {}).items():
            if (
                key in pairs
                and key in run.judge
                and str(human).lower() in ("yes", "no", "true", "false")
            ):
                pairs[key].append((str(human).lower() in ("yes", "true"), run.judge[key].answer))
    result = {}
    print(f"{'question':<24}{'n':>4}{'agree':>8}{'kappa':>8}")
    for key, values in pairs.items():
        agree = sum(a == b for a, b in values) / len(values) if values else None
        k = kappa(values)
        result[key] = {"n": len(values), "agreement": agree, "kappa": k}
        print(
            f"{key:<24}{len(values):>4}"
            f"{'' if agree is None else f'{agree:.0%}':>8}{'' if k is None else f'{k:.2f}':>8}"
        )
    (run_dir / "calibration.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        f"Saved {run_dir / 'calibration.json'}. Drop any question whose agreement is poor "
        f"from the judge (D-018). '{SIMULATOR_RUBRIC}' checks the simulator, not the agent."
    )
    return 0


# --------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="check every scenario file; no DB or LLM needed")

    run = sub.add_parser("run", help="run scenarios against the real model")
    run.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    run.add_argument("--category", action="append", choices=[c.value for c in Category])
    run.add_argument("--id", action="append", help="scenario id (repeatable; ignores --split)")
    run.add_argument("--repeat", type=int, default=1, help="runs per scenario (k)")
    run.add_argument("--model", help="agent model, overriding LLM_MODEL")
    run.add_argument("--sim-model", help="simulated customer model (default: the agent's)")
    run.add_argument("--judge-model", help="judge model (default: the agent's)")
    run.add_argument("--no-judge", action="store_true")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--max-cost", type=float, default=2.0, help="USD; stops starting new runs")
    run.add_argument("--milestone", help="required for --split test")
    run.add_argument("--name", help="suffix for the run directory")

    rep = sub.add_parser("report", help="rebuild summary.json and report.md for a run")
    rep.add_argument("run_dir")
    rep.add_argument(
        "--regrade", action="store_true", help="re-grade stored traces with current expectations"
    )
    rep.add_argument("--note", help="why the regrade (required with --regrade)")

    label = sub.add_parser("label", help="sample transcripts for human labels")
    label.add_argument("run_dir")
    label.add_argument("--n", type=int, default=40)
    label.add_argument("--seed", type=int, default=7)
    label.add_argument("--out")

    cal = sub.add_parser("calibrate", help="judge vs human agreement for a labels file")
    cal.add_argument("labels")
    return p


COMMANDS = {
    "validate": cmd_validate,
    "run": cmd_run,
    "report": cmd_report,
    "label": cmd_label,
    "calibrate": cmd_calibrate,
}


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    return COMMANDS[args.command](args)
