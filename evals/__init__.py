"""The eval harness (Phase 3, DESIGN section 9, D-018).

    uv run python -m evals validate            # check every scenario file, no DB or LLM needed
    uv run python -m evals run --split dev     # run the dev split against the real model
    uv run python -m evals report <run dir>    # rebuild a run's summary and report

Scenarios live in ``evals/scenarios/<category>.yaml``. Each run drives the production pipeline
(``handle_inbound`` with ``Channel.EVAL``, D-013) against a fresh copy of the seed business, then
grades the database and the tool-call trace. Run outputs go to ``evals/runs/`` (git-ignored);
summaries worth keeping go in RESULTS.md.
"""
