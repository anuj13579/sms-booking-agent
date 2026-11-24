# SMS Booking Agent

An SMS agent for small HVAC, plumbing, and electrical businesses. It answers customer texts, books real technician arrival windows, handles reschedules and cancellations, and hands off to the owner for emergencies, price questions, and anything it isn't sure about.

**Status:** Phase 3 (the eval harness) built: 250 scenarios, deterministic graders, a simulated customer, an LLM judge, and confidence intervals on every rate. A first 16-scenario live check passed 15 of 16 for under two cents; the full baseline and the work on its failures are Phase 4.

## Documents

- [`docs/DESIGN.md`](docs/DESIGN.md): architecture, message flow, agent and safety design, data model, eval plan, budget, and risks.
- [`DECISIONS.md`](DECISIONS.md): every major design decision, with the alternatives that were rejected.
- [`RESULTS.md`](RESULTS.md): every eval run worth keeping, with metrics and what it found. The dev baseline (2026-11-23): task success 96.9%, emergency handling 25/25, correct escalation 96.8%, zero double-bookings, $0.0004 a conversation.

## Build phases

0. Design ✅
1. Core domain, no LLM ✅
2. Agent loop, plus the web-chat simulator ✅
3. Eval harness ✅
4. Iterate on eval results
5. Public demo: per-visitor sandboxes, the owner dashboard, and a Telegram bot
6. Deployment
7. Share the demo and write up

**No Twilio.** Sending real SMS needs a paid Twilio account and US sender registration, which doesn't fit a portfolio project on a budget under $10/month. The deployed agent is reached through a public web demo and a Telegram bot instead, and it still follows SMS rules on every channel: GSM-7 text, at most three segments, and carrier keywords like STOP. Adding Twilio later would mean one more channel adapter, not a rewrite. See [D-033](DECISIONS.md).

**The Phase 5 plan** ([DESIGN §12](docs/DESIGN.md), D-034 to D-037):

- Each visitor gets a private copy of the demo business, tied to a cookie and deleted 48 hours after last use. They chat as a customer, then open the owner dashboard to see their bookings, escalations, and the agent's tool calls.
- One React app holds the customer chat (with an "under the hood" panel for tool calls, tokens, and cost) and the owner dashboard.
- A Telegram bot is a second way to chat. Its link connects the Telegram chat to the visitor's sandbox, so those conversations show up in the same dashboard.
- Rate limits and a daily LLM spend cap are counted in Postgres.

## What Phase 3 built

- **250 scenarios** (`evals/scenarios/`) in the eleven categories of DESIGN §9: normal booking 50, ambiguous 35, reschedule 25, cancel 20, angry 20, clear emergency 20, subtle emergency 15, emergency look-alike 15, price 20, off-topic and prompt injection 20, opt-out 10. Each pins a frozen clock and a starting state (a returning customer, existing bookings, booked-out windows, technician time off), and says what must be true at the end. Each carries its dev/test split, stratified 70/30.
- **A harness that runs the production path** (`evals/`). Every run gets its own copy of the seed business, and its messages go through the same `handle_inbound` the channels use (D-013, D-038). Runs go concurrently against a separate `_eval` database.
- **Deterministic grading** (D-039). The database and the tool-call trace decide: which bookings exist, in which windows, at which address; what happened to existing bookings; which escalations were raised; whether the safety message went out in the reply to the disclosing text; whether anything was texted to an opted-out customer. Saying "you're booked" without a booking row fails.
- **Customers.** Scripted ones for emergencies, opt-outs, and injection attempts; an LLM playing a persona for everything adaptive, with a fixed opening and a `[DONE]` signal. Runs where it breaks character are excluded and counted (D-040).
- **A judge that can't move the numbers** (D-041). Five yes/no questions about style, politeness, and unfounded promises, reported separately and marked uncalibrated until checked against human labels (`python -m evals label` / `calibrate`).
- **Honest statistics.** Wilson 95% intervals on every rate, a verdict per target ("meets", "below", or "inconclusive" when the interval spans it), repeats with flakiness detection, and the test split locked behind `--milestone` (D-042).
- **Cost control.** An upfront estimate refuses runs over `--max-cost`, and live spend stops new conversations at the cap.
- **Tests.** 62 new ones (393 in all), still at $0: the harness tests itself with a scripted FakeProvider standing in for the agent's model and the simulated customer. A test checks that every "clear" emergency trips the pre-filter and no "subtle" one does, so the two categories measure different detectors.

One finding from the first live run: "my furnace won't **fire** up" trips the pre-filter's fire pattern, so a routine heating job gets an electrical safety message and goes to the owner. The look-alike category exists to measure exactly this; the fix belongs to Phase 4.

## What Phase 2 built

- **The agent** (`app/agent`). One model, five tools, at most six LLM calls a turn (D-001). Tool arguments are Pydantic models and the schema the model sees is generated from them. No argument can name a customer, business, or phone number: identity comes from the conversation (D-010). Each tool call commits on its own.
- **Prompting for cost.** The prompt only ever grows at the end: each turn stores exactly what it added, and the next turn replays it unchanged before a fresh context note, so DeepSeek's prefix cache keeps hitting (D-015, D-030). A test checks this byte for byte. The prompt version is a hash of the template and the tool descriptions, and it is logged on every call.
- **Output guard.** Typography is normalised; prices, emoji, and anything over three SMS segments are rejected, with one rewrite before the fixed fallback (D-016, D-029).
- **Failure handling.** Per-call timeouts, a turn budget, one retry, a forced text answer on the last call, and handoff escalations that pre-empt everything else in the same response (D-031). The customer always gets a reply.
- **Provider.** Plain httpx against any OpenAI-compatible endpoint. DeepSeek thinks by default, so the adapter turns that off explicitly (D-032). Every call is logged with tokens, cache hits, latency, and cost from a dated price table.
- **Web-chat simulator** (`app/api`). Text the agent from a browser as any customer, and inspect every turn: tool calls with arguments and results, tokens, cost, escalations, bookings, and the SMS segment count of each reply.
- **Tests.** Over 320, still all at $0: the agent runs against a scripted FakeProvider, and everything else (tools, domain, guard, escalation policy, logging, the HTTP app) is real.

One finding worth reading: three concatenated SMS segments hold 459 characters, not 480. [D-029](DECISIONS.md) corrects the Phase 0 number.

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
uv run python -m evals validate       # check the 250 scenarios; no DB or LLM needed
```

To talk to the agent, copy `.env.example` to `.env`, set `DEEPSEEK_API_KEY`, then:

```bash
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 for the web-chat simulator. It is for development only: it lets anyone text the agent as any number, so keep it on localhost (`SIMULATOR_ENABLED=false` turns it off).

Eval runs need `DEEPSEEK_API_KEY` too. Each one wipes and rebuilds `booking_agent_eval` (or `EVAL_DATABASE_URL`) and writes its results, `summary.json`, and `report.md` to `evals/runs/<run id>/`:

```bash
uv run python -m evals run --split dev                            # 174 conversations, about $0.11; capped at --max-cost ($2)
uv run python -m evals run --category clear_emergency --repeat 3  # one category, three runs each
uv run python -m evals run --id nb-001 --id la-004                # specific scenarios
uv run python -m evals report evals/runs/<run id>                 # rebuild a report after changing metrics
uv run python -m evals run --split test --milestone "phase-4"     # the held-out split, milestones only
```

The same runs can be started from GitHub (Actions, "Evals", manual dispatch) with the `DEEPSEEK_API_KEY` repository secret.

`uv run python scripts/smoke_live.py` runs six fixed conversations against the real model (booking, cancel, price question, a subtle emergency, out of scope, prompt injection) with deterministic checks. It costs well under a cent on `deepseek-flash`. It is a smoke test, not an eval.

Checks CI runs on every push: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and `uv run pytest`. CI makes no LLM calls (D-019).

## Utilities

- `python scripts/estimate_eval_cost.py`: estimates eval-run cost on DeepSeek pricing (see DESIGN §8).
