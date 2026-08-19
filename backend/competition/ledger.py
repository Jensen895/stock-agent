"""The contest's memory: who is playing, and what happened on each day.

Two files under ``data/competition/``:

    contest.json   Written once at ``init`` and read on every run. The roster
                   (which workspace id belongs to agent A), the universe, the
                   session count, when it started. Small, human-readable, and
                   the thing that makes a run resumable after a reboot.

    ledger.json    One append-only row per session date: every mark, every
                   trade, and every argument behind them. The reports are
                   rendered *from* this rather than alongside it, so a report
                   can be regenerated months later and a bug in the renderer
                   costs a re-render rather than the record.

Why the ledger keeps the reasoning and not just the numbers
-----------------------------------------------------------
Because the numbers alone cannot answer the question this exercise exists to
answer. "Agent A finished up 4.1%" is a fact about thirty coin flips unless you
can also read what A thought it was doing on the day it bought the thing that
worked. The per-agent arguments are already generated — the advisor attaches
all five to every suggestion — and they are perishable: ``ai_suggestions.json``
holds only the latest, so tomorrow's close overwrites today's reasoning. This
is the copy taken at the moment of the trade.

The session date is the primary key. Recording a date that is already present
replaces nothing and appends nothing — it returns the existing row — which is
what makes ``competition.py run`` safe to invoke twice, and what lets the
watcher fire on a schedule without worrying about a manual run that beat it.
"""

import os

from backend.storage import JSONStorage

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMPETITION_DIR = os.path.join(_ROOT, "data", "competition")
REPORTS_DIR = os.path.join(COMPETITION_DIR, "reports")

# Where each agent's raw model answers are kept when the provider is the local
# Claude CLI. Per agent, so A's twenty files and B's twenty files don't
# overwrite each other on the way past — see ``runner._point_cli_at``.
RESPONSES_DIR = os.path.join(COMPETITION_DIR, "responses")


class ContestError(RuntimeError):
    """Raised when the contest isn't in a state the command can work with."""


class Contest:
    """The roster and the clock — everything ``init`` decided, on disk."""

    def __init__(self, path: str = None):
        self.path = path or os.path.join(COMPETITION_DIR, "contest.json")
        self.storage = JSONStorage(self.path)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def load(self) -> dict:
        data = self.storage.load() or {}
        if not data.get("agents"):
            raise ContestError(
                "No contest has been set up yet. Run `python3 competition.py init`."
            )
        return data

    def save(self, data: dict) -> None:
        self.storage.save(data)

    def portfolio_id(self, key: str) -> str:
        """The workspace id agent ``key`` trades in."""
        agents = self.load()["agents"]
        if key not in agents:
            raise ContestError(f"There is no agent {key} in this contest.")
        return agents[key]["portfolio_id"]


class Ledger:
    """Append-only, one row per session date.

    Held as ``{"days": [row, ...]}`` sorted by date so the file reads top to
    bottom in the order the month happened, and so ``days[-1]`` is always
    yesterday's close.
    """

    def __init__(self, path: str = None):
        self.path = path or os.path.join(COMPETITION_DIR, "ledger.json")
        self.storage = JSONStorage(self.path)

    # --- reads ------------------------------------------------------------

    def days(self) -> list:
        return list((self.storage.load() or {}).get("days") or [])

    def get(self, date: str):
        """The row for one session date, or None."""
        for row in self.days():
            if row.get("date") == date:
                return row
        return None

    def latest(self):
        """The most recent row, or None before the first run."""
        days = self.days()
        return days[-1] if days else None

    def day_number(self) -> int:
        """Which session the *next* run will be — 1 before anything has run."""
        return len(self.days()) + 1

    def previous_equity(self, key: str):
        """What agent ``key`` was worth at the last close, or None on day one.

        The denominator for the day's move. Taken from the ledger rather than
        recomputed, so "today's P&L" is always measured against the number that
        was actually reported yesterday — including on a Monday, where the
        previous close is Friday's and a weekend gap belongs in the day's move
        rather than being quietly dropped.
        """
        last = self.latest()
        if not last:
            return None
        entry = (last.get("agents") or {}).get(key) or {}
        return entry.get("equity")

    # --- writes -----------------------------------------------------------

    def record(self, row: dict, replace: bool = False) -> dict:
        """Append one session. A date already present is returned untouched.

        Idempotent on purpose: the watcher and a manual ``run`` can both fire
        for the same close, and the second one must not book a second set of
        trades. The caller checks ``get(date)`` before doing any work; this is
        the backstop for the race where two processes pass that check at once.

        ``replace`` is what ``--force`` passes, and it exists because the
        backstop and the deliberate re-run want opposite things. A forced run
        has already re-scored and re-traded by the time it gets here; keeping
        the old row would leave the ledger describing a book that no longer
        exists, which is worse than either overwriting it or refusing to run.
        """
        doc = self.storage.load() or {}
        days = list(doc.get("days") or [])
        for i, existing in enumerate(days):
            if existing.get("date") == row.get("date"):
                if not replace:
                    return existing
                days[i] = row
                break
        else:
            days.append(row)
        days.sort(key=lambda r: r.get("date") or "")
        doc["days"] = days
        self.storage.save(doc)
        return row


def report_path(date: str) -> str:
    return os.path.join(REPORTS_DIR, f"{date}.md")


def write_report(date: str, text: str) -> str:
    """Save a rendered report and return where it went."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = report_path(date)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path
