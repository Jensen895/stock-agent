"""What actually happened — the other half of every back-test row.

A score is only a prediction once something is measured against it. This module
takes the ledger's rows and fills in, for each one, what the stock did over the
next 1, 5, 10, 21 and 63 trading days, plus what SPY did over exactly the same
window.

Three decisions here do most of the work, and all three are about not flattering
the agents:

**Close to close.** Every return is measured from one official close to another.
Intraday prices would let the measurement drift toward whichever moment made the
call look best, and the app's own price fields are whatever Yahoo happened to be
serving at refresh time, which is not a price anyone could transact at.

**The anchor may not be the day of the prediction.** ``history.anchor_after``
decides which close a prediction could first have been acted on: a refresh at
the opening bell gets that afternoon's close, one stamped after 16:00 ET gets
the next session's. The difference matters — pricing an evening prediction at
that afternoon's close hands the agents the day's move for free, and on a
volatile name that alone can look like skill. Weekends and holidays roll forward
to the next date the series actually contains, so no assumption about the
trading calendar is needed beyond "the data has a row for every open day".

**Trading days, not calendar days.** Horizons step forward by position in the
close series, so "21 days" is a month of sessions whether or not a holiday fell
in it, and every observation of a given horizon spans the same amount of market.

Returns are recorded three ways: the stock's own, the benchmark's, and the
difference. The difference is the one the analysis leans on — a portfolio of
large-cap tech in a rising market goes up regardless of what any agent said, and
absolute returns would credit the agents for the market's work.
"""

from datetime import datetime, timezone

# In trading days. 21 is about a month and 63 about a quarter, which brackets
# the one-to-three-month horizon the agents are explicitly asked to answer on;
# the short ones are there to show how little the early days mean, which is a
# finding in itself when the ledger is young.
HORIZONS = (1, 5, 10, 21, 63)

# Trailing window for the momentum baseline. Every agent has to beat "buy what
# went up last month", which costs nothing to compute and no model to run — see
# metrics.py, where it is scored exactly like a sixth agent.
MOMENTUM_LOOKBACK = 21

DEFAULT_BENCHMARK = "SPY"


def _iso(ts_ms) -> str:
    """A Yahoo millisecond timestamp as a UTC calendar date."""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).date().isoformat()


def _closes(series) -> list:
    """A daily series as an ordered [(date, close)] with the junk dropped."""
    out = []
    for point in series or []:
        try:
            ts, close = point
        except (TypeError, ValueError):
            continue
        if close is None:
            continue
        try:
            out.append((_iso(ts), float(close)))
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    out.sort(key=lambda p: p[0])
    # Yahoo occasionally repeats the last bar; keep the final value per date.
    deduped = {}
    for date, close in out:
        deduped[date] = close
    return sorted(deduped.items())


def _index_at_or_after(closes: list, date: str):
    """Position of the first close on or after ``date``, or None past the end."""
    for index, (day, _) in enumerate(closes):
        if day >= date:
            return index
    return None


def _pct(later: float, earlier: float):
    if not earlier:
        return None
    return round((later - earlier) / earlier * 100.0, 4)


def _needed_tickers(doc: dict) -> list:
    """Every ticker with at least one row still missing an outcome.

    Rows whose horizons are all filled are skipped, so a ledger that has been
    scored daily for a year re-fetches only the handful of names whose 63-day
    window has just matured, not all of them.
    """
    wanted = set()
    for snapshot in doc.get("snapshots") or []:
        for row in snapshot.get("rows") or []:
            if _row_incomplete(row):
                wanted.add(row["ticker"])
    return sorted(wanted)


def _row_incomplete(row: dict) -> bool:
    outcomes = row.get("outcomes") or {}
    if row.get("anchor_close") is None:
        return True
    return any(str(h) not in outcomes for h in HORIZONS)


def score(doc: dict, market_data, benchmark: str = DEFAULT_BENCHMARK,
          verbose: bool = True) -> dict:
    """Fill in every outcome the price history can currently support.

    Mutates ``doc`` in place and returns a small summary. A horizon whose window
    has not closed yet is simply left absent — it is not a gap to be imputed,
    it is a measurement that has not happened, and writing a placeholder for it
    would let an unfinished window into an average.

    Prices come from the same Yahoo provider the app uses, which serves one year
    of daily closes. A prediction older than that can no longer be anchored; the
    row keeps whatever it already had and is reported as unscorable rather than
    silently dropped.
    """
    tickers = _needed_tickers(doc)
    if not tickers:
        return {"tickers": 0, "filled": 0, "pending": 0, "unscorable": 0}

    fetch = sorted(set(tickers) | {benchmark})
    if verbose:
        print(f"backtest: fetching daily closes for {len(fetch)} symbols...")
    raw = market_data.get_daily_many(fetch) or {}
    closes = {t: _closes(raw.get(t)) for t in fetch}
    bench = closes.get(benchmark) or []
    if verbose and not bench:
        print(
            f"backtest: no history for benchmark {benchmark} — "
            "excess returns will be unavailable for this run."
        )

    filled = pending = unscorable = 0
    missing = set()
    for snapshot in doc.get("snapshots") or []:
        requested = snapshot.get("anchor_requested") or snapshot.get("date")
        for row in snapshot.get("rows") or []:
            if not _row_incomplete(row):
                continue
            series = closes.get(row["ticker"]) or []
            if not series:
                missing.add(row["ticker"])
                unscorable += 1
                continue
            start = _index_at_or_after(series, requested)
            if start is None:
                # The anchor is in the future (a prediction made after today's
                # close, before tomorrow's session). Normal; try again tomorrow.
                pending += 1
                continue
            anchor_date, anchor_close = series[start]
            row["anchor"] = anchor_date
            row["anchor_close"] = round(anchor_close, 4)
            row["momentum_pct"] = _trailing(series, start, MOMENTUM_LOOKBACK)

            b_start = _index_at_or_after(bench, anchor_date) if bench else None
            outcomes = dict(row.get("outcomes") or {})
            for horizon in HORIZONS:
                key = str(horizon)
                if key in outcomes:
                    continue
                end = start + horizon
                if end >= len(series):
                    pending += 1
                    continue
                end_date, end_close = series[end]
                entry = {
                    "as_of": end_date,
                    "close": round(end_close, 4),
                    "ret_pct": _pct(end_close, anchor_close),
                }
                b_ret = _benchmark_return(bench, b_start, horizon)
                if b_ret is not None and entry["ret_pct"] is not None:
                    entry["bench_pct"] = b_ret
                    entry["excess_pct"] = round(entry["ret_pct"] - b_ret, 4)
                outcomes[key] = entry
                filled += 1
            row["outcomes"] = outcomes

    if verbose and missing:
        print(
            "backtest: no price history for "
            + ", ".join(sorted(missing)[:8])
            + (" ..." if len(missing) > 8 else "")
            + " — those rows stay unscored."
        )
    return {
        "tickers": len(tickers),
        "filled": filled,
        "pending": pending,
        "unscorable": unscorable,
        "benchmark": benchmark if bench else None,
    }


def _benchmark_return(bench: list, start, horizon: int):
    """SPY's return over the same window, or None when it can't be aligned."""
    if start is None or not bench:
        return None
    end = start + horizon
    if end >= len(bench):
        return None
    return _pct(bench[end][1], bench[start][1])


def _trailing(series: list, index: int, lookback: int):
    """The stock's own return over the ``lookback`` sessions *before* the anchor.

    This is the momentum baseline's input, and it is computed from data that
    existed at prediction time — never from anything after the anchor — so it is
    a fair competitor to the agents rather than a hindsight variable.
    """
    start = index - lookback
    if start < 0:
        return None
    return _pct(series[index][1], series[start][1])
