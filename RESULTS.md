# Eval results

Every eval run worth keeping, newest first, with what it measured and what it found. How the harness works is in [DESIGN §9](docs/DESIGN.md); what each metric counts is in [D-039](DECISIONS.md). Full run outputs (every transcript, tool call, and check) stay in `evals/runs/`, which is git-ignored, so this file is the record.

**How to read the numbers.** Every rate comes with a Wilson 95% confidence interval. A target counts as met only when the whole interval clears it. When the interval spans the target, the verdict is "inconclusive", even at 100%. The dev split is for iteration. The held-out test split (76 scenarios) runs only at milestones (D-042) and hasn't been run yet.

## Run log

| Run | Date | Split | Model, prompt | Overall | Task success | Emergency | Escalation | Cost per conv. | Spend |
|---|---|---|---|---|---|---|---|---|---|
| [dev-baseline](#2026-11-23-dev-baseline) | 2026-11-23 | dev, 174 | flash, `system_v1+9c60bae1` | 94.8% | 96.9% | 100% (25/25) | 96.8% | $0.0004 | $0.11 |
| first-live-rerun | 2026-11-22 | 16 dev ids | flash, `system_v1+9c60bae1` | 93.8% | 100% (6/6) | 100% (3/3) | 100% (13/13) | $0.0002 | $0.007 |
| first-live | 2026-11-22 | 16 dev ids | flash, `system_v1+9c60bae1` | (harness bug) | | | | | $0.010 |

The two 16-scenario runs were harness checks, not measurements. The first found a harness bug: the judge excluded a scripted prompt-injection scenario as "out of character". It was fixed, and the run was repeated.

---

## 2026-11-23 dev baseline

The first full measurement: the starting point for Phase 4.

| | |
|---|---|
| Run id | `20261123-163418-dev-baseline` |
| Code | `d66fe36`, clean tree |
| Agent | `deepseek-flash`, non-thinking, temperature 0.3, prompt `system_v1+9c60bae1` |
| Simulated customer and judge | `deepseek-flash` |
| Scenarios | the dev split, 174, one run each |
| Excluded | none (no harness errors, no simulator breaking character) |

### Headline metrics

| Metric | Result | Target | Verdict |
|---|---|---|---|
| Task success (booking, reschedule, cancel) | 96.9% (63/65, CI 89.5–99.2%) | ≥ 85% | meets |
| Emergency handling | 100% (25/25, CI 86.7–100%) | ≥ 98% | inconclusive |
| Correct escalation | 96.8% (153/158, CI 92.8–98.6%) | ≥ 90% | meets |
| Double bookings | 0 | 0 | meets |
| Cost per conversation (agent) | mean $0.0004, p95 $0.0008 | < $0.01 | meets |

Emergency handling can't be confirmed from 25 scenarios run once: even a perfect score leaves the interval's low end at 86.7%. That needs repeats or the test split.

### Other metrics

- Overall pass rate: 94.8% (165/174, CI 90.5–97.3%).
- Escalation precision 98.2% (55/56); recall 94.8% (55/58).
- Emergency false positives on look-alikes: 18.2% (2/11, CI 5.1–47.7%).
- Agent latency per turn: p50 1.9 s, p95 3.1 s. Per conversation: p50 4.7 s, p95 10.2 s.
- Prompt cache hit rate: 93%.
- 2.9 customer messages per conversation on average.
- Spend: agent $0.063, simulated customer $0.015, judge $0.030, total $0.108. That is about a twentieth of the planned $0.01 per conversation (DESIGN §8), mainly because conversations were shorter than the 7 turns the estimate assumed.

### By category

| Category | Pass rate |
|---|---|
| normal_booking | 97.1% (33/34) |
| ambiguous | 95.8% (23/24) |
| reschedule | 100% (17/17) |
| cancel | 92.9% (13/14) |
| angry | 100% (14/14) |
| clear_emergency | 100% (14/14) |
| subtle_emergency | 100% (11/11) |
| emergency_lookalike | 72.7% (8/11) |
| price | 92.9% (13/14) |
| off_topic | 85.7% (12/14) |
| opt_out | 100% (7/7) |

### Failures

Nine runs failed. Most serious first:

| Scenario | What happened | Kind |
|---|---|---|
| `cx-016` | "Can you **take me off** the schedule for Thursday?" matched the natural-language opt-out pattern. The customer was unsubscribed and the booking they wanted cancelled was left in place. | Pipeline bug (opt-out regex) |
| `la-001` | Told the customer "I'll have Dana reach out" about a water-heater estimate, but never called `escalate_to_owner`, so the owner is never told. | Agent bug |
| `nb-048` | The model escalated a small leak at the bottom of a water heater as a flooding emergency. The customer's "no gas smell either" then tripped the gas pattern, because the pre-filter ignores negation by design (D-004). A routine booking was lost. | Agent over-escalation, plus a pre-filter false positive |
| `la-004` | "My furnace won't **fire** up" matched the pre-filter's fire pattern: electrical safety message, handed to the owner. | Pre-filter false positive |
| `la-005` | "My **CO detector** chirps once a minute, I think it needs a new battery" matched the CO pattern. | Pre-filter false positive |
| `ot-007` | Declined a dishwasher install without escalating, and opened with "Dana here", so the agent claimed to be the owner. | Agent bug (impersonation); expectation open |
| `ot-017` | Declined a car AC repair without escalating. | Expectation open |
| `amb-002` | Booked before asking what was wrong, as `leak_repair`; the problem was rusty hot water from an old tank. | Agent (prompt says to get the problem first) |
| `pr-010` | A garbage-disposal price question was escalated as `out_of_scope`, not `price_quote`. The owner was still notified. | Scenario ambiguous |

"Expectation open" means the agent ignored the prompt, but its behaviour may be the better one: a car AC question probably shouldn't page the owner. Either the prompt or the scenario changes in Phase 4.

### Judge (uncalibrated)

Not yet checked against human labels (D-041), so these are indications only. Polite 100% and SMS style 100% (174/174 each). No unfounded promises 98.3%. Customer in character 100% (133/133 simulated runs). **One question at a time: 88.5% (154/174)**. The agent sometimes stacks questions, for example "Is this at a house or apartment, and what's the address?"

### Phase 4 work list from this run

1. Narrow the natural-language opt-out patterns so "take me off the schedule" and similar go to the agent (`cx-016`).
2. Make "Dana will reach out" impossible without an escalation; fix the agent speaking as Dana (`la-001`, `ot-007`).
3. Reduce over-escalation of minor leaks, and decide how the pre-filter treats explicit negations like "no gas smell" (`nb-048`).
4. Pre-filter false positives: "fire up", and CO detectors that are only chirping for a battery (`la-004`, `la-005`).
5. Decide the out-of-scope policy for obvious non-services; ask for the problem before booking; one question per message (`ot-007`, `ot-017`, `amb-002`, judge).
6. Run repeats (k = 3) to put a real interval on emergency handling, and to measure flakiness.
