"""One session of the contest, end to end, for all three agents.

The shape of a day
------------------
For each competitor in turn — never in parallel, because they share one Yahoo
session, one model, and one process-wide "active portfolio" pointer:

    1. switch      Point every service at this agent's workspace. One line in
                   the manager repoints holdings, cash, wishlist, weights and
                   suggestions at once; ``reload()`` drops the previous agent's
                   cached scores so nothing leaks across the boundary.
    2. mark        What the book is worth going in. This is what sizes the
                   position cap and the cash floor, and what the mandate quotes
                   back to the agents.
    3. mandate     Today's standing instruction: the day number, the sessions
                   remaining, the balance, and this competitor's remit.
    4. score       The advisor's twenty calls and Discover's ten, run
                   synchronously. Every score today is generated under today's
                   mandate — there is no carry-over.
    5. sell        Screen the plan's sells through the policy and book them.
                   Proceeds land in cash immediately.
    6. buy         Re-request the plan. No model is called: ``AiActionsService``
                   is arithmetic over scores that already exist, so asking a
                   second time costs a quote fetch and returns a plan sized
                   against the cash the sells just raised. This is what lets an
                   agent rotate out of a loser and into a new name on the same
                   day, which a single pass could not — the buys in the first
                   plan were sized before the money existed.
    7. mark        Again, for the record. Trades swap cash for stock at one
                   price, so the number should barely move; what changes is
                   what the book is made of.

Then one row for the whole session goes into the ledger and one report is
rendered from it.

Failure is per agent, per stage
-------------------------------
A wedged quote or a model that times out costs the agent it happened to a
day's trading — it holds, the error is recorded, and the report says so. It
does not cost the other two their day, and it does not cost the contest its
row: a session with a hole in it is still a session, and pretending otherwise
would mean thirty days of results that quietly skip the interesting ones.
"""

import os
import traceback
from contextlib import contextmanager

from backend.ai_advisor import is_market_open
from backend.competition import clock
from backend.competition.agents import (
    BENCHMARK,
    COMPETITORS,
    SESSIONS,
    STARTING_CASH,
    UNIVERSE,
)
from backend.competition.book import Book, TradableCash
from backend.competition.ledger import RESPONSES_DIR, Contest, ContestError, Ledger
from backend.actions import AiActionsService


class SessionSkipped(RuntimeError):
    """Raised when there is no work to do — already run, or the contest is over."""


@contextmanager
def _borrowed(services):
    """Put back everything the run moves, whether or not it finished.

    "Which portfolio is active" is persisted global state — it is how the app
    decides what to show you when you next open it. A contest that walks
    through three workspaces and stops on the last one has quietly changed
    which portfolio you were looking at, and a nightly ``watch`` would do that
    every day without ever saying so. The mandate and the CLI's response
    directory are the same kind of borrowed state.

    Restoring in a ``finally`` rather than at the end of the loop matters:
    the interesting case is the run that dies halfway through, which is exactly
    when the pointer would otherwise be left on whichever agent failed.
    """
    manager = services.manager
    was_active = manager.active_id()
    was_mandate = services.advisor.mandate
    dirs = {id(c): getattr(c, "cache_dir", None) for c in services.clients}
    try:
        yield
    finally:
        services.advisor.mandate = was_mandate
        for client in services.clients:
            if dirs.get(id(client)) is not None:
                client.cache_dir = dirs[id(client)]
        try:
            manager.switch(was_active)
            services.advisor.reload()
            services.discover.reload()
        except Exception as e:
            print(f"Competition: could not restore portfolio {was_active} — {e}")


# --- setup ---------------------------------------------------------------


def setup(services, reset: bool = False, cash: float = STARTING_CASH) -> dict:
    """Create the three portfolios and write ``contest.json``.

    Each competitor gets a registered workspace of its own, so the contest is
    visible in the app: open the portfolio switcher and "Agent A — The Quant"
    is there, with its own holdings, its own sliders already set to A's
    weighting, and its own AI columns. That is worth more than a private
    directory the UI can't see — the daily report says what A did, and the app
    lets you go and look at why.

    ``reset`` archives any previous run's portfolios first. Archived, not
    deleted: ``WorkspaceManager.delete`` moves the directory into ``_archive/``,
    so a contest that gets restarted still has its predecessor's book on disk.
    """
    contest = Contest()
    existing = contest.storage.load() or {}

    if existing.get("agents") and not reset:
        raise ContestError(
            "A contest already exists. Use `--reset` to archive it and start "
            "a new one, or `python3 competition.py status` to see where it got to."
        )

    if reset:
        for entry in (existing.get("agents") or {}).values():
            pid = entry.get("portfolio_id")
            try:
                services.manager.delete(pid)
                print(f"  archived previous workspace {pid}")
            except Exception:
                pass  # already gone, or it was the last one standing

    agents = {}
    with _borrowed(services):
        for competitor in COMPETITORS:
            entry = services.manager.create(
                f"Agent {competitor.key} — {competitor.name}"
            )
            pid = entry["id"]
            services.manager.switch(pid)
            services.manager.set_risk(competitor.risk, pid)

            # The weighting is the competitor. Written through the advisor so
            # it lands normalised, in the same file and the same shape the UI's
            # sliders write — a competitor is not a special kind of portfolio.
            services.advisor.reload()
            services.advisor.set_weights(competitor.weights)

            services.available.set(cash)
            for ticker in UNIVERSE:
                try:
                    services.wishlist.add(ticker)
                except Exception as e:
                    print(f"  {competitor.key}: could not watch {ticker} — {e}")

            agents[competitor.key] = {
                "portfolio_id": pid,
                "portfolio_name": entry["name"],
                **competitor.describe(),
            }
            print(f"  Agent {competitor.key} — {competitor.name}: "
                  f"${cash:,.0f}, {len(UNIVERSE)} names watched, workspace {pid}")

    data = {
        "created_at": clock.now_utc().isoformat(),
        "starting_cash": cash,
        "sessions": SESSIONS,
        "universe": list(UNIVERSE),
        "benchmark": BENCHMARK,
        "agents": agents,
    }
    contest.save(data)
    return data


# --- one session ---------------------------------------------------------


def run_session(services, date: str = None, use_discover: bool = True,
                force: bool = False) -> dict:
    """Run one competition day and return the ledger row.

    ``date`` defaults to the session the current moment belongs to — today
    after the close, the previous trading day before it. Raises
    ``SessionSkipped`` when that session is already in the ledger (so the
    watcher and a manual run can both fire) or the thirty are used up.
    """
    contest = Contest().load()
    ledger = Ledger()

    date = date or clock.session_date()
    if date is None:  # pragma: no cover - needs tz data to be missing
        raise SessionSkipped("No market close has happened yet.")

    existing = ledger.get(date)
    if existing and not force:
        raise SessionSkipped(
            f"{date} is already in the ledger (day {existing.get('day')}). "
            f"Nothing to do."
        )

    # A "close" taken at eleven in the morning is not a close. Every price in
    # the run — the mark, the fills, the benchmark — is a live quote, so during
    # the session the whole row would be stamped with a date whose closing
    # prices had not happened yet. Before the open and after it, the same live
    # quote *is* the last close, which is why the guard is only on the hours in
    # between.
    if is_market_open() and not force:
        raise SessionSkipped(
            "The market is open. Every price here is a live quote, so a run "
            "now would record intraday marks as though they were the close — "
            f"wait for {clock.next_close().isoformat(timespec='minutes')}, or "
            "pass --force if you meant it."
        )

    # A forced re-run keeps the day number the session already had. Counting it
    # as a new one would push every later session up by one and leave the
    # thirtieth off the end of the contest.
    day = existing.get("day") if existing else ledger.day_number()
    if day > contest["sessions"]:
        raise SessionSkipped(
            f"The contest is over — all {contest['sessions']} sessions have "
            f"been run. `python3 competition.py standings` for the result."
        )
    sessions_left = contest["sessions"] - day

    print(f"\nCompetition day {day}/{contest['sessions']} — session {date}")
    print("=" * 60)

    results = {}
    with _borrowed(services):
        for competitor in COMPETITORS:
            entry = contest["agents"].get(competitor.key) or {}
            pid = entry.get("portfolio_id")
            if not pid:
                continue
            results[competitor.key] = _run_one(
                services, competitor, pid, ledger, day, sessions_left, use_discover
            )

    row = {
        "day": day,
        "date": date,
        "closed_at": clock.now_utc().isoformat(),
        "sessions": contest["sessions"],
        "sessions_left": sessions_left,
        "starting_cash": contest["starting_cash"],
        "agents": results,
        "benchmark": _benchmark(services, ledger, contest),
    }
    ledger.record(row, replace=bool(existing))
    return row


def _run_one(services, competitor, pid, ledger, day, sessions_left,
             use_discover: bool) -> dict:
    """One agent's whole day. Never raises — a failure is recorded, not thrown."""
    print(f"\n[{competitor.key}] {competitor.name} — {competitor.thesis}")

    errors = []
    services.manager.switch(pid)
    services.advisor.reload()
    services.discover.reload()
    _point_cli_at(services, competitor)

    opening = Book(competitor, services.portfolio, services.available,
                   services.market, services.wishlist, UNIVERSE).mark()
    print(f"  book: ${opening['equity']:,.2f} "
          f"(${opening['cash']:,.2f} cash, {len(opening['positions'])} positions)")

    # Everything the agents are told beyond their own evidence, set before a
    # single call goes out so no score today is generated under yesterday's
    # deadline.
    services.advisor.mandate = competitor.mandate(
        day, sessions_left, opening["equity"], opening["cash"]
    )

    scored = True
    try:
        services.advisor.refresh_now()
    except Exception as e:  # pragma: no cover - refresh_now already swallows
        errors.append(f"advisor: {e}")
        scored = False

    advisor_state = services.advisor.get()
    if advisor_state.get("error"):
        errors.append(f"advisor: {advisor_state['error']}")
        scored = False
    for message in advisor_state.get("model_errors") or []:
        errors.append(f"advisor: {message}")

    if use_discover and services.discover.available():
        try:
            services.discover.refresh_now()
        except Exception as e:  # pragma: no cover
            errors.append(f"discover: {e}")
        for message in services.discover.get().get("model_errors") or []:
            errors.append(f"discover: {message}")

    book = Book(competitor, services.portfolio, services.available,
                services.market, services.wishlist, UNIVERSE)
    trades, declined = [], []

    if scored:
        try:
            trades, declined = _trade(services, competitor, book, opening)
        except Exception as e:
            errors.append(f"trading: {type(e).__name__}: {e}")
            traceback.print_exc()
    else:
        declined.append({
            "ticker": "—",
            "side": "hold",
            "why": "no scores today; the book was left untouched",
        })

    closing = book.mark()
    previous = ledger.previous_equity(competitor.key)
    base = previous if previous is not None else closing["equity"]
    started = STARTING_CASH

    realized = _realized_total(services)
    record = {
        "key": competitor.key,
        "name": competitor.name,
        "thesis": competitor.thesis,
        "portfolio_id": pid,
        "risk": competitor.risk,
        "weights": dict(services.advisor.weights),
        "policy": competitor.policy_summary(),
        "mandate": services.advisor.mandate,
        "equity": closing["equity"],
        "cash": closing["cash"],
        "positions_value": closing["positions_value"],
        "cost_basis": closing["cost_basis"],
        "priced": closing["priced"],
        "day_pnl": round(closing["equity"] - base, 2),
        "day_pnl_pct": round((closing["equity"] - base) / base * 100, 2) if base else 0.0,
        "total_pnl": round(closing["equity"] - started, 2),
        "total_pnl_pct": round((closing["equity"] - started) / started * 100, 2),
        "realized": realized,
        "positions": closing["positions"],
        "trades": trades,
        "declined": declined,
        "thoughts": _thoughts(services, competitor),
        "errors": errors,
    }
    print(f"  close: ${record['equity']:,.2f} "
          f"({record['day_pnl']:+,.2f} today, {record['total_pnl_pct']:+.2f}% total) "
          f"— {len(trades)} trade{'s' if len(trades) != 1 else ''}")
    return record


def _trade(services, competitor, book, opening):
    """Sells, then buys against the proceeds. Returns (trades, declined)."""
    trades, declined = [], []
    positions = {p["ticker"]: p for p in opening["positions"]}
    equity = opening["equity"]

    # --- sells ---------------------------------------------------------
    plan = _plan(services, competitor, book, equity, opening["cash"])
    accepted, rejected = book.screen_sells(plan.get("sells") or [], positions)
    declined += [{**r, "side": "sell"} for r in rejected]
    for row in accepted:
        try:
            trade = book.sell(row)
            trades.append(trade)
            print(f"    SELL {trade['shares']:.4f} {trade['ticker']} @ "
                  f"${trade['price']:,.2f} = ${trade['value']:,.2f} "
                  f"({trade['confidence']}/100)")
        except Exception as e:
            declined.append({**row, "side": "sell", "why": f"order failed: {e}"})

    # --- buys, against what the sells just raised ------------------------
    mark = book.mark()
    positions = {p["ticker"]: p for p in mark["positions"]}
    plan = _plan(services, competitor, book, mark["equity"], mark["cash"])
    accepted, rejected = book.screen_buys(
        plan.get("buys") or [], mark["equity"],
        book.tradable_cash(mark["equity"], mark["cash"]), positions,
    )
    declined += [{**r, "side": "buy"} for r in rejected]
    for row in accepted:
        try:
            trade = book.buy(row)
            trades.append(trade)
            print(f"    BUY  {trade['shares']:.4f} {trade['ticker']} @ "
                  f"${trade['price']:,.2f} = ${trade['value']:,.2f} "
                  f"({trade['confidence']}/100)")
        except Exception as e:
            declined.append({**row, "side": "buy", "why": f"order failed: {e}"})

    if not any(t["side"] == "buy" for t in trades):
        declined += _near_misses(services, competitor,
                                 {t["ticker"] for t in trades})
    if not trades:
        print("    no trades")
    return trades, declined


def _near_misses(services, competitor, traded, limit: int = 3) -> list:
    """The best names the buy floor kept this book out of, on a day it bought
    nothing.

    Without this a quiet day is unreadable. ``declined`` only ever holds names
    the *policy* screen turned away, and the buy floor sits upstream of it
    inside ``AiActionsService``: a competitor whose floor is 62 on a day when
    nothing scored above 58 produces an empty plan, an empty declined list, and
    a report that says "nothing" with no way to tell it apart from a day the
    agents were never asked. "The best thing it saw was MU at 58.4, four points
    short" is the sentence that makes the difference legible.

    Only computed when there were no buys. On a day the book did trade, the
    names that fell short of the floor are the ordinary background of a
    selective strategy rather than the story of the day.
    """
    rows = []
    profile = ((services.advisor.get().get("risk_profiles") or {})
               .get(competitor.risk) or {})
    rows += [(s, "it holds") for s in profile.get("suggestions") or []]
    rows += [(s, "it watches")
             for s in (profile.get("wishlist") or {}).get("candidates") or []]
    discover = ((services.discover.get().get("risk_profiles") or {})
                .get(competitor.risk) or {})
    rows += [(s, "the news") for s in discover.get("suggestions") or []]

    best = {}
    for suggestion, origin in rows:
        ticker = (suggestion or {}).get("ticker")
        score = (suggestion or {}).get("confidence")
        if not ticker or score is None or ticker in traded:
            continue
        if score >= competitor.buy_floor:
            continue  # it cleared the floor; something later stopped it
        if ticker not in best or score > best[ticker]["confidence"]:
            best[ticker] = {"ticker": ticker, "confidence": score,
                            "origin": origin,
                            "consensus": (suggestion or {}).get("consensus"),
                            "headline": (suggestion or {}).get("headline")}
    ranked = sorted(best.values(), key=lambda r: -r["confidence"])[:limit]
    return [
        {
            "side": "buy",
            "ticker": r["ticker"],
            "confidence": r["confidence"],
            "why": f"the best it saw in {r['origin']}, and still "
                   f"{competitor.buy_floor - r['confidence']:.1f} short of this "
                   f"book's {competitor.buy_floor:g} floor"
                   + (f" — “{r['headline']}”" if r["headline"] else ""),
        }
        for r in ranked
    ]


def _plan(services, competitor, book, equity: float, cash: float) -> dict:
    """This competitor's action plan, at its own floor and its own cash.

    A fresh service each time rather than one held on the runner: it is
    stateless arithmetic over the advisor's cache, and the deployable balance
    it is sized against changes the moment a sale settles.
    """
    actions = AiActionsService(
        services.advisor, services.discover, services.market, services.portfolio,
        available=TradableCash(book.tradable_cash(equity, cash)),
        max_buys=competitor.max_buys, max_sells=competitor.max_sells,
        buy_floor=competitor.buy_floor,
        full_conviction_at=competitor.full_conviction_at,
    )
    return (actions.get().get("plans") or {}).get(competitor.risk) or {}


# --- the parts of the record that aren't the book ------------------------


def _thoughts(services, competitor) -> list:
    """Each researcher's note on the whole list, from all three columns.

    This is the "what were they thinking" half of the report, and it exists
    whether or not anything was traded — an agent that looked at twenty names
    and wanted none of them has said something, and a day with no trades is
    exactly the day you want to read it.

    All three columns, and the watchlist one is not optional. A book with no
    positions produces an empty holdings profile, so on day one — and on any
    later day an agent has sold out of everything — the holdings notes are the
    one place there is nothing to read. That is precisely the day the reasoning
    matters most, and it is sitting on the watchlist profile.
    """
    notes = []
    profile = ((services.advisor.get().get("risk_profiles") or {})
               .get(competitor.risk) or {})
    for note in profile.get("portfolio_notes") or []:
        notes.append({**note, "column": "what it holds"})
    for note in (profile.get("wishlist") or {}).get("portfolio_notes") or []:
        notes.append({**note, "column": "what it's watching"})

    discover = ((services.discover.get().get("risk_profiles") or {})
                .get(competitor.risk) or {})
    for note in discover.get("portfolio_notes") or []:
        notes.append({**note, "column": "what's in the news"})
    return notes


def _realized_total(services) -> float:
    """Everything this book has locked in by selling, since day one."""
    try:
        return round(
            sum(s.get("realized_gain") or 0.0 for s in services.sales.list_sales()), 2
        )
    except Exception:
        return 0.0


def _benchmark(services, ledger, contest) -> dict:
    """Buy-and-hold in the benchmark, bought with the same money on day one.

    Priced off the same closes as the books, so "did any of them beat just
    owning the index" is a comparison of like with like rather than of a
    marked book against a number from a website.
    """
    ticker = contest.get("benchmark") or BENCHMARK
    started = contest.get("starting_cash") or STARTING_CASH
    try:
        quote = (services.market_data.get_quotes([ticker]) or {}).get(ticker) or {}
        price = quote.get("price")
    except Exception:
        price = None
    if not price:
        return {"ticker": ticker, "price": None, "equity": None, "pnl_pct": None}

    first = (ledger.days() or [{}])[0].get("benchmark") or {}
    entry = first.get("entry_price") or price
    equity = started * price / entry
    return {
        "ticker": ticker,
        "price": round(price, 4),
        "entry_price": round(entry, 4),
        "equity": round(equity, 2),
        "pnl_pct": round((price - entry) / entry * 100, 2),
    }


def _point_cli_at(services, competitor) -> None:
    """Give each agent its own directory of raw model answers, when the
    provider is the local Claude CLI.

    That client saves every call to a fixed file named after the slot that
    produced it, so three portfolios scored one after another through the same
    twenty slots would each overwrite the last — and the record of what agent A
    was actually shown would survive for as long as it took B to start. A
    directory per competitor keeps all sixty, and makes it possible to open the
    exact prompt behind any line in any report.

    A no-op for every hosted provider, which has no such directory.
    """
    target = os.path.join(RESPONSES_DIR, competitor.key)
    for client in services.clients:
        if hasattr(client, "cache_dir"):
            client.cache_dir = target
