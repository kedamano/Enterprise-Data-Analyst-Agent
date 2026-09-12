"""Eval harness — quality & cost regression for the Data Analyst Agent.

The project's biggest hiring/engineering gap was "no eval set, no answer-quality
regression". This package adds the missing layer in an offline-first way:

* ``golden.py``    — a small golden question set with deterministic assertions.
* ``runner.py``    — run each case through ``run_analysis`` in a chosen LLM mode
  (``mock`` = offline smoke / CI; ``real`` = needs API key), aggregate metrics.
* ``report.py``    — quality/cost metrics + per-case assertions into a report.

Metrics produced (used as the "quantified" story in interviews):
    pass_rate, finish_rate, tool_success_rate, reflect_pass_rate,
    avg_tool_calls, avg_spans (nodes), cost_estimate_usd (tokens heuristic).

Run::
    python -m app.eval.runner --mode mock            # offline, CI-safe
    python -m app.eval.runner --mode real            # real LLM (needs key)
"""
