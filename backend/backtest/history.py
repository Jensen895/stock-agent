"""The prediction ledger — what the five agents said, on which day, about what.

One JSON file per portfolio, ``data/backtest/<portfolio-id>/history.json``,
appended to once a day. It is deliberately separate from everything the app
writes: ``ai_suggestions.json`` holds *the latest* prediction and is overwritten
on every refresh, so without this file yesterday's scores are gone by 9:31 and
there is nothing to back-test against.

What a row keeps, and why
-------------------------
The five **raw agent scores**, not just the blended average. The average is a
function of the weights, and the whole point of this exercise is to ask what the
weights should have been — which is only answerable if the inputs survive. From
the raw five, any weighting can be recomputed after the fact for free; from the
average, nothing can be recovered. The blended score is stored too, but as a
record of what the app actually showed on the day, not as the analysis input.

Both risk profiles and all three scopes are kept, because they are genuinely
different predictions made by the same agents:

    holdings    stocks owned, scored buy-more / hold / sell
    wishlist    stocks watched, scored buy-now / wait / avoid
    discover    trending stocks the portfolio neither owns nor watches

For the wishlist the ledger reads ``candidates`` rather than ``suggestions``.
The app filters the watchlist down to genuine buys before showing it, and
back-testing only the names that passed that filter would be selecting on the
predicted variable — the surviving scores would look good because low ones were
thrown away. ``candidates`` is the unfiltered set the filter ran on.

One prediction per scope per day
--------------------------------
Restarting the app generates again, and two generations on the same day are two
readings of the same evidence, not two independent observations. Recording both
would quietly double-count that day in every average downstream. So a snapshot
replaces any earlier one with the same (scope, market date) and the last one of
the day wins.

Storage is a single JSON document rather than a database because it stays small
(a few hundred rows a day, a few MB a year), because it has to be readable by
hand when a number looks wrong, and because the rest of this project stores
things exactly this way.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from backend.ai_agents import AGENT_KEYS

try:  # stdlib on 3.9+; only used to turn a UTC stamp into a market date
    from zoneinfo import ZoneInfo

    _EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - no tz database on this machine
    _EASTERN = None

SCHEMA_VERSION = 1

# The three bodies of predictions the app produces, all scored by the same five
# agents on the same 0-100 scale, which is what makes pooling them legitimate.
SCOPES = ("advisor", "discover")
KINDS = ("holdings", "wishlist", "discover")

# The market shuts at 16:00 ET. A prediction stamped after that could not have
# been acted on at that day's close, so it is anchored to the next one — see
# ``anchor_after`` and the outcome module that consumes it.
_CLOSE_HOUR = 16


def _parse_ts(value):
    """An ISO timestamp as an aware UTC datetime, or None if unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def market_date(value) -> str:
    """The US market calendar date a UTC timestamp falls on ("YYYY-MM-DD")."""
    dt = _parse_ts(value)
    if dt is None:
        return ""
    local = dt.astimezone(_EASTERN) if _EASTERN is not None else dt
    return local.date().isoformat()


def anchor_after(value) -> str:
    """The earliest date whose *close* a prediction could have been traded at.

    A refresh at the opening bell can be acted on at that afternoon's close, so
    the anchor is the same day. A refresh stamped after 16:00 ET cannot — that
    close is already history, and pricing the prediction at it would hand the
    agents several hours of hindsight and manufacture skill that was never
    there. Those are anchored to the next calendar day, which the outcome module
    then rolls forward to the next day the market was actually open.

    Returns "" when the timestamp is unusable, which drops the snapshot from
    scoring rather than guessing at a date for it.
    """
    dt = _parse_ts(value)
    if dt is None:
        return ""
    local = dt.astimezone(_EASTERN) if _EASTERN is not None else dt
    if local.hour >= _CLOSE_HOUR:
        local = local + timedelta(days=1)
    return local.date().isoformat()


def _scores_from(suggestion: dict) -> dict:
    """The five raw agent scores on one suggestion, keyed by agent."""
    out = {}
    for source in suggestion.get("sources") or []:
        if source.get("kind") != "agent":
            continue
        key = source.get("key")
        score = source.get("confidence")
        if key in AGENT_KEYS and isinstance(score, (int, float)):
            out[key] = float(score)
    return out


def _weights_from(suggestion: dict) -> dict:
    """The weights the app was using when this suggestion was blended."""
    out = {}
    for source in suggestion.get("sources") or []:
        if source.get("kind") == "agent" and source.get("key") in AGENT_KEYS:
            try:
                out[source["key"]] = float(source.get("weight") or 0.0)
            except (TypeError, ValueError):
                pass
    return out


def _row(suggestion: dict, kind: str, risk: str):
    """One prediction as a ledger row, or None if it carries no agent scores.

    Suggestions written before the five-agent split have ``kind: "model"``
    sources and nothing to attribute, so they are skipped rather than recorded
    as a mystery average.
    """
    ticker = (suggestion.get("ticker") or "").strip().upper()
    scores = _scores_from(suggestion)
    if not ticker or not scores:
        return None
    equal = round(sum(scores.values()) / len(scores), 2)
    blended = suggestion.get("confidence")
    return {
        "ticker": ticker,
        "kind": kind,
        "risk": risk,
        # What the panel showed on the day, under that day's weights.
        "blended": float(blended) if isinstance(blended, (int, float)) else None,
        # The same five scores with every agent counting once. This is the
        # weight-free reference every weighting is compared against, including
        # the optimiser's answer — an optimised weighting that cannot beat a
        # plain average has found nothing.
        "equal": equal,
        "action": suggestion.get("action"),
        "consensus": suggestion.get("consensus"),
        "horizon_months": suggestion.get("horizon_months"),
        "scores": scores,
        # Filled in later by outcomes.py; kept on the row so the ledger is
        # self-contained and a report can be regenerated with no network.
        "anchor": None,
        "anchor_close": None,
        "outcomes": {},
    }


def rows_from(latest: dict, scope: str) -> list:
    """Every scored prediction inside one persisted ``latest`` document.

    Handles both shapes the app writes — the advisor's (holdings plus a nested
    wishlist) and Discover's (one flat list of trending picks) — because they
    are the same structure with different lists hanging off it.
    """
    rows = []
    profiles = (latest or {}).get("risk_profiles") or {}
    if not isinstance(profiles, dict):
        return rows
    for risk, profile in sorted(profiles.items()):
        if not isinstance(profile, dict):
            continue
        primary = "discover" if scope == "discover" else "holdings"
        for suggestion in profile.get("suggestions") or []:
            row = _row(suggestion, primary, risk)
            if row:
                rows.append(row)
        wishlist = profile.get("wishlist")
        if isinstance(wishlist, dict):
            # candidates, not suggestions — the unfiltered set. See the module
            # docstring: back-testing the survivors of a score filter measures
            # the filter, not the agents.
            for suggestion in (
                wishlist.get("candidates") or wishlist.get("suggestions") or []
            ):
                row = _row(suggestion, "wishlist", risk)
                if row:
                    rows.append(row)
    return rows


def snapshot_from(latest: dict, scope: str):
    """One day's predictions as a snapshot, or None when there is nothing to
    record (no generation yet, a failed refresh, or pre-agent saved data)."""
    generated_at = (latest or {}).get("generated_at")
    date = market_date(generated_at)
    anchor = anchor_after(generated_at)
    if not date:
        return None
    rows = rows_from(latest, scope)
    if not rows:
        return None
    weights = {}
    for row in rows:
        weights = _weights_from_row(latest, row) or weights
        if weights:
            break
    models = sorted(
        {
            m
            for profile in (latest.get("risk_profiles") or {}).values()
            if isinstance(profile, dict)
            for m in (profile.get("models") or [])
        }
    )
    return {
        "scope": scope,
        "date": date,
        "generated_at": generated_at,
        # Every row shares this anchor; kept at snapshot level too so a glance
        # at the file shows whether a day was priced same-day or next-day.
        "anchor_requested": anchor,
        "models": models,
        "weights_at_capture": weights,
        "rows": rows,
    }


def _weights_from_row(latest: dict, row: dict) -> dict:
    """Recover the capture-time weights by re-finding the row's suggestion.

    Cheap and only done once per snapshot: the weights are a property of the
    portfolio, not of the ticker, so the first suggestion that carries them
    answers for all of them.
    """
    for profile in (latest.get("risk_profiles") or {}).values():
        if not isinstance(profile, dict):
            continue
        pools = [profile.get("suggestions") or []]
        wishlist = profile.get("wishlist")
        if isinstance(wishlist, dict):
            pools.append(wishlist.get("candidates") or [])
        for pool in pools:
            for suggestion in pool:
                weights = _weights_from(suggestion)
                if weights:
                    return weights
    return {}


class HistoryStore:
    """The ledger file for one portfolio.

    Loads and saves the whole document. That is fine at this size and it keeps
    every write atomic and every read a plain ``json.load`` — the file is meant
    to be opened and read by a human when a number looks wrong.
    """

    def __init__(self, path: str, portfolio_id: str = "", portfolio_name: str = ""):
        self.path = path
        self.portfolio_id = portfolio_id
        self.portfolio_name = portfolio_name

    # --- persistence ----------------------------------------------------

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        doc.setdefault("schema", SCHEMA_VERSION)
        doc.setdefault("portfolio_id", self.portfolio_id)
        doc.setdefault("portfolio_name", self.portfolio_name)
        doc.setdefault("agents", list(AGENT_KEYS))
        doc.setdefault("snapshots", [])
        if self.portfolio_name:
            doc["portfolio_name"] = self.portfolio_name
        return doc

    def save(self, doc: dict) -> None:
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        doc["snapshots"] = sorted(
            doc.get("snapshots") or [],
            key=lambda s: (s.get("date") or "", s.get("scope") or ""),
        )
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    # --- recording ------------------------------------------------------

    def record(self, latest: dict, scope: str) -> dict:
        """Fold one persisted ``latest`` document into the ledger.

        Returns ``{"status": ..., "rows": n, "date": ...}``. ``status`` is
        ``added`` for a new day, ``replaced`` when a later generation supersedes
        one already recorded for that day, ``duplicate`` when the same
        generation has been seen before, and ``skipped`` when there was nothing
        scorable in it. Safe to call as often as you like — this is what lets
        both the live hook and the catch-up command write to the same ledger.
        """
        snapshot = snapshot_from(latest, scope)
        if snapshot is None:
            return {"status": "skipped", "rows": 0, "date": None, "scope": scope}

        doc = self.load()
        snapshots = doc["snapshots"]
        for index, existing in enumerate(snapshots):
            if existing.get("scope") != scope or existing.get("date") != snapshot["date"]:
                continue
            if existing.get("generated_at") == snapshot["generated_at"]:
                return {
                    "status": "duplicate",
                    "rows": len(existing.get("rows") or []),
                    "date": snapshot["date"],
                    "scope": scope,
                }
            # A later run on the same day supersedes the earlier one, and takes
            # its outcomes with it: the prices already fetched are still correct
            # for any row whose ticker and anchor survived.
            snapshot["rows"] = _carry_outcomes(existing.get("rows") or [], snapshot["rows"])
            snapshots[index] = snapshot
            self.save(doc)
            return {
                "status": "replaced",
                "rows": len(snapshot["rows"]),
                "date": snapshot["date"],
                "scope": scope,
            }

        snapshots.append(snapshot)
        self.save(doc)
        return {
            "status": "added",
            "rows": len(snapshot["rows"]),
            "date": snapshot["date"],
            "scope": scope,
        }


class WorkspaceRecorder:
    """A recorder that always writes to the *active* portfolio's ledger.

    The same trick ``WorkspaceStorage`` uses: hold no state, recompute the path
    on every call from whichever portfolio the app currently has open. Switching
    portfolios in the UI therefore switches which ledger the next refresh lands
    in, with nothing to rewire — and two portfolios can never end up sharing a
    prediction history, which would make every number in the report meaningless.

    This is the object ``run.py`` hands to the advisor and to Discover. It is
    the only part of the back-test that the running app touches.
    """

    def __init__(self, manager, root: str):
        self._manager = manager
        self._root = root

    def _store(self) -> HistoryStore:
        pid = self._manager.active_id()
        name = ""
        for entry in self._manager.state().get("portfolios") or []:
            if entry.get("id") == pid:
                name = entry.get("name") or ""
                break
        return HistoryStore(
            os.path.join(self._root, pid, "history.json"),
            portfolio_id=pid,
            portfolio_name=name,
        )

    def record(self, latest: dict, scope: str) -> dict:
        return self._store().record(latest, scope)


def _carry_outcomes(old_rows: list, new_rows: list) -> list:
    """Keep prices already fetched when a snapshot is replaced.

    Matched on (ticker, kind, risk). The scores change — that is why the
    snapshot is being replaced — but what the stock did afterwards does not, so
    re-fetching it would be a network round trip to arrive at the same number.
    """
    prior = {
        (r.get("ticker"), r.get("kind"), r.get("risk")): r
        for r in old_rows
    }
    for row in new_rows:
        was = prior.get((row["ticker"], row["kind"], row["risk"]))
        if was and was.get("anchor"):
            row["anchor"] = was.get("anchor")
            row["anchor_close"] = was.get("anchor_close")
            row["outcomes"] = was.get("outcomes") or {}
    return new_rows
