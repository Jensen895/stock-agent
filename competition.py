#!/usr/bin/env python3
"""Three AI agents, $5,000 of fake money each, thirty sessions. Run it yourself.

    python3 competition.py init         # set the three books up, once
    python3 competition.py run          # today's session + today's report
    python3 competition.py watch        # sit and do that at every close
    python3 competition.py --help       # everything below, in short form

``run.py`` starts the assistant. This starts no server and opens no port: it
drives the same assistant from a script, three portfolios at a time, and writes
a report to ``data/competition/reports/``.

The three agents are the same five researchers from ``backend/ai_agents.py``
reading the same evidence, blended under three different weightings and three
different trading policies:

    A  The Quant              multiples and the balance sheet; low risk
    B  The Narrative Trader   the story and the tape; high risk
    C  The Committee          acts only on agreement; hoards cash

Set them up once with ``init``. After that a session is one command, and it is
safe to run twice — a session already in the ledger does nothing.

Commands:

    init            Create the three portfolios, fund them, seed the shared
                    universe onto every wishlist, and write the roster.
                    --reset archives an existing contest first.
    run             Score, trade and report one session. Defaults to the
                    session the current moment belongs to (today after the
                    close, the previous trading day before it).
    watch           Run at 16:05 ET every trading day until you stop it,
                    catching up first if a session was missed.
    report          Re-print a day's report from the ledger. No trading.
    standings       The whole month on one screen, plus who is doing what.
    status          Where the contest is up to, in a few lines.
    agents          The three personas — weights, policy, mandate.

Useful flags:

    --date YYYY-MM-DD    the session to run or report on
    --no-discover        skip the trending column (10 fewer model calls per
                         agent per day); the wishlist universe still trades
    --quiet              write the report without printing it
    --force              re-run a session already in the ledger, replacing
                         its row. It re-trades against today's prices on top
                         of the positions the first run left, so the book
                         moves; use it to repair a bad day, not casually.

Not financial advice. None of this money is real, and none of these numbers
should be acted on.
"""

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Same loader ``run.py`` uses, so ``.env`` settings apply here too."""
    from backend.wiring import load_dotenv

    load_dotenv(_ROOT)


sys.path.insert(0, _ROOT)
_load_dotenv()

from backend.competition import clock, report as reporting  # noqa: E402
from backend.competition.agents import COMPETITORS, SESSIONS, STARTING_CASH  # noqa: E402
from backend.competition.ledger import (  # noqa: E402
    Contest,
    ContestError,
    Ledger,
    write_report,
)
from backend.competition.runner import SessionSkipped, run_session, setup  # noqa: E402
from backend.wiring import build  # noqa: E402


# --- commands ------------------------------------------------------------


def cmd_init(args) -> int:
    print("Setting up the competition.")
    services = build(quiet=True)
    if not services.advisor.available():
        print(
            "No AI model is configured, so the agents would have nothing to "
            "think with. Set AI_PROVIDER and a key in .env first — see the "
            "'Run it' section of README.md."
        )
        return 1
    try:
        data = setup(services, reset=args.reset, cash=args.cash)
    except ContestError as e:
        print(e)
        return 1
    print(
        f"\nReady. {len(data['agents'])} agents, ${data['starting_cash']:,.0f} "
        f"each, {data['sessions']} sessions.\n"
        f"Run `python3 competition.py run` after today's close, or "
        f"`python3 competition.py watch` to have it happen by itself."
    )
    return 0


def cmd_run(args) -> int:
    services = build(quiet=True)
    try:
        row = run_session(
            services, date=args.date, use_discover=not args.no_discover,
            force=args.force,
        )
    except (ContestError, SessionSkipped) as e:
        print(e)
        return 0
    _emit(row, quiet=args.quiet)
    return 0


def cmd_watch(args) -> int:
    """Run at every close until interrupted, catching up on the way in."""
    services = build(quiet=True)
    print(
        f"Watching. A session runs {clock.SETTLE_MINUTES} minutes after each "
        f"close (16:{clock.SETTLE_MINUTES:02d} ET, Mon-Fri). Ctrl-C to stop."
    )
    while True:
        try:
            row = run_session(services, use_discover=not args.no_discover)
            _emit(row, quiet=args.quiet)
            if row["day"] >= row["sessions"]:
                print("\nThat was the last session. The contest is over.")
                return cmd_standings(args)
        except SessionSkipped as e:
            print(f"[{clock.now_utc().isoformat(timespec='seconds')}] {e}")
        except ContestError as e:
            print(e)
            return 1
        except Exception as e:  # a bad day is not a reason to stop watching
            print(f"[{clock.now_utc().isoformat(timespec='seconds')}] "
                  f"session failed: {type(e).__name__}: {e}")

        wait = clock.seconds_until_next_close() + 5
        upcoming = clock.next_close()
        print(f"Next session at {upcoming.isoformat(timespec='minutes')} "
              f"({wait / 3600:.1f}h away). Sleeping.")
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def cmd_report(args) -> int:
    ledger = Ledger()
    date = args.date or (ledger.latest() or {}).get("date")
    if not date:
        print("No sessions have been run yet.")
        return 0
    row = ledger.get(date)
    if not row:
        print(f"No session was recorded for {date}.")
        return 1
    _emit(row, quiet=args.quiet)
    return 0


def cmd_standings(args) -> int:
    try:
        contest = Contest().load()
    except ContestError as e:
        print(e)
        return 1
    print(reporting.standings(Ledger().days(), contest))
    return 0


def cmd_status(args) -> int:
    try:
        contest = Contest().load()
    except ContestError as e:
        print(e)
        return 1
    ledger = Ledger()
    days = ledger.days()
    latest = ledger.latest()

    print(f"Contest started {contest['created_at'][:10]} — "
          f"{len(days)} of {contest['sessions']} sessions run.")
    print(f"Universe: {len(contest['universe'])} names, "
          f"benchmark {contest.get('benchmark')}.")
    print(f"Next session: {clock.next_close().isoformat(timespec='minutes')} "
          f"(the session for {clock.session_date()} "
          f"{'is already recorded' if ledger.get(clock.session_date()) else 'is due'}).")
    print()
    if not latest:
        for competitor in COMPETITORS:
            entry = contest["agents"][competitor.key]
            print(f"  {competitor.key}  {competitor.name:<22} "
                  f"${contest['starting_cash']:>9,.2f}   workspace {entry['portfolio_id']}")
        return 0

    ranked = sorted((latest.get("agents") or {}).values(),
                    key=lambda a: -(a.get("equity") or 0))
    for i, agent in enumerate(ranked, 1):
        print(f"  {i}. {agent['key']}  {agent['name']:<22} "
              f"${agent['equity']:>9,.2f}  {agent['total_pnl_pct']:+6.2f}%  "
              f"{len(agent.get('positions') or [])} names, "
              f"${agent['cash']:,.2f} cash")
    bench = latest.get("benchmark") or {}
    if bench.get("pnl_pct") is not None:
        print(f"     {bench['ticker']} buy & hold        "
              f"${bench['equity']:>9,.2f}  {bench['pnl_pct']:+6.2f}%")
    return 0


def cmd_agents(args) -> int:
    print(f"Three agents, ${STARTING_CASH:,.0f} each, {SESSIONS} sessions.\n")
    for competitor in COMPETITORS:
        print(f"{competitor.key} — {competitor.name}")
        print(f"    {competitor.thesis}")
        print(f"    weights: {competitor.weights_summary()}")
        print(f"    policy:  {competitor.policy_summary()}")
        print(f"    remit:   {competitor.style}")
        print()
    return 0


# --- shared --------------------------------------------------------------


def _emit(row: dict, quiet: bool = False) -> None:
    """Render, save and (unless quiet) print one session's report."""
    text = reporting.daily(row)
    path = write_report(row["date"], text)
    if not quiet:
        print()
        print(text)
    print(f"Report written to {os.path.relpath(path, _ROOT)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="competition.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--date", help="session date, YYYY-MM-DD")
    parser.add_argument("--no-discover", action="store_true",
                        help="skip the trending column")
    parser.add_argument("--quiet", action="store_true",
                        help="write the report without printing it")
    parser.add_argument("--force", action="store_true",
                        help="re-run a session already in the ledger, replacing its row")
    parser.add_argument("--reset", action="store_true",
                        help="init: archive an existing contest first")
    parser.add_argument("--cash", type=float, default=STARTING_CASH,
                        help=f"init: starting money each (default {STARTING_CASH:g})")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("init", "run", "watch", "report", "standings",
                                 "status", "agents"))

    args = parser.parse_args(argv)
    commands = {
        "init": cmd_init,
        "run": cmd_run,
        "watch": cmd_watch,
        "report": cmd_report,
        "standings": cmd_standings,
        "status": cmd_status,
        "agents": cmd_agents,
    }
    try:
        return commands[args.command](args)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
