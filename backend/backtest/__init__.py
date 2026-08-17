"""Back-test harness: does any of the five agents actually predict anything?

This package is **not part of the app**. Nothing in ``run.py``'s request path
imports it, no endpoint serves it, and no panel renders it. It exists to answer
a question the app itself cannot: over time, does an agent's 0-100 score have
any relationship to what the stock subsequently did?

The pieces, in the order the daily command runs them:

    history.py    The ledger. One row per (day, ticker, risk, kind) carrying
                  the five raw agent scores and the blended average. Append
                  only; the raw scores are what make everything downstream
                  possible, because a weighted average can be recomputed at any
                  weights but an agent's own number cannot be recovered from it.

    outcomes.py   What actually happened. Anchors each prediction to the first
                  close it could have been acted on and fills in the forward
                  return at 1, 5, 10, 21 and 63 trading days, alongside SPY's
                  over the same window.

    metrics.py    The arithmetic. Per-agent information coefficients computed
                  cross-sectionally per day and averaged (Fama-MacBeth), hit
                  rates against the benchmark, score dispersion, the
                  agent-to-agent correlation matrix that tests the app's
                  disjoint-evidence claim, a momentum baseline the agents have
                  to beat to be worth anything, and a weight search with an
                  out-of-sample split so an over-fit answer is visible as one.

    analyst.py    The judgement. A Claude CLI call that reads the metrics and
                  writes the report — explicitly licensed to conclude that none
                  of the five agents work, that the weights are noise, or that
                  the missing signal is not one of these five.

    report.py     Rendering and archiving, plus a no-model fallback so the
                  command still produces a report when the CLI is unavailable.

Run it with ``python3 backtest.py`` from the project root.
"""
