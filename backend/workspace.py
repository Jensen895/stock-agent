"""Multiple portfolios ("workspaces") — the memory that keeps them separate.

A *portfolio* here is a whole self-contained workspace: its own holdings, its own
wishlist, its own sales log, and its own AI suggestions. Switching portfolios
swaps every one of those at once, so two portfolios never mix.

The design keeps the existing layering untouched. Services still talk to a
``StorageBackend``; they never learn that portfolios exist. The trick is
``WorkspaceStorage``: a backend that, on every read/write, redirects to the file
belonging to whichever portfolio is *currently active*. Change the active
portfolio in the manager and every service instantly follows — no rewiring.

On-disk layout (all under ``data/``)::

    data/
      portfolios/
        index.json                # registry: active id + [{id, name, created_at}]
        <id>/
          portfolio.json
          wishlist.json
          sales.json
          ai_suggestions.json
          ai_weights.json         # how much each AI agent counts
          discover.json           # trending stocks you don't own or watch
        _archive/                 # deleted portfolios are moved here, never erased

The active portfolio id is persisted in ``index.json``, so restarting the app
reopens on the portfolio you were last looking at. Every write is atomic (via
``JSONStorage``), so an action is saved the moment it happens.
"""

import os
import shutil
import threading
import uuid
from datetime import datetime, timezone

from backend.storage import JSONStorage, StorageBackend

# The per-portfolio data files. Migrating legacy top-level data moves exactly
# these; creating a portfolio lazily creates them on first write.
#
# ``ai_weights.json`` holds how much each of the five AI agents counts toward
# that portfolio's blended score. It is per-portfolio for the same reason the
# risk toggle is: a speculative watchlist and a retirement account deserve to
# weigh the macro agent differently, and a weight set on one shouldn't quietly
# follow you to the other.
# ``discover.json`` holds the trending picks — stocks the market is talking
# about that this portfolio neither holds nor watches. Per-portfolio because
# the exclusions are: the same three names are a discovery for one portfolio
# and old news for another that already owns them.
DATA_FILES = (
    "portfolio.json",
    "wishlist.json",
    "sales.json",
    "ai_suggestions.json",
    "ai_weights.json",
    "discover.json",
)

# Each portfolio remembers its own AI-advisor risk toggle (which risk profile the
# UI shows). Stored on the portfolio's registry entry so it's part of the same
# per-portfolio memory as everything else.
VALID_RISKS = ("low", "high")
DEFAULT_RISK = "low"


class WorkspaceError(ValueError):
    """Raised on invalid portfolio operations (bad name, unknown id, ...)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkspaceStorage(StorageBackend):
    """A storage backend that always points at the *active* portfolio's file.

    Holds no data itself — it recomputes the target path on every access from the
    manager's current active portfolio, so switching portfolios repoints every
    service that was handed one of these. ``filename`` is the per-portfolio file
    this instance is responsible for (e.g. ``"portfolio.json"``).
    """

    def __init__(self, manager: "WorkspaceManager", filename: str):
        self._manager = manager
        self._filename = filename

    def load(self) -> dict:
        return JSONStorage(self._manager.active_file(self._filename)).load()

    def save(self, data: dict) -> None:
        JSONStorage(self._manager.active_file(self._filename)).save(data)


class WorkspaceManager:
    """Owns the registry of portfolios and which one is active.

    Thread-safe: the AI advisor's background scheduler reads the active portfolio
    while the UI thread may be switching it, so all registry access is guarded.
    """

    def __init__(self, root: str):
        # root is data/portfolios; legacy top-level files live in its parent.
        self.root = root
        self.registry_path = os.path.join(root, "index.json")
        self._archive_dir = os.path.join(root, "_archive")
        self._lock = threading.RLock()

        os.makedirs(self.root, exist_ok=True)
        self._registry = self._load_registry()
        self._migrate_legacy()
        self._ensure_default()

    # --- paths ----------------------------------------------------------

    def active_id(self) -> str:
        with self._lock:
            return self._registry["active"]

    def active_dir(self) -> str:
        return os.path.join(self.root, self.active_id())

    def active_file(self, filename: str) -> str:
        return os.path.join(self.active_dir(), filename)

    # --- registry queries -----------------------------------------------

    def state(self) -> dict:
        """The full picture the UI needs: every portfolio + which is active."""
        with self._lock:
            active = self._registry["active"]
            return {
                "active": active,
                "portfolios": [
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "created_at": p.get("created_at"),
                        "risk": p.get("risk", DEFAULT_RISK),
                        "active": p["id"] == active,
                    }
                    for p in self._registry["portfolios"]
                ],
            }

    # --- mutations ------------------------------------------------------

    def create(self, name: str) -> dict:
        """Create a new (empty) portfolio and switch to it. Returns its entry."""
        name = self._clean_name(name)
        with self._lock:
            pid = self._new_id()
            os.makedirs(os.path.join(self.root, pid), exist_ok=True)
            entry = {"id": pid, "name": name, "created_at": _now()}
            self._registry["portfolios"].append(entry)
            self._registry["active"] = pid  # start using it right away
            self._save_registry()
            return dict(entry)

    def rename(self, pid: str, name: str) -> dict:
        """Rename a portfolio. Returns its updated entry."""
        name = self._clean_name(name)
        with self._lock:
            entry = self._find(pid)
            entry["name"] = name
            self._save_registry()
            return dict(entry)

    def switch(self, pid: str) -> str:
        """Make ``pid`` the active portfolio. Returns the active id."""
        with self._lock:
            self._find(pid)  # validates existence
            self._registry["active"] = pid
            self._save_registry()
            return pid

    def get_risk(self, pid: str = None) -> str:
        """The AI-advisor risk toggle for a portfolio (active one if omitted)."""
        with self._lock:
            entry = self._find(pid or self._registry["active"])
            return entry.get("risk", DEFAULT_RISK)

    def set_risk(self, risk: str, pid: str = None) -> str:
        """Remember the AI-advisor risk toggle for a portfolio (active if
        omitted). Returns the stored risk."""
        risk = self._clean_risk(risk)
        with self._lock:
            entry = self._find(pid or self._registry["active"])
            entry["risk"] = risk
            self._save_registry()
            return risk

    def delete(self, pid: str) -> str:
        """Remove a portfolio. Its data is *archived*, not erased.

        Refuses to delete the last remaining portfolio. Deleting the active one
        moves the active pointer to another portfolio. Returns the id that is
        active afterwards, so callers can refresh derived state (e.g. the AI
        advisor's cache).
        """
        with self._lock:
            self._find(pid)  # validates existence
            if len(self._registry["portfolios"]) <= 1:
                raise WorkspaceError("You can't delete your only portfolio.")

            self._registry["portfolios"] = [
                p for p in self._registry["portfolios"] if p["id"] != pid
            ]
            if self._registry["active"] == pid:
                self._registry["active"] = self._registry["portfolios"][0]["id"]

            self._archive(pid)
            self._save_registry()
            return self._registry["active"]

    # --- internals ------------------------------------------------------

    def _find(self, pid: str) -> dict:
        for p in self._registry["portfolios"]:
            if p["id"] == pid:
                return p
        raise WorkspaceError("That portfolio doesn't exist.")

    @staticmethod
    def _clean_name(name) -> str:
        if not isinstance(name, str) or not name.strip():
            raise WorkspaceError("Portfolio name is required.")
        return name.strip()[:60]

    @staticmethod
    def _clean_risk(risk) -> str:
        if risk not in VALID_RISKS:
            raise WorkspaceError("Risk must be 'low' or 'high'.")
        return risk

    def _new_id(self) -> str:
        existing = {p["id"] for p in self._registry["portfolios"]}
        while True:
            pid = uuid.uuid4().hex[:8]
            if pid not in existing:
                return pid

    def _archive(self, pid: str) -> None:
        """Move a deleted portfolio's directory into _archive/ (never erase)."""
        src = os.path.join(self.root, pid)
        if not os.path.isdir(src):
            return
        os.makedirs(self._archive_dir, exist_ok=True)
        stamp = _now().replace(":", "").replace("-", "")[:15]
        shutil.move(src, os.path.join(self._archive_dir, f"{pid}_{stamp}"))

    # --- registry persistence ------------------------------------------

    def _load_registry(self) -> dict:
        try:
            data = JSONStorage(self.registry_path).load()
        except Exception:
            data = {}
        portfolios = data.get("portfolios") or []
        active = data.get("active")
        if active not in {p["id"] for p in portfolios}:
            active = portfolios[0]["id"] if portfolios else None
        return {"active": active, "portfolios": portfolios}

    def _save_registry(self) -> None:
        JSONStorage(self.registry_path).save(self._registry)

    def _migrate_legacy(self) -> None:
        """One-time import of the old single-portfolio layout.

        Older versions kept ``portfolio.json`` etc. directly under ``data/``.
        If a registry doesn't exist yet but those files do, fold them into a
        first "My Portfolio" so no history is lost.
        """
        if self._registry["portfolios"]:
            return
        legacy_dir = os.path.dirname(self.root)  # data/
        sources = {
            f: os.path.join(legacy_dir, f)
            for f in DATA_FILES
            if os.path.isfile(os.path.join(legacy_dir, f))
        }
        if not sources:
            return
        pid = self._new_id()
        dest = os.path.join(self.root, pid)
        os.makedirs(dest, exist_ok=True)
        for fname, src in sources.items():
            shutil.move(src, os.path.join(dest, fname))
        self._registry["portfolios"].append(
            {"id": pid, "name": "My Portfolio", "created_at": _now()}
        )
        self._registry["active"] = pid
        self._save_registry()

    def _ensure_default(self) -> None:
        """Guarantee at least one portfolio exists (fresh installs)."""
        if self._registry["portfolios"]:
            return
        self.create("My Portfolio")
