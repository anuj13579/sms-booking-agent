# SMS Booking Agent

An SMS agent for small HVAC, plumbing, and electrical businesses. It answers customer texts, books real technician arrival windows, handles reschedules and cancellations, and hands off to the owner for emergencies, price questions, and anything it isn't sure about.

**Status:** Phase 0 (design) complete. No application code yet.

## Documents

- [`docs/DESIGN.md`](docs/DESIGN.md): architecture, message flow, agent and safety design, data model, eval plan, budget, and risks.
- [`DECISIONS.md`](DECISIONS.md): every major design decision, with the alternatives that were rejected.
- `RESULTS.md` (from Phase 4): every eval run, with metrics.

## Build phases

0. Design ✅
1. Core domain, no LLM
2. Agent loop, plus the web-chat simulator
3. Eval harness
4. Iterate on eval results
5. Twilio, plus the owner dashboard
6. Deployment
7. Pilot and write-up

## Utilities

- `python scripts/estimate_eval_cost.py`: estimates eval-run cost on DeepSeek pricing (see DESIGN §8).
