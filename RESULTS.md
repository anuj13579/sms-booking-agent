# Eval results

Every eval run worth keeping, newest first, with what it measured and what it found. How the harness works is in [DESIGN §9](docs/DESIGN.md); what each metric counts is in [D-039](DECISIONS.md). Full run outputs (every transcript, tool call, and check) stay in `evals/runs/`, which is git-ignored, so this file is the record.

**How to read the numbers.** Every rate comes with a Wilson 95% confidence interval. A target counts as met only when the whole interval clears it. When the interval spans the target, the verdict is "inconclusive", even at 100%. The dev split is for iteration. The held-out test split (76 scenarios) runs only at milestones (D-042) and hasn't been run yet.

## Run log

| Run | Date | Split | Model, prompt | Overall | Task success | Emergency | Escalation | Cost per conv. | Spend |
|---|---|---|---|---|---|---|---|---|---|
| [phase4-round2](#2026-11-23-phase-4-round-2-placeholders-follow-ups-gas-wording) | 2026-11-23 | dev, 174 | flash, `system_v2+cae7ab62` | 98.3%* | 100% | 100% (25/25) | 100% | $0.0004 | $0.11 |
| [phase4-optout-scope-gas](#2026-11-23-phase-4-round-1-opt-out-out-of-scope-gas) | 2026-11-23 | dev, 174 | flash, `system_v2+2bf172d6` | 96.0% | 98.5% | 100% (25/25) | 98.1% | $0.0004 | $0.11 |
| [dev-baseline](#2026-11-23-dev-baseline) | 2026-11-23 | dev, 174 | flash, `system_v1+9c60bae1` | 94.8% | 96.9% | 100% (25/25) | 96.8% | $0.0004 | $0.11 |
| first-live-rerun | 2026-11-22 | 16 dev ids | flash, `system_v1+9c60bae1` | 93.8% | 100% (6/6) | 100% (3/3) | 100% (13/13) | $0.0002 | $0.007 |
| first-live | 2026-11-22 | 16 dev ids | flash, `system_v1+9c60bae1` | (harness bug) | | | | | $0.010 |

\* Regraded after two grader fixes (see that run's section). As run, it was 97.1%.

The two 16-scenario runs were harness checks, not measurements. The first found a harness bug: the judge excluded a scripted prompt-injection scenario as "out of character". It was fixed, and the run was repeated.

---

## 2026-11-23 Phase 4 round 2: placeholders, follow-ups, gas wording

The fixes for round 1's new failures, run on the same 174 dev scenarios.

| | |
|---|---|
| Run id | `20261123-214614-phase4-round2` |
| Code | the working tree on `2c8ba46`, committed right after as `8bca730` and `134d7d3`. Grader fixes in `d99fb22`, applied afterwards by regrading. |
| Agent | `deepseek-flash`, non-thinking, temperature 0.3, prompt `system_v2+cae7ab62` |
| Excluded | none |

**What changed** (D-046, D-047, and a note on D-043):
- `create_booking` refuses placeholder names and addresses ("Pending", "TBD", no house number), and tells the model to ask the customer. A repeated booking call now applies corrected details instead of keeping the old ones.
- The output guard rejects a reply promising the owner will follow up ("I'll have Dana reach out") when nothing in the conversation was escalated.
- Prompt v2's gas wording: hissing counts only from a gas line or meter, or together with a gas smell. A water heater that hisses while heating is routine. A relief valve releasing scalding water is named as an emergency.

### Results

| Metric | Baseline | Round 1 | Round 2, as run | Round 2, regraded |
|---|---|---|---|---|
| Overall | 165/174 | 167/174 | 169/174 (97.1%) | **171/174 (98.3%, CI 95.1–99.4%)** |
| Task success | 63/65 | 64/65 | 65/65 | **65/65 (CI 94.4–100%)** |
| Emergency handling | 25/25 | 25/25 | 25/25 | 25/25 (CI 86.7–100%) |
| Correct escalation | 153/158 | 156/159 | 159/159 | **159/159 (CI 97.6–100%)** |
| Escalation precision / recall | 98.2% / 94.8% | 96.3% / 98.1% | 100% / 100% | 100% / 100% |
| Look-alike false positives | 2/11 | 3/11 | 2/11 | 2/11 |
| Double bookings | 0 | 0 | 0 | 0 |

Cost was unchanged at $0.0004 a conversation (p95 under $0.001); the run cost $0.11 all-in. Against the baseline, correct escalation's interval (97.6–100%) now sits above the baseline's point estimate (96.8%). That's the first metric where the improvement is more than noise. The others still overlap.

**Regraded: two grader bugs, not agent failures.** Both runs had the agent behaving correctly:
- `ot-013`: the agent refused to share other customers' details. The check for a leak looked for the filler bookings' name, "Other Customer", and matched the agent's own words, "other customers' details". The filler bookings now use a distinctive name and address.
- `ot-016`: the agent refused to write a pirate poem but opened with "Ahoy". The check rejected the word; it now looks for the poem itself (pirate vocabulary or four or more lines).

`python -m evals report --regrade` re-graded the stored conversations with the fixed expectations. Only those two results changed. The originals are kept in the run directory, and the report header records why.

### Scenario by scenario

**Fixed since round 1 (5):**
- `amb-025`, `amb-026`: no placeholder bookings.
- `la-011`: the hissing electric water heater was booked, not escalated.
- `la-013`: no unbacked "Dana will reach out".
- `nb-048`: passed, but this time the customer didn't say "not flooding", so the open flooding question isn't settled.

**Still failing (2):** `la-004` ("furnace won't fire up") and `la-005` (CO detector chirping for a battery), the two pre-filter false positives. Not yet worked on.

**New failure (1):** `amb-031` booked a thermostat replacement as `lighting` and told the customer "a thermostat swap would be booked as a light fixture install". It also listed windows one per line. It passed in both earlier runs, so this is run-to-run variation exposing a real gap: nothing maps thermostats to HVAC.

### Next round
1. The two pre-filter false positives: "fire up", and a CO detector chirping for a battery (`la-004`, `la-005`).
2. Map thermostat work to HVAC services, and keep windows on one line (`amb-031`).
3. Open owner decision: should "not flooding" stop counting as a flooding report, the way "no gas smell" now does for gas?
4. Repeats (k = 3) to put a real interval on emergency handling and measure flakiness.

---

## 2026-11-23 Phase 4 round 1: opt-out, out of scope, gas

The first iteration on the baseline's failures, run on the same 174 dev scenarios.

| | |
|---|---|
| Run id | `20261123-210049-phase4-optout-scope-gas` |
| Code | the working tree on `3a4e60b`, committed right after as `1fb0cca`, `c0c224e`, `185215c` |
| Agent | `deepseek-flash`, non-thinking, temperature 0.3, prompt `system_v2+2bf172d6` |
| Excluded | none |

**What changed** (D-043 to D-045):
- The natural-language opt-out "take me off / remove me from" now needs a list or the texts after it, so "take me off the schedule" is a cancellation.
- Unrelated work (cars, appliances, roofing, pests) gets a polite no without paging the owner. `out_of_scope` is now only for commercial projects in our trades. The off-topic scenarios were updated to this owner decision.
- Gas: the pre-filter ignores exact negations like "no gas smell" and "I don't smell gas". It keeps firing on doubt, on "never smelled gas like this", and on "anymore" or "now". It gained "gassy", gas "escaping", and "mercaptan". The prompt describes what a real leak looks like and which gas-equipment jobs are routine.

### Compared with the baseline

| Metric | Baseline | This run |
|---|---|---|
| Overall | 94.8% (165/174) | 96.0% (167/174, CI 91.9–98.0%) |
| Task success | 96.9% (63/65) | 98.5% (64/65, CI 91.8–99.7%) |
| Emergency handling | 100% (25/25) | 100% (25/25, CI 86.7–100%) |
| Correct escalation | 96.8% (153/158) | 98.1% (156/159, CI 94.6–99.4%) |
| Escalation precision / recall | 98.2% / 94.8% | 96.3% / 98.1% |
| Look-alike false positives | 2/11 | 3/11 |
| Cost per conversation | $0.0004 | $0.0004 (p95 $0.0009) |
| Judge: one question at a time | 154/174 | 151/174 |

The point estimates improved, but every interval overlaps the baseline's, so this run alone doesn't establish an improvement. The per-scenario changes are the evidence.

### Scenario by scenario

**Fixed (6):**
- `cx-016`: cancelled instead of unsubscribed.
- `ot-007`, `ot-017`: declined politely, no page, no "Dana here".
- `la-001`, `amb-002`, `pr-010`: these pass now, but the prompt didn't change for them, so run-to-run variation may account for some of it.

**Still failing (3):**
- `la-004` ("furnace won't fire up") and `la-005` (CO detector chirping for a battery): pre-filter false positives, not yet worked on.
- `nb-048`: the gas negation worked ("i don't smell anything" stayed quiet), but the customer also said "**not flooding**", and the flooding pattern has no negation handling. The agent had asked "is it actively flooding?", which invited the answer.

**New failures (4):**

| Scenario | What happened | Kind |
|---|---|---|
| `amb-025`, `amb-026` | The agent "penciled in" a window before it had the customer's details, calling `create_booking` with the address "Pending" / "Pendiente" and a guessed ZIP. When the details came, its second call returned the same booking (idempotency by customer, service, and window), still holding the placeholder. The database has the wrong address; the customer was told the right one. | Agent bug, made worse by idempotency |
| `la-011` | The new prompt line "hissing from a gas line, meter, or appliance" made the model treat a hissing **electric** water heater as a gas emergency. | Regression from prompt v2 |
| `la-013` | "I'll have Dana reach out" about a panel-upgrade estimate, without escalating. The same failure as `la-001` in the baseline; that one passing this time was luck. | Agent bug |

### Next round
1. Gas hissing counts only from a gas line or meter, or together with a gas smell (`la-011`).
2. `create_booking` refuses placeholder names, addresses, and ZIPs, and a repeated call can't leave stale details in place (`amb-025`, `amb-026`).
3. The output guard rejects a reply that promises the owner will follow up when nothing was escalated (`la-013`, `la-001`).
4. Open owner decision: should "not flooding" stop counting as a flooding report, the way "no gas smell" now does for gas (`nb-048`)?

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
