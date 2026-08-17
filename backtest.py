#!/usr/bin/env python3
"""Back-test the five agents. Not part of the app — run it yourself.

    python3 backtest.py                 # the daily run: record, price, review
    python3 backtest.py --help          # everything below, in short form

``run.py`` starts the assistant. This starts nothing: it reads what the
assistant has already written, measures it against what the market subsequently
did, and writes a report to ``data/backtest/<portfolio>/reports/``. No server,
no port, no UI.

The daily run does four things in order:

    record    Fold today's prediction into the ledger. Reads the same
              ``ai_suggestions.json`` and ``discover.json`` the app maintains,
              which hold only the *latest* prediction — this is what makes them
              history instead of a rolling overwrite. Idempotent, so running it
              twice costs nothing.

    score     Fetch daily closes and fill in what each prediction was followed
              by at 1, 5, 10, 21 and 63 trading days, alongside SPY's move over
              the same window. Only rows with something still missing are
              fetched.

    measure   Per-agent information coefficients, hit rates, dispersion, the
              agent-to-agent correlation matrix, a momentum baseline, and a
              weight search with an out-of-sample split. All deterministic.

    review    One Claude CLI call that reads the measurements and writes the
              judgement — including, when that is what the data says, that none
              of the five agents predicts anything.

Other commands:

    python3 backtest.py record          # capture today's prediction only
    python3 backtest.py score           # fill in outcomes only (fetches prices)
    python3 backtest.py report          # re-measure and re-review, no fetching
    python3 backtest.py status          # one screen: how much data there is
    python3 backtest.py show            # print the last report again
    python3 backtest.py export          # the metrics as JSON, on stdout

Useful flags:

    --portfolio ID | all | active       default: the portfolio the app has open
    --no-ai                             tables only, no model call, no cost
    --apply-weights                     write the recommendation into the live
                                        sliders, but only when both the
                                        out-of-sample test and the reviewer
                                        endorse it
    --horizon N                         lead the report with a different horizon
    --model NAME                        reviewer model (default opus)
    --quiet                             write the report without printing it
"""

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Same loader ``run.py`` uses, so ``.env`` settings apply here too."""
    for name in (".env", ".env.local"):
        path = os.path.join(_ROOT, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if v.endswith(","):
                        v = v[:-1].strip()
                    if k and v and k not in os.environ:
                        os.environ[k] = v
        except OSError:
            pass


_load_dotenv()
sys.path.insert(0, _ROOT)

from backend.ai_advisor import normalize_weights            # noqa: E402
from backend.ai_agents import AGENT_KEYS                     # noqa: E402
from backend.backtest import analyst, metrics, outcomes, report  # noqa: E402
from backend.backtest.history import HistoryStore            # noqa: E402
from backend.market_data import MarketDataProvider           # noqa: E402

DATA_DIR = os.path.join(_ROOT, "data")
PORTFOLIOS_DIR = os.path.join(DATA_DIR, "portfolios")
BACKTEST_DIR = os.path.join(DATA_DIR, "backtest")

# Which of the app's files hold predictions, and what to call the scope they
# belong to in the ledger.
SOURCES = (("ai_suggestions.json", "advisor"), ("discover.json", "discover"))


# --- reading the app's data (without disturbing it) ---------------------


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def portfolios(selector: str) -> list:
    """The portfolios to work on, as ``[{id, name}]``.

    Reads ``index.json`` directly rather than going through
    ``WorkspaceManager``, which creates a default portfolio when it finds none
    and rewrites the registry. This tool observes; it should not be able to
    change which portfolio the app opens on.
    """
    registry = _read_json(os.path.join(PORTFOLIOS_DIR, "index.json")) or {}
    entries = registry.get("portfolios") or []
    if not entries:
        return []
    if selector in ("active", "", None):
        active = registry.get("active") or entries[0]["id"]
        return [p for p in entries if p["id"] == active] or entries[:1]
    if selector == "all":
        return entries
    chosen = [p for p in entries if p["id"] == selector or p.get("name") == selector]
    if not chosen:
        raise SystemExit(
            f"backtest: no portfolio '{selector}'. Known: "
            + ", ".join(f"{p['id']} ({p['name']})" for p in entries)
        )
    return chosen


def _weights_for(pid: str) -> dict:
    saved = (_read_json(os.path.join(PORTFOLIOS_DIR, pid, "ai_weights.json")) or {}).get(
        "weights"
    )
    return normalize_weights(saved or {})


def _store_for(entry: dict) -> HistoryStore:
    return HistoryStore(
        os.path.join(BACKTEST_DIR, entry["id"], "history.json"),
        portfolio_id=entry["id"],
        portfolio_name=entry.get("name") or "",
    )


def _reports_dir(pid: str) -> str:
    return os.path.join(BACKTEST_DIR, pid, "reports")


# --- the steps ----------------------------------------------------------


def do_record(entry: dict, quiet: bool = False) -> list:
    """Fold whatever the app has on disk into the ledger."""
    store = _store_for(entry)
    results = []
    for filename, scope in SOURCES:
        latest = (_read_json(os.path.join(PORTFOLIOS_DIR, entry["id"], filename)) or {})
        latest = latest.get("latest")
        if not isinstance(latest, dict):
            continue
        result = store.record(latest, scope)
        results.append(result)
        if not quiet and result["status"] in ("added", "replaced"):
            print(
                f"backtest: {result['status']} {scope} {result['date']} "
                f"({result['rows']} predictions)"
            )
    if not quiet and not any(r["status"] in ("added", "replaced") for r in results):
        print("backtest: nothing new to record — today's prediction is already in.")
    return results


def do_score(entry: dict, quiet: bool = False) -> dict:
    """Fetch closes and fill in what happened next."""
    store = _store_for(entry)
    doc = store.load()
    if not doc["snapshots"]:
        if not quiet:
            print("backtest: ledger is empty — nothing to price.")
        return {}
    summary = outcomes.score(doc, MarketDataProvider(), verbose=not quiet)
    store.save(doc)
    if not quiet and summary:
        print(
            f"backtest: filled {summary['filled']} outcome(s); "
            f"{summary['pending']} window(s) still open."
        )
    return summary


def do_measure(entry: dict, horizon=None) -> dict:
    store = _store_for(entry)
    doc = store.load()
    result = metrics.compute(doc, _weights_for(entry["id"]))
    if horizon and str(horizon) in result["horizons"]:
        result["primary_horizon"] = str(horizon)
        result["verdict"] = metrics.verdict(result["horizons"][str(horizon)])
    return result


def _previous_review(pid: str):
    return _read_json(os.path.join(_reports_dir(pid), "last_review.json"))


def _save_review(pid: str, review: dict) -> None:
    directory = _reports_dir(pid)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "last_review.json"), "w", encoding="utf-8") as f:
        json.dump(review, f, indent=1, ensure_ascii=False)


def do_review(entry: dict, result: dict, use_ai: bool, model=None, quiet=False):
    """Run the reviewing agent. Returns ``(review, error_message)``."""
    if not use_ai:
        return None, "skipped (--no-ai)"
    reviewer = analyst.ClaudeAnalyst(model=model)
    if not reviewer.available():
        return None, (
            f"the '{reviewer.binary}' CLI is not on PATH — install Claude Code "
            "or set BACKTEST_CLAUDE_BIN"
        )
    if not quiet:
        print(f"backtest: reviewing with {reviewer.label} (this can take a minute)...")
    try:
        review = reviewer.review(result, _previous_review(entry["id"]))
    except RuntimeError as e:
        return None, str(e)
    _save_review(entry["id"], review)
    return review, ""


def _apply_weights(entry: dict, result: dict, review, quiet=False) -> bool:
    """Write the recommended weights into the app, if they have earned it.

    Two independent gates, both of which have to open. The out-of-sample test
    is the one that matters — an in-sample optimum over five free parameters
    and a few dozen days is not evidence of anything — and the reviewer's
    endorsement is a second opinion on top of it. A recommendation that only
    one of them likes is printed and not written.
    """
    horizon = result.get("primary_horizon")
    w = ((result.get("horizons") or {}).get(horizon) or {}).get("weights") or {}
    if w.get("verdict") != "holds-up-out-of-sample":
        print(
            "backtest: not applying weights — the search's answer did not "
            f"survive the held-out half ({w.get('verdict')})."
        )
        return False
    if review is not None and not review.get("apply_recommendation"):
        print("backtest: not applying weights — the reviewer declined to endorse them.")
        return False
    if review is None:
        # The out-of-sample test is the gate that matters, and it has opened —
        # but it is one gate, and "positive on the held-out half" is not the
        # same as "large enough to act on". Say so rather than let --no-ai
        # quietly halve the checks.
        print(
            "backtest: no reviewer ran, so this is the out-of-sample test's "
            "verdict alone. Held-out IC "
            f"{w.get('out_of_sample_ic')} vs {w.get('equal_weight_ic')} for "
            "equal weights — check that gap is worth acting on."
        )
    proposed = (review or {}).get("recommended_weights") or w.get("best_weights")
    if not proposed:
        print("backtest: not applying weights — no recommendation to apply.")
        return False

    weights = normalize_weights(proposed)
    path = os.path.join(PORTFOLIOS_DIR, entry["id"], "ai_weights.json")
    before = _weights_for(entry["id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"weights": weights}, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    if not quiet:
        print("backtest: applied new agent weights —")
        for key in AGENT_KEYS:
            print(f"    {key:<22} {before.get(key, 1.0):>5.2f} -> {weights[key]:>5.2f}")
        print("    (the app picks these up on its next reblend or restart)")
    return True


# --- commands -----------------------------------------------------------


def cmd_daily(args) -> int:
    for entry in portfolios(args.portfolio):
        print(f"\n=== {entry.get('name')} ({entry['id']}) ===")
        do_record(entry, args.quiet)
        do_score(entry, args.quiet)
        result = do_measure(entry, args.horizon)
        review, error = do_review(entry, result, not args.no_ai, args.model, args.quiet)
        markdown = report.render(result, review, error)
        path = report.write(_reports_dir(entry["id"]), markdown)
        if args.apply_weights:
            _apply_weights(entry, result, review, args.quiet)
        if args.quiet:
            print(f"backtest: wrote {path}")
        else:
            print("\n" + markdown)
            print(f"(saved to {path})")
    return 0


def cmd_record(args) -> int:
    for entry in portfolios(args.portfolio):
        do_record(entry, args.quiet)
    return 0


def cmd_score(args) -> int:
    for entry in portfolios(args.portfolio):
        do_score(entry, args.quiet)
    return 0


def cmd_report(args) -> int:
    """Re-measure and re-review without touching the network for prices."""
    for entry in portfolios(args.portfolio):
        result = do_measure(entry, args.horizon)
        review, error = do_review(entry, result, not args.no_ai, args.model, args.quiet)
        markdown = report.render(result, review, error)
        path = report.write(_reports_dir(entry["id"]), markdown)
        if args.apply_weights:
            _apply_weights(entry, result, review, args.quiet)
        print(markdown if not args.quiet else f"backtest: wrote {path}")
    return 0


def cmd_status(args) -> int:
    entries = portfolios(args.portfolio)
    if not entries:
        print("backtest: no portfolios yet — run the app once first.")
        return 1
    for entry in entries:
        doc = _store_for(entry).load()
        snapshots = doc.get("snapshots") or []
        days = sorted({s.get("date") for s in snapshots if s.get("date")})
        rows = sum(len(s.get("rows") or []) for s in snapshots)
        scored = sum(
            1
            for s in snapshots
            for r in (s.get("rows") or [])
            if (r.get("outcomes") or {}).get("21")
        )
        print(f"\n{entry.get('name')} ({entry['id']})")
        print(f"  days recorded    {len(days)}"
              + (f"   {days[0]} -> {days[-1]}" if days else ""))
        print(f"  predictions      {rows}")
        print(f"  scored at 21d    {scored}")
        for horizon in outcomes.HORIZONS:
            obs = metrics.observations(doc, horizon)
            sections = metrics.cross_sections(obs)
            note = "" if len(sections) >= metrics.MIN_DAYS_FOR_INFERENCE else \
                f"  (need {metrics.MIN_DAYS_FOR_INFERENCE - len(sections)} more to test)"
            print(f"  {horizon:>3}d horizon    {len(sections):>3} usable day(s)"
                  f", {len(obs):>4} observation(s){note}")
        latest = os.path.join(_reports_dir(entry["id"]), "latest.md")
        print(f"  last report      {latest if os.path.isfile(latest) else '— none yet'}")
    return 0


def cmd_show(args) -> int:
    for entry in portfolios(args.portfolio):
        path = os.path.join(_reports_dir(entry["id"]), "latest.md")
        if not os.path.isfile(path):
            print(f"backtest: no report yet for {entry['id']} — run `python3 backtest.py`.")
            continue
        with open(path, "r", encoding="utf-8") as f:
            print(f.read())
    return 0


def cmd_export(args) -> int:
    out = [do_measure(entry, args.horizon) for entry in portfolios(args.portfolio)]
    json.dump(out if len(out) != 1 else out[0], sys.stdout, indent=1, default=str)
    sys.stdout.write("\n")
    return 0


COMMANDS = {
    "daily": cmd_daily,
    "record": cmd_record,
    "score": cmd_score,
    "report": cmd_report,
    "status": cmd_status,
    "show": cmd_show,
    "export": cmd_export,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="backtest.py",
        description=__doc__.split("\n\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with no arguments for the full daily cycle.",
    )
    parser.add_argument("command", nargs="?", default="daily", choices=sorted(COMMANDS),
                        help="default: daily")
    parser.add_argument("--portfolio", default="active",
                        help="portfolio id or name, or 'all' (default: the active one)")
    parser.add_argument("--horizon", type=int, default=None,
                        help=f"lead with this horizon in trading days "
                             f"{outcomes.HORIZONS}")
    parser.add_argument("--no-ai", action="store_true",
                        help="skip the reviewing agent; tables only")
    parser.add_argument("--model", default=None,
                        help=f"reviewer model (default: {analyst.DEFAULT_MODEL})")
    parser.add_argument("--apply-weights", action="store_true",
                        help="write the recommendation into the app's weights, "
                             "but only if it survived the out-of-sample test")
    parser.add_argument("--quiet", action="store_true",
                        help="write the report without printing it")
    args = parser.parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
