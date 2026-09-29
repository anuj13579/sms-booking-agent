# SMS Booking Agent

An SMS agent for small HVAC, plumbing, and electrical businesses. It answers customer texts, books real technician arrival windows, handles reschedules and cancellations, and hands off to the owner for emergencies, price questions, and anything it isn't sure about.

**Status:** Phase 1 (core domain, no LLM) complete. The scheduling core, the database guarantees, and the deterministic half of the inbound pipeline are built and tested. The agent itself arrives in Phase 2.

## Documents

- [`docs/DESIGN.md`](docs/DESIGN.md): architecture, message flow, agent and safety design, data model, eval plan, budget, and risks.
- [`DECISIONS.md`](DECISIONS.md): every major design decision, with the alternatives that were rejected.
- `RESULTS.md` (from Phase 4): every eval run, with metrics.

## Build phases

0. Design ✅
1. Core domain, no LLM ✅
2. Agent loop, plus the web-chat simulator
3. Eval harness
4. Iterate on eval results
5. Twilio, plus the owner dashboard
6. Deployment
7. Pilot and write-up

## What Phase 1 built

- **Schema** (`app/db`). One Alembic migration. Postgres enforces the invariants that must never break: no technician double-booking (an exclusion constraint), idempotent booking creation (a partial unique index), webhook dedupe, and one open conversation per customer and channel.
- **Scheduling** (`app/domain`). Arrival windows expanded per date in the business's time zone, availability, and booking create, reschedule, and cancel with least-loaded technician assignment and an audit trail. Failures come back as data with alternative windows, ready to hand to the LLM as tool results.
- **Inbound pipeline** (`app/pipeline`). Dedupe, STOP/START/HELP keywords, the emergency pre-filter with fixed safety templates, the opted-out and handoff gates, escalation policy, and a Postgres work queue with per-conversation leases. The agent plugs in through a Protocol.
- **Tests.** Over 200 tests against real Postgres, including DST transitions, race tests that fire simultaneous bookings, and a layering check that `app/domain` imports nothing AI-related.

One finding worth reading: the race tests showed that concurrent inserts under an exclusion constraint can deadlock. [D-026](DECISIONS.md) explains the fix.

## Running it locally

Needs [uv](https://docs.astral.sh/uv/) and Docker.

```bash
docker compose up -d                  # Postgres 16 with booking_agent and booking_agent_test
uv sync                               # Python deps into .venv
uv run alembic upgrade head           # create the schema
uv run python -m app.seed             # the fictional pilot business, Brightwater Home Services
uv run python scripts/demo.py         # availability, a booking, and the pipeline, no LLM needed
uv run pytest                         # the test suite (wipes and rebuilds booking_agent_test)
```

Checks CI runs on every push: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and `uv run pytest`. CI makes no LLM calls (D-019).

## Utilities

- `python scripts/estimate_eval_cost.py`: estimates eval-run cost on DeepSeek pricing (see DESIGN §8).
