"""Three agents, $5,000 each, thirty sessions, one report a day.

This package is **not part of the app**. Nothing in ``run.py``'s request path
imports it, no endpoint serves it and no panel renders it — the same
relationship ``backend/backtest`` has. It exists to answer a question the app
is not shaped to ask: if you hand the same research to three differently-minded
allocators and let them trade a real tape with fake money for six weeks, which
discipline ends up ahead, and can you tell from the daily record *why*?

The experiment
--------------
Three portfolios, A, B and C. Each is a normal workspace — you can open the app
and look at any of them — and each is run by the same five research agents from
``ai_agents.py`` over the same evidence, the same candidate universe and the
same closing prices. What differs is only how each blends those five scores and
what it will let the blend do:

    A  The Quant              multiples and the balance sheet; low risk
    B  The Narrative Trader   the story and the tape; high risk
    C  The Committee          acts only on agreement; hoards cash

That is the entire independent variable. Anything that separates the three
books after thirty sessions is produced by the weighting and the policy, and
by nothing else — which is what makes the daily disagreement section worth
reading and the final leaderboard worth (a little) trust.

The pieces, in the order a day runs them:

    agents.py    Who the three are: weights, policy, and the mandate each one
                 has its researchers told every morning. Pure data, no model.
    clock.py     When a day happens — 16:05 ET, and the session-date arithmetic
                 that makes a missed run catch up instead of double-booking.
    runner.py    The day itself: switch workspace, mark, score, sell, buy,
                 mark again. One agent at a time; a failure costs that agent
                 the day and nobody else.
    book.py      The paper book: the policy screen, the orders, and the mark.
                 The one place a sale credits cash.
    ledger.py    Where it is all kept — the roster, and one append-only row per
                 session carrying every trade and every argument behind it.
    report.py    The daily read: leaderboard, what each agent did, what it
                 declined to do, what its researchers thought, what it holds.

Run it with ``python3 competition.py`` from the project root.

A warning that belongs in the source and not just the reports: thirty sessions
is a very short sample. Three strategies will finish in some order whether or
not any of them is better than the others, and the gap between first and third
after six weeks is usually indistinguishable from noise. The reasoning captured
each day is the durable output here; the leaderboard is the hook.
"""
