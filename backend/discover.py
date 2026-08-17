"""Discover — three stocks the market is talking about that you don't own.

The rest of the app answers questions about a list you wrote: what are my
holdings worth, should I add to them, is this watchlist name a buy yet. This
answers the one question none of that can, because it starts from outside your
list — *what is everyone else talking about that you have not looked at?*

The pipeline, and why it is in this order:

  1. ``TrendingBoard`` (``trending_data.py``) reads three rooms — retail chatter,
     the financial press, and the WSJ — and ranks what they are discussing.
  2. Everything already in your holdings or on your wishlist is dropped. Those
     have panels of their own; a discover column that keeps recommending the
     stock you own most of has told you nothing.
  3. The top three survivors are enriched exactly like a wishlist name would
     be: live quote, company fundamentals, sell-side research, recent company
     news, and a year of price history.
  4. Those three go to the same five agents that score everything else, through
     ``AIAdvisorService.score_context``, at both risk settings.

Step 4 is the point. It would have been much less work to ask one model "what's
hot and should I buy it", and the answer would have been unfalsifiable — a
paragraph of confident prose about stocks the model may or may not have any
information on. Routing discoveries through the same five independent agents
means a stock found on Reddit is scored by the same statistics agent, on the
same 0-100 scale, under the same weights, as a stock you have held for a year.
The number in the discover column and the number in the advisor column mean the
same thing, and you can compare them directly.

What each pick carries, matching what the panel shows:

  trending    which lanes are talking about it, how loudly, and real headlines
              with links — the evidence that it belongs here at all
  background  what the company actually does, its sector, size, and where the
              price sits — the context you need before reading a score for a
              ticker you may never have heard of
  suggestion  the blended confidence score and all five agents' arguments,
              identical in shape to a holding's or a wishlist name's

Cost, and why the scheduler is careful
--------------------------------------
A discover refresh is five agents x two risk profiles = up to ten model calls,
on top of the advisor's twenty. On a free tier that is most of a daily
allowance, so this refreshes once per trading day at the opening bell like the
advisor, and ``_scheduler_loop`` waits for the advisor to finish before it
starts. Sharing a bell but not a moment keeps the burst half the size.

Everything degrades: no model configured -> the panel says so; the board coming
back empty -> "nothing new is trending"; agents failing -> the last good picks
stay on screen. Not financial advice.
"""

import threading
import time
from datetime import datetime, timezone

from backend.ai_advisor import _history_stats, last_market_open, next_market_open

# How many stocks the panel shows. Three is the ask, and it is also about as
# many unfamiliar companies as anyone will actually read about in a column.
_PICKS = 3

# The scheduler's tick, matching the advisor's.
_TICK_SECONDS = 60

# How long to let the advisor's own refresh run before starting ours anyway.
# Normally it finishes in well under a minute; this only exists so a wedged
# advisor can't keep the discover panel empty forever.
_ADVISOR_WAIT_SECONDS = 300

# The fundamentals that make up the "what is this company" card. Deliberately
# not the full set the statistics agent sees: this is orientation for a reader
# who has never heard of the ticker, not a screener.
_BACKGROUND_FIELDS = (
    "business_summary",
    "sector",
    "industry",
    "market_cap",
    "total_revenue",
    "revenue_growth_pct",
    "profit_margin_pct",
    "trailing_pe",
    "forward_pe",
    "beta",
    "week52_high",
    "week52_low",
    "dividend_yield_pct",
    "short_pct_of_float",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DiscoverService:
    """Finds what's trending, scores it with the five agents, caches the result.

    Composes the trending board with the same providers the advisor uses and
    with the advisor itself, which owns the agents and the weights. Holds no
    scoring logic of its own — deliberately, so that a discovered stock and a
    held stock are scored by exactly the same code.
    """

    def __init__(self, board, advisor, market, news, wishlist, portfolio,
                 fundamentals=None, analysts=None, storage=None, picks=_PICKS,
                 recorder=None):
        self.board = board
        self.advisor = advisor
        self.market = market
        self.news = news
        self.wishlist = wishlist
        self.portfolio = portfolio
        self.fundamentals = fundamentals
        self.analysts = analysts
        self.storage = storage
        self.picks = picks
        # Optional back-test ledger — see ``backend/backtest``. Not part of the
        # app; nothing here reads it back.
        self.recorder = recorder

        self._lock = threading.Lock()
        self._refreshing = False
        self._latest = self._load_persisted()

    # --- public API -----------------------------------------------------

    def available(self) -> bool:
        """True when there is both something to read and something to score with."""
        return self.board.available() and self.advisor.available()

    def get(self) -> dict:
        """The latest cached picks plus status, in the shape the UI reads."""
        latest = self._latest or {}
        upcoming = next_market_open()
        return {
            "configured": self.available(),
            "model_configured": self.advisor.available(),
            "sources": self.board.describe(),
            "refreshing": self._refreshing,
            "refresh_schedule": "daily at market open",
            "next_refresh": upcoming.isoformat() if upcoming else None,
            "generated_at": latest.get("generated_at"),
            "picks": latest.get("picks"),
            "risk_profiles": latest.get("risk_profiles"),
            # Which lanes answered and how much they returned, so a blocked
            # source shows up on screen instead of as a silent zero.
            "lanes": latest.get("lanes"),
            "considered": latest.get("considered"),
            "skipped_known": latest.get("skipped_known"),
            "model_errors": latest.get("model_errors"),
            "error": latest.get("error"),
        }

    def reload(self) -> None:
        """Re-read the now-active portfolio's picks.

        Picks are per-portfolio because the *exclusions* are: what counts as a
        discovery depends on what that portfolio already holds and watches.
        """
        with self._lock:
            self._latest = self._load_persisted()

    def request_refresh(self) -> bool:
        """Kick off a background regeneration. False if one is already running."""
        if not self.available() or self._refreshing:
            return False
        threading.Thread(target=self._safe_generate, daemon=True).start()
        return True

    def start_scheduler(self):
        """Generate on boot if nothing is cached, then daily at the bell."""
        if not self.available():
            print("Discover: no model configured — discover panel disabled.")
            return
        print(f"Discover: {self.board.describe()} — refreshing daily at the bell")
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    # --- scheduler ------------------------------------------------------

    def _scheduler_loop(self):
        if self._latest is None:
            self._wait_for_advisor()
            self._safe_generate()
        while True:
            time.sleep(_TICK_SECONDS)
            if self._refresh_due() and not self.advisor.refreshing():
                self._safe_generate()

    def _wait_for_advisor(self):
        """Let the advisor's boot generation finish before adding ten more calls.

        Both wake at the same moment on a cold start and both draw on the same
        per-minute quota; going at once means the advisor loses agents to rate
        limits and the discover picks are scored by whatever is left. Waiting is
        free — nobody is watching a panel that has never been generated.
        """
        deadline = time.monotonic() + _ADVISOR_WAIT_SECONDS
        while self.advisor.refreshing() and time.monotonic() < deadline:
            time.sleep(_TICK_SECONDS / 6)

    def _refresh_due(self) -> bool:
        """True when the last generation predates the most recent opening bell.

        Same rule as the advisor's, and for the same reasons — it catches up
        after a sleeping laptop and it cannot double-fire on a restart.
        """
        opened = last_market_open()
        if opened is None:
            return False
        last = self._generated_at_epoch()
        return last is None or last < opened.timestamp()

    def _safe_generate(self):
        """One generation, guarded so only one runs at a time. Never raises."""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        try:
            self._generate()
        except Exception as e:  # keep the last good picks on screen
            print(f"Discover: generation failed: {e}")
            if self._latest is None:
                self._latest = {
                    "generated_at": _now_iso(),
                    "picks": None,
                    "risk_profiles": None,
                    "error": str(e),
                }
        finally:
            self._refreshing = False

    # --- generation -----------------------------------------------------

    def _generate(self):
        known = self._known_tickers()
        board = self.board.top(count=self.picks, exclude=known)
        picks = board.get("picks") or []

        if not picks:
            # A real answer, not a failure: the board found nothing you don't
            # already own or watch. Persist it so the panel says so plainly.
            self._latest = {
                "generated_at": _now_iso(),
                "picks": [],
                "risk_profiles": None,
                "lanes": board.get("lanes"),
                "considered": board.get("considered"),
                "skipped_known": board.get("skipped_known"),
                "model_errors": None,
                "error": None,
            }
            self._persist(self._latest)
            print("Discover: nothing trending that isn't already known.")
            return

        tickers = [p["ticker"] for p in picks]
        enriched = self._enrich(tickers)
        context = self._context(enriched)

        street = {
            t: row.get("wall_street") for t, row in enriched.items()
            if row.get("wall_street")
        }
        # Scored as a watchlist: the investor doesn't own these, so the agents'
        # neutral point is "wait" rather than "hold", which is the right frame.
        profiles, errors = self.advisor.score_context(
            context, "wishlist", tickers, street, scope="discover"
        )
        if not profiles:
            raise RuntimeError("; ".join(errors) or "Every agent failed.")

        self._latest = {
            "generated_at": _now_iso(),
            "picks": [self._pick_payload(p, enriched.get(p["ticker"], {})) for p in picks],
            "risk_profiles": profiles,
            "lanes": board.get("lanes"),
            "considered": board.get("considered"),
            "skipped_known": board.get("skipped_known"),
            "model_errors": errors or None,
            "error": None,
        }
        self._persist(self._latest)
        print(
            f"Discover: refreshed at {self._latest['generated_at']} — "
            f"{', '.join(tickers)} from {board.get('considered')} names."
        )

    def _known_tickers(self) -> set:
        """Everything the investor already holds or watches.

        Read fresh on every generation rather than cached: a stock bought this
        morning must not turn up as a discovery this afternoon.
        """
        known = set()
        try:
            known.update(s["ticker"] for s in self.portfolio.list_stocks())
        except Exception:
            pass
        try:
            known.update(e["ticker"] for e in self.wishlist.list_wishlist())
        except Exception:
            pass
        return known

    # --- evidence -------------------------------------------------------

    def _enrich(self, tickers) -> dict:
        """Everything the agents and the background card need, per ticker.

        Each source is wrapped on its own: a ratings outage should cost the
        expert agent its evidence, not cost the whole panel its picks.
        """
        quotes = self._safely(lambda: self._quotes(tickers), {}, "quotes")
        # The full info, not just the date: a company that reported three days
        # ago and one reporting next week both have "an earnings date", and
        # labelling the first one "next earnings" is simply wrong. ``reported``
        # is what tells them apart.
        earnings = self._safely(
            lambda: self._provider().get_earnings_infos(tickers), {}, "earnings"
        )
        news = self._safely(lambda: self.news.get_news_many(tickers), {}, "news")
        fundamentals = self._safely(
            lambda: (
                self.fundamentals.get_fundamentals_many(tickers)
                if self.fundamentals is not None and self.fundamentals.available()
                else {}
            ),
            {},
            "fundamentals",
        )
        street = self._safely(
            lambda: (
                self.analysts.get_ratings_many(tickers)
                if self.analysts is not None and self.analysts.available()
                else {}
            ),
            {},
            "analyst ratings",
        )
        history = self._safely(lambda: self._history(tickers), {}, "price history")

        out = {}
        for ticker in tickers:
            quote = quotes.get(ticker) or {}
            info = earnings.get(ticker) or {}
            out[ticker] = {
                "ticker": ticker,
                "price": quote.get("price"),
                "open": quote.get("open"),
                "previous_close": quote.get("previous_close"),
                # The agents read the bare date; the panel reads the whole entry.
                "earnings_date": info.get("date"),
                "earnings": info or None,
                "fundamentals": fundamentals.get(ticker),
                "wall_street": street.get(ticker),
                "recent_news": news.get(ticker) or [],
                "history": history.get(ticker),
            }
        return out

    def _provider(self):
        return getattr(self.market, "provider", None)

    def _quotes(self, tickers) -> dict:
        provider = self._provider()
        if provider is None:
            return {}
        return provider.get_quotes(tickers) or {}

    def _history(self, tickers) -> dict:
        """A year of daily closes per ticker, measured into a pattern.

        The same treatment the advisor gives a wishlist name — reusing
        ``_history_stats`` rather than re-deriving it, so the personal agent
        reads an identically shaped payload whichever panel asked.
        """
        provider = self._provider()
        if provider is None:
            return {}
        daily = provider.get_daily_many(tickers) or {}
        return {t: _history_stats(daily.get(t)) for t in tickers}

    @staticmethod
    def _safely(fetch, fallback, label: str):
        try:
            return fetch() or fallback
        except Exception as e:
            print(f"Discover: {label} unavailable: {e}")
            return fallback

    def _context(self, enriched: dict) -> dict:
        """The agents' context, shaped exactly like the advisor's.

        The rows go under ``wishlist`` because that is the key the agents read
        for a not-owned stock — see ``ai_agents._rows``. ``holdings`` is empty
        and the portfolio-level figures are absent, which is correct: these
        stocks have nothing to do with the investor's book, and the personal
        agent should be reading the price pattern alone.

        ``available_cash`` is the exception, and it is here deliberately. A
        stock found in the news competes for the same money as one already on
        the wishlist, so leaving it out would mean the constraint applied to
        two of the three columns and this one scored as though capital were
        free — which is precisely the column most likely to talk you into
        something. None while the section is vacant.
        """
        return {
            "as_of": _now_iso(),
            "available_cash": self.advisor.available_cash(),
            "macro_news": self._safely(
                lambda: (
                    self.advisor.macro_news.get_macro_news()
                    if self.advisor.macro_available()
                    else []
                ),
                [],
                "macro news",
            ),
            "holdings": [],
            "wishlist": list(enriched.values()),
        }

    # --- payload --------------------------------------------------------

    def _pick_payload(self, pick: dict, row: dict) -> dict:
        """One pick as the UI reads it: why it's here, and what it is."""
        return {
            "ticker": pick["ticker"],
            "name": pick.get("name"),
            "price": row.get("price"),
            "previous_close": row.get("previous_close"),
            "change": _change(row.get("price"), row.get("previous_close")),
            "earnings": row.get("earnings"),
            "trending": {
                "score": pick.get("score"),
                "lanes": pick.get("lanes") or [],
                "mentions": pick.get("mentions") or {},
                "headlines": pick.get("headlines") or [],
            },
            "background": _background(row.get("fundamentals")),
        }

    # --- persistence ----------------------------------------------------

    def _load_persisted(self):
        """The saved picks, or None if there is nothing usable on disk.

        A saved record has to carry a ``picks`` list to be worth loading. The
        advisor tolerates its own older shapes because a stale suggestion is
        still a readable suggestion; here the panel is built entirely around
        per-pick trending evidence and background, and a record written before
        those existed renders as an empty column that never refills — the
        scheduler sees a cached result and waits for tomorrow's bell. Treating
        it as absent instead makes the next boot regenerate.
        """
        if self.storage is None:
            return None
        try:
            latest = (self.storage.load() or {}).get("latest")
        except Exception:
            return None
        if not isinstance(latest, dict):
            return None
        if not isinstance(latest.get("picks"), list):
            if latest.get("error"):
                return latest  # a recorded failure is a valid state
            print("Discover: ignoring saved picks in an older format.")
            return None
        return latest

    def _persist(self, latest: dict):
        if self.storage is None:
            return
        try:
            self.storage.save({"latest": latest})
        except Exception as e:
            print(f"Discover: could not persist picks: {e}")
        # Copy into the back-test ledger, when one is wired in. Same reasoning
        # as the advisor's: this file keeps only the newest set of picks, so
        # without the copy there is no history to measure. Optional, outside
        # the app, and never allowed to break a refresh.
        if self.recorder is None:
            return
        try:
            self.recorder.record(latest, "discover")
        except Exception as e:
            print(f"Discover: could not archive to the back-test ledger: {e}")

    def _generated_at_epoch(self):
        if not self._latest or not self._latest.get("generated_at"):
            return None
        try:
            return datetime.fromisoformat(self._latest["generated_at"]).timestamp()
        except (TypeError, ValueError):
            return None


def _background(fundamentals) -> dict:
    """The "what is this company" card — the named fields that have a value."""
    if not fundamentals:
        return {}
    return {
        field: fundamentals[field]
        for field in _BACKGROUND_FIELDS
        if fundamentals.get(field) is not None
    }


def _change(price, previous_close):
    """Today's move as ``{value, pct}``, or None without both prices."""
    if price is None or not previous_close:
        return None
    value = price - previous_close
    return {"value": round(value, 2), "pct": round(value / previous_close * 100, 2)}
