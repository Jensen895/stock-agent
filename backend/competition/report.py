"""The daily report: what each agent is worth, what it did, and why.

Rendered from the ledger row, never from live state, so the report for day 6
says what was true on day 6 no matter when you print it.

What it leads with, and why
---------------------------
The leaderboard is first because it is the question, but the report is
deliberately not *only* the leaderboard. Over thirty sessions three agents will
end up in some order, and the order alone is close to worthless — it is one
draw from a very noisy distribution, and the agent that wins by 1.2% is not
thereby the better strategy. What makes the exercise worth running is the
second half of every entry: the argument each agent made on the day it acted,
and the argument it made on the days it declined to.

So each agent gets, in this order: what it is worth, what it bought and sold,
**what it decided against and why**, what its five researchers said about the
whole list, and what it is holding. The rejections are given equal billing with
the trades on purpose — a book that did nothing because every name failed its
position cap and a book that did nothing because its agents were unconvinced
are different books having different days, and a report that shows both as
"no trades" hides the only distinction that matters.

The last section is disagreement: the same ticker, scored by the same five
researchers over the same evidence, landing on different sides of a trade in
two of the three books. That is the closest this exercise gets to a controlled
experiment, and by the end of the month it is where the answer lives.
"""

from datetime import datetime

from backend.ai_agents import CASH_APR_PCT

_MEDALS = ("1st", "2nd", "3rd")


# --- small formatters ----------------------------------------------------


def _money(value) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def _signed_money(value) -> str:
    if value is None:
        return "—"
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _signed_pct(value) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def _pretty_date(date: str) -> str:
    try:
        return datetime.strptime(date, "%Y-%m-%d").strftime("%A %-d %B %Y")
    except Exception:
        return date


def _agent_label(key: str) -> str:
    """Turn a research agent's key into the name a reader recognises."""
    return (key or "").replace("_", " ")


def _one_line(text, limit: int = 200) -> str:
    """Flatten a message onto one line and cut it short.

    Provider errors arrive as several lines of banner and stack; ten of those
    in a bullet list is a page of noise between the reader and the report. The
    ledger keeps the full text — this is only what gets printed.
    """
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --- the daily report ----------------------------------------------------


def daily(row: dict) -> str:
    """One session, as markdown."""
    out = []
    day, total = row.get("day"), row.get("sessions")
    left = row.get("sessions_left")
    out.append(f"# Competition day {day} of {total} — {_pretty_date(row['date'])}")
    out.append("")
    out.append(
        f"*{left} trading session{'s' if left != 1 else ''} left. "
        f"Every agent started with {_money(row.get('starting_cash'))}. "
        f"Marked at the close; not financial advice, and none of this money is "
        f"real.*"
    )
    out.append("")
    out += _leaderboard(row)
    out.append("")

    ranked = _ranked(row)
    for entry in ranked:
        out += _agent_section(entry)
        out.append("")

    out += _disagreements(ranked)
    return "\n".join(out).rstrip() + "\n"


def _ranked(row: dict) -> list:
    agents = list((row.get("agents") or {}).values())
    return sorted(agents, key=lambda a: -(a.get("equity") or 0))


def _leaderboard(row: dict) -> list:
    out = [
        "| # | Agent | Worth | Today | Since day 1 | Cash | Names |",
        "|---|-------|------:|------:|------------:|-----:|------:|",
    ]
    for i, agent in enumerate(_ranked(row)):
        place = _MEDALS[i] if i < len(_MEDALS) else f"{i + 1}th"
        out.append(
            f"| {place} | **{agent['key']}** {agent['name']} "
            f"| {_money(agent.get('equity'))} "
            f"| {_signed_money(agent.get('day_pnl'))} "
            f"({_signed_pct(agent.get('day_pnl_pct'))}) "
            f"| {_signed_money(agent.get('total_pnl'))} "
            f"({_signed_pct(agent.get('total_pnl_pct'))}) "
            f"| {_money(agent.get('cash'))} "
            f"| {len(agent.get('positions') or [])} |"
        )
    bench = row.get("benchmark") or {}
    if bench.get("equity") is not None:
        out.append(
            f"| — | _{bench.get('ticker')} bought on day 1 and held_ "
            f"| {_money(bench['equity'])} | — "
            f"| {_signed_pct(bench.get('pnl_pct'))} | — | 1 |"
        )
    return out


def _agent_section(agent: dict) -> list:
    out = [
        f"## {agent['key']} — {agent['name']}  ·  "
        f"{_money(agent.get('equity'))} ({_signed_pct(agent.get('total_pnl_pct'))})",
        "",
        f"> {agent.get('thesis', '')}",
        "",
        f"**Weighting** {_weights_line(agent.get('weights') or {})}  ",
        f"**Policy** {agent.get('policy', '')}",
        "",
    ]

    errors = agent.get("errors") or []
    if errors:
        out.append(f"**Problems today** ({len(errors)})")
        out += [f"- {_one_line(e)}" for e in errors[:8]]
        if len(errors) > 8:
            out.append(f"- …and {len(errors) - 8} more; see the ledger.")
        out.append("")

    out += _trades_block(agent)
    out += _declined_block(agent)
    out += _thoughts_block(agent)
    out += _book_block(agent)
    return out


def _weights_line(weights: dict) -> str:
    ordered = sorted(weights.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{_agent_label(k)} ×{v:g}" for k, v in ordered)


def _trades_block(agent: dict) -> list:
    trades = agent.get("trades") or []
    out = ["### What it did", ""]
    if not trades:
        out += ["Nothing. The book was left exactly as it was.", ""]
        return out

    for trade in trades:
        out += _one_trade(trade)
        out.append("")
    return out


def _one_trade(trade: dict) -> list:
    side = trade["side"].upper()
    line = (
        f"**{side} {trade['shares']:.4f} {trade['ticker']} at "
        f"{_money(trade['price'])} — {_money(trade['value'])}**"
    )
    meta = [f"scored {trade.get('confidence')}/100"]
    if trade.get("consensus"):
        meta.append(f"the five were *{trade['consensus']}*")
    if trade.get("origin_label"):
        meta.append(trade["origin_label"].lower())
    if trade["side"] == "sell":
        if trade.get("sold_out"):
            meta.append("**position closed**")
        elif trade.get("sell_fraction"):
            meta.append(f"{trade['sell_fraction']}% of the position")
        if trade.get("realized_gain") is not None:
            meta.append(f"realised {_signed_money(trade['realized_gain'])}")
    elif trade.get("claim_pct") is not None:
        meta.append(
            f"took {trade['claim_pct']:.0f}% of a "
            f"{_money(trade.get('slice_size'))} slice"
        )

    out = [line, f"{' · '.join(meta)}", ""]
    if trade.get("headline"):
        source = _agent_label(trade.get("headline_from"))
        out.append(f"> {trade['headline']}"
                   + (f" — *{source}*" if source else ""))
        out.append("")

    out.append("<details><summary>What each of the five said</summary>")
    out.append("")
    for source in trade.get("agents") or []:
        weight = source.get("weight")
        weighted = f", weight ×{weight:g}" if weight is not None else ""
        out.append(
            f"- **{source.get('name')}** ({source.get('confidence')}/100"
            f"{weighted}) — {source.get('detail') or ''}"
        )
        for line_ in _lines(source.get("reasoning")):
            out.append(f"  - {line_}")
        if source.get("risks"):
            out.append(f"  - *Risk:* {source['risks']}")
    out.append("")
    out.append("</details>")
    return out


def _lines(reasoning) -> list:
    """The agents return reasoning as a list of short lines, or occasionally as
    one string. Take both."""
    if not reasoning:
        return []
    if isinstance(reasoning, str):
        return [l.strip(" -•") for l in reasoning.splitlines() if l.strip()]
    return [str(l).strip() for l in reasoning if str(l).strip()]


def _declined_block(agent: dict) -> list:
    declined = agent.get("declined") or []
    if not declined:
        return []
    out = ["### What it decided against", ""]
    for row in declined:
        side = (row.get("side") or "").upper()
        ticker = row.get("ticker", "—")
        score = row.get("confidence")
        scored = f" ({score}/100)" if score is not None else ""
        out.append(f"- **{side} {ticker}**{scored} — {row.get('why')}")
    out.append("")
    return out


def _thoughts_block(agent: dict) -> list:
    thoughts = agent.get("thoughts") or []
    if not thoughts:
        return []
    out = ["### What the five researchers made of the whole list", ""]
    for column in ("what it holds", "what it's watching", "what's in the news"):
        rows = [n for n in thoughts if n.get("column") == column]
        if not rows:
            continue
        out.append(f"**On {column}**")
        out += [f"- **{n.get('name')}** — {n.get('note')}" for n in rows]
        out.append("")
    leftover = [n for n in thoughts
                if n.get("column") not in
                ("what it holds", "what it's watching", "what's in the news")]
    out += [f"- **{n.get('name')}** — {n.get('note')}" for n in leftover]
    if leftover:
        out.append("")
    return out


def _book_block(agent: dict) -> list:
    positions = agent.get("positions") or []
    equity = agent.get("equity") or 0.0
    cash = agent.get("cash") or 0.0
    out = ["### The book", ""]

    if positions:
        out += [
            "| Ticker | Shares | Cost | Last | Value | % of book | Today | Since bought |",
            "|--------|-------:|-----:|-----:|------:|----------:|------:|-------------:|",
        ]
        for p in positions:
            share = (p["market_value"] / equity * 100) if equity else 0
            out.append(
                f"| {p['ticker']} | {p['shares']:.4f} | {_money(p['avg_price'])} "
                f"| {_money(p.get('price'))} | {_money(p['market_value'])} "
                f"| {share:.1f}% | {_signed_pct(p.get('day_pct'))} "
                f"| {_signed_money(p.get('total_value'))} "
                f"({_signed_pct(p.get('total_pct'))}) |"
            )
        out.append("")
    else:
        out += ["Entirely in cash.", ""]

    share = (cash / equity * 100) if equity else 100.0
    out.append(
        f"Cash {_money(cash)} — {share:.1f}% of the book, earning "
        f"{CASH_APR_PCT:g}% APR ({_money(cash * CASH_APR_PCT / 100 / 12)} a month). "
        f"Realised so far: {_signed_money(agent.get('realized'))}."
    )
    if not agent.get("priced", True):
        out.append("")
        out.append(
            "*At least one holding had no live quote and is carried at cost, so "
            "the mark above is approximate.*"
        )
    out.append("")
    return out


def _disagreements(ranked: list) -> list:
    """Tickers two agents took opposite sides of today.

    Same five researchers, same evidence, same day — so anything here is
    produced purely by the weighting and the policy, which is exactly the
    variable under test.
    """
    sides = {}
    for agent in ranked:
        for trade in agent.get("trades") or []:
            sides.setdefault(trade["ticker"], []).append(
                (agent["key"], trade["side"], trade.get("confidence"))
            )
        for row in agent.get("declined") or []:
            if row.get("ticker") and row.get("ticker") != "—":
                sides.setdefault(row["ticker"], []).append(
                    (agent["key"], f"declined to {row.get('side')}",
                     row.get("confidence"))
                )

    split = {
        ticker: entries
        for ticker, entries in sides.items()
        if len({e[1] for e in entries}) > 1
    }
    if not split:
        return []

    out = ["## Where they disagreed", "",
           "Same researchers, same evidence, same day — so the difference here "
           "is the weighting and the policy, and nothing else.", ""]
    for ticker in sorted(split):
        parts = [
            f"**{key}** {side}" + (f" at {score}/100" if score is not None else "")
            for key, side, score in split[ticker]
        ]
        out.append(f"- **{ticker}** — " + "; ".join(parts))
    out.append("")
    return out


# --- the running scoreboard ----------------------------------------------


def standings(days: list, contest: dict) -> str:
    """Every session so far on one screen, plus who is doing what."""
    if not days:
        return "No sessions have been run yet.\n"

    latest = days[-1]
    total = contest.get("sessions")
    out = [
        f"# Standings after {len(days)} of {total} sessions",
        "",
        f"*Through {_pretty_date(latest['date'])}. Started at "
        f"{_money(contest.get('starting_cash'))} each.*",
        "",
    ]
    out += _leaderboard(latest)
    out.append("")

    keys = sorted((latest.get("agents") or {}).keys())
    out += [
        "## The whole month",
        "",
        "| Day | Date | " + " | ".join(keys) + " | " +
        (contest.get("benchmark") or "SPY") + " |",
        "|----:|------|" + "|".join(["------:"] * (len(keys) + 1)) + "|",
    ]
    for row in days:
        cells = []
        for key in keys:
            agent = (row.get("agents") or {}).get(key) or {}
            cells.append(_signed_pct(agent.get("total_pnl_pct")))
        bench = (row.get("benchmark") or {}).get("pnl_pct")
        out.append(
            f"| {row.get('day')} | {row.get('date')} | "
            + " | ".join(cells) + f" | {_signed_pct(bench)} |"
        )
    out.append("")

    out += ["## How each of them trades", ""]
    for key in keys:
        agent = (latest.get("agents") or {}).get(key) or {}
        traded = sum(
            len(((r.get("agents") or {}).get(key) or {}).get("trades") or [])
            for r in days
        )
        out += [
            f"### {key} — {agent.get('name')}",
            "",
            f"> {agent.get('thesis', '')}",
            "",
            f"- **Weighting** {_weights_line(agent.get('weights') or {})}",
            f"- **Policy** {agent.get('policy', '')}",
            f"- **{traded} trade{'s' if traded != 1 else ''}** over "
            f"{len(days)} session{'s' if len(days) != 1 else ''}, "
            f"currently holding {len(agent.get('positions') or [])} "
            f"name{'s' if len(agent.get('positions') or []) != 1 else ''} "
            f"and {_money(agent.get('cash'))} in cash",
            f"- **Realised** {_signed_money(agent.get('realized'))}, "
            f"**marked** {_signed_money(agent.get('total_pnl'))} "
            f"({_signed_pct(agent.get('total_pnl_pct'))})",
            "",
        ]

    if len(days) >= total:
        out += _verdict(days, keys)
    return "\n".join(out).rstrip() + "\n"


def _verdict(days: list, keys: list) -> list:
    """The end of the contest, said plainly — including when it says nothing."""
    latest = days[-1]
    ranked = sorted(
        ((latest.get("agents") or {}).get(k) or {} for k in keys),
        key=lambda a: -(a.get("equity") or 0),
    )
    winner = ranked[0]
    last = ranked[-1]
    bench = (latest.get("benchmark") or {}).get("pnl_pct")
    spread = (winner.get("total_pnl_pct") or 0) - (last.get("total_pnl_pct") or 0)

    out = [
        "## The result",
        "",
        f"**{winner.get('key')} — {winner.get('name')}** finished ahead at "
        f"{_money(winner.get('equity'))} "
        f"({_signed_pct(winner.get('total_pnl_pct'))}), "
        f"{spread:.2f} points clear of {last.get('key')}.",
        "",
    ]
    if bench is not None:
        beat = [
            a.get("key") for a in ranked
            if (a.get("total_pnl_pct") or 0) > bench
        ]
        out.append(
            f"Buying the benchmark on day one and doing nothing returned "
            f"{_signed_pct(bench)}. "
            + (f"Beaten by: {', '.join(beat)}." if beat else
               "**None of the three beat it.**")
        )
        out.append("")
    out += [
        "Read the spread before reading the order. Thirty sessions is a very "
        "short sample: a gap of a point or two between three strategies over "
        "six weeks is well inside the range that identical strategies would "
        "produce by chance, and the winner is only evidence of a better "
        "approach if the margin is wide and the daily reports show it was "
        "earned by the decisions rather than by one lucky name. "
        "`python3 backtest.py --portfolio all` measures the underlying agents "
        "directly, which is the more reliable question to ask of this data.",
        "",
    ]
    return out
