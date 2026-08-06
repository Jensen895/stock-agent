"""Analyst data provider — what the big financial firms say, as evidence.

This is **testimony, not a vote**. The advisor's score comes from two AI models
and nothing else; this module hands those models the sell-side research —
Goldman Sachs, JP Morgan, Morgan Stanley, Wells Fargo and the rest — as one more
input to weigh alongside fundamentals, momentum and news. Each model reads it,
argues with it if it wants, and reaches its own number.

That is a deliberate change from an earlier design in which the street was
averaged in as a third score. Mechanical averaging turned out to be the wrong
tool: sell-side ratings are famously skewed to the bullish end (across a real
23-holding portfolio the consensus mean only ever ranged 1.3-3.5 of a nominal
1-5 scale, and 22 of 23 stocks scored "buy"), so any fixed mapping onto a
0-100 conviction scale is mis-centred by construction. A model, unlike an
average, can notice the skew and discount it — and can read *why* the desks
disagree rather than collapsing them to a mean. So no score is computed here
at all; the raw picture is passed through instead.

A self-contained I/O boundary like ``market_data.py`` and ``news_data.py``: the
rest of the app asks for a ticker's street view and never talks to a ratings
source directly. Swap the provider and nothing else changes.

Source: Yahoo Finance's ``quoteSummary`` endpoint, three modules:

  - ``financialData``          -> recommendationMean (the 1-5 consensus), how
                                  many analysts cover the stock, and the mean /
                                  high / low price targets.
  - ``recommendationTrend``    -> the full strong-buy .. strong-sell head count.
  - ``upgradeDowngradeHistory``-> individual firms' recent calls: who upgraded,
                                  who downgraded, to what, and at what target.

That endpoint needs a crumb, so this provider composes the existing
``MarketDataProvider`` (via its public ``fetch_quote_summary``) rather than
minting a second one — one cookie session for all of Yahoo.

Surfacing the contradictions
----------------------------
A consensus label hides the interesting part. "Buy, 2.07/5" can mean forty
desks quietly agreeing or a genuine fight — 22 buys against 3 strong sells,
with price targets from $215 to $400. The second is a much weaker signal than
the first, and it is exactly what a lone averaged number destroys.

So alongside the raw figures this module computes the disagreement explicitly —
the bull / neutral / bear split, how wide the price targets are spread relative
to their mean, what the low and high targets imply for the current price, and
how many firms upgraded versus downgraded recently — plus a one-line
``disagreement_note`` summarising it in plain English. The models get both the
numbers and the sentence.

Ratings move on the timescale of research notes, not quotes, so results are
cached for six hours. Everything degrades gracefully: any failure yields None
and the models simply reason without the street's view.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# Ratings change when a desk publishes, which is rarely. Cache accordingly.
_TTL_ANALYST = 21600  # seconds (6h)

# Individual firm calls to surface, and how far back to look for them. One
# quarter keeps the list current without going empty on thinly covered names.
# Eight rather than a handful: naming the firms on both sides of a split is the
# point, and a short list would hide one side of it.
_MAX_FIRM_ACTIONS = 8
_ACTION_LOOKBACK_DAYS = 120

_MODULES = ["financialData", "recommendationTrend", "upgradeDowngradeHistory"]

# The head-count buckets, grouped into camps for the bull / bear split.
_BULL_BUCKETS = ("strongBuy", "buy")
_NEUTRAL_BUCKETS = ("hold",)
_BEAR_BUCKETS = ("sell", "strongSell")
_BUCKETS = _BULL_BUCKETS + _NEUTRAL_BUCKETS + _BEAR_BUCKETS

# Yahoo's recommendationKey values, prettied up for display.
_RATING_LABELS = {
    "strong_buy": "Strong Buy",
    "buy": "Buy",
    "hold": "Hold",
    "underperform": "Underperform",
    "sell": "Sell",
    "none": "No rating",
}

# Yahoo's terse action codes, spelled out so a model doesn't have to guess.
_ACTION_LABELS = {
    "up": "upgraded",
    "down": "downgraded",
    "init": "initiated coverage",
    "main": "reiterated",
    "reit": "reiterated",
}


def _raw(value):
    """Unwrap a Yahoo number, which arrives as {"raw": 1.23, "fmt": "1.23"}."""
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class YahooAnalystProvider:
    """Wall Street consensus ratings for a ticker, from Yahoo Finance.

    Composes a ``MarketDataProvider`` for HTTP: it already holds the cookie
    session and crumb that ``quoteSummary`` requires, plus retry and caching.
    Any object exposing ``fetch_quote_summary(ticker, modules, ttl)`` works.
    """

    def __init__(self, session, max_workers: int = 8):
        self.session = session
        self.max_workers = max_workers

    # --- public API -----------------------------------------------------

    def available(self) -> bool:
        """Keyless, so always usable — outages surface as a missing view."""
        return self.session is not None

    def describe(self) -> str:
        """Human-readable source name, for the boot log and the UI."""
        return "Yahoo Finance analyst ratings"

    def get_ratings(self, ticker: str):
        """Return the street's view of one ticker, or None if nobody covers it.

        No score: this is evidence for the models to weigh, not a vote to be
        averaged. Shape::

            {"ticker",
             "rating", "mean",           # Yahoo's key + the 1-5 consensus
             "analyst_count",
             "distribution": {"strongBuy": .., .., "strongSell": ..},
             "bulls", "neutral", "bears",          # the head count, grouped
             "target": {"mean", "high", "low", "upside_pct",
                        "spread_pct",             # (high-low)/mean — dispersion
                        "low_implies_pct",        # vs. the current price
                        "high_implies_pct"},
             "recent_upgrades", "recent_downgrades",
             "firms": [{"firm", "grade", "action", "action_label", "date",
                        "price_target"}, ...],
             "disagreement_note": "22 of 41 rate it Buy while 5 rate it Sell; …",
             "summary": "41 analysts · Buy (2.07/5) · target $324.01 (+4.2%)"}
        """
        if not self.available():
            return None
        try:
            result = self.session.fetch_quote_summary(
                ticker, _MODULES, ttl=_TTL_ANALYST
            )
        except Exception:  # a ratings outage must never break the advisor
            return None
        if not result:
            return None
        return self._parse(ticker.upper(), result)

    def get_ratings_many(self, tickers) -> dict:
        """Fetch ratings for several tickers concurrently.
        Returns {ticker: ratings-or-None}."""
        tickers = list(tickers)
        if not tickers or not self.available():
            return {t: None for t in tickers}
        workers = min(self.max_workers, len(tickers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(zip(tickers, pool.map(self.get_ratings, tickers)))

    # --- parsing --------------------------------------------------------

    def _parse(self, ticker: str, result: dict):
        financial = result.get("financialData") or {}
        distribution = self._distribution(result)
        count = _raw(financial.get("numberOfAnalystOpinions"))
        mean = _raw(financial.get("recommendationMean"))

        analyst_count = int(count) if count else sum(distribution.values())
        if not analyst_count and mean is None:
            return None  # covered by nobody — the models just won't see a view

        price = _raw(financial.get("currentPrice"))
        target = self._target(financial, price)
        firms = self._firms(result)
        rating_key = financial.get("recommendationKey") or ""

        entry = {
            "ticker": ticker,
            "rating": _RATING_LABELS.get(
                rating_key, rating_key.replace("_", " ").title()
            ),
            # 1 = Strong Buy … 5 = Strong Sell. Passed through as Yahoo states
            # it; no rescaling, since nothing downstream averages it any more.
            "mean": round(mean, 2) if mean else None,
            "analyst_count": analyst_count,
            "distribution": distribution,
            "bulls": sum(distribution[b] for b in _BULL_BUCKETS),
            "neutral": sum(distribution[b] for b in _NEUTRAL_BUCKETS),
            "bears": sum(distribution[b] for b in _BEAR_BUCKETS),
            "target": target,
            "recent_upgrades": sum(1 for f in firms if f["action"] == "up"),
            "recent_downgrades": sum(1 for f in firms if f["action"] == "down"),
            "firms": firms,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        entry["disagreement_note"] = self._disagreement_note(entry)
        entry["summary"] = self._summarize(entry)
        return entry

    @staticmethod
    def _target(financial: dict, price):
        """Price targets plus how far apart they are — the dispersion is the
        disagreement, and it is invisible in the mean alone."""
        mean = _raw(financial.get("targetMeanPrice"))
        high = _raw(financial.get("targetHighPrice"))
        low = _raw(financial.get("targetLowPrice"))

        def _implies(value):
            if not value or not price:
                return None
            return round((value - price) / price * 100, 2)

        return {
            "mean": mean,
            "high": high,
            "low": low,
            "upside_pct": _implies(mean),
            # How wide the range is, as a share of the mean target.
            "spread_pct": (
                round((high - low) / mean * 100, 2) if high and low and mean else None
            ),
            # What the extremes would mean for the position from here.
            "low_implies_pct": _implies(low),
            "high_implies_pct": _implies(high),
        }

    @staticmethod
    def _distribution(result: dict) -> dict:
        """The current month's strong-buy .. strong-sell head count."""
        trend = (result.get("recommendationTrend") or {}).get("trend") or []
        # Yahoo lists "0m" (this month) first, then -1m, -2m, -3m.
        current = next(
            (t for t in trend if isinstance(t, dict) and t.get("period") == "0m"),
            trend[0] if trend and isinstance(trend[0], dict) else {},
        )
        out = {}
        for bucket in _BUCKETS:
            try:
                out[bucket] = int(current.get(bucket) or 0)
            except (TypeError, ValueError):
                out[bucket] = 0
        return out

    @staticmethod
    def _disagreement_note(entry: dict) -> str:
        """One plain-English line on how much the desks actually disagree.

        The models get the structured numbers too, but stating the split in a
        sentence makes it hard to skim past — which is the whole point of
        feeding them the contradictions rather than a tidy consensus.
        """
        bits = []
        bulls, neutral, bears = entry["bulls"], entry["neutral"], entry["bears"]
        total = bulls + neutral + bears
        if total:
            # "ratings", not "analysts" — Yahoo's numberOfAnalystOpinions and
            # its ratings head count are collected differently and rarely match.
            if bears and bulls:
                bits.append(
                    f"{bulls} of {total} ratings are buy while {bears} say sell "
                    f"and {neutral} sit on hold"
                )
            elif bulls and not bears:
                bits.append(
                    f"{bulls} of {total} ratings are buy with none saying sell"
                    + (f" and {neutral} on hold" if neutral else "")
                )
            else:
                bits.append(
                    f"{bulls} buy / {neutral} hold / {bears} sell across "
                    f"{total} ratings"
                )

        target = entry.get("target") or {}
        if target.get("low") and target.get("high"):
            span = f"price targets run ${target['low']:,.0f} to ${target['high']:,.0f}"
            if target.get("spread_pct"):
                span += f" ({target['spread_pct']:.0f}% of the mean target)"
            if (
                target.get("low_implies_pct") is not None
                and target.get("high_implies_pct") is not None
            ):
                span += (
                    f", implying anything from {target['low_implies_pct']:+.0f}% "
                    f"to {target['high_implies_pct']:+.0f}% from here"
                )
            bits.append(span)

        ups, downs = entry.get("recent_upgrades", 0), entry.get("recent_downgrades", 0)
        if ups or downs:
            bits.append(
                f"recently {ups} upgrade{'s' if ups != 1 else ''} and "
                f"{downs} downgrade{'s' if downs != 1 else ''}"
            )
        return "; ".join(bits)

    @staticmethod
    def _firms(result: dict) -> list:
        """The most recent call from each firm, newest first.

        One entry per firm — a desk that reiterates a rating weekly would
        otherwise crowd everyone else out of a six-item list.
        """
        history = (result.get("upgradeDowngradeHistory") or {}).get("history") or []
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_ACTION_LOOKBACK_DAYS)
        ).timestamp()

        seen, out = set(), []
        for item in sorted(
            (h for h in history if isinstance(h, dict) and h.get("firm")),
            key=lambda h: h.get("epochGradeDate") or 0,
            reverse=True,
        ):
            when = item.get("epochGradeDate")
            if when and float(when) < cutoff:
                break  # sorted newest first, so everything after is older too
            firm = item["firm"]
            if firm in seen:
                continue
            seen.add(firm)
            target = _raw(item.get("currentPriceTarget"))
            action = item.get("action") or ""
            out.append(
                {
                    "firm": firm,
                    "grade": item.get("toGrade") or "",
                    # "up" / "down" / "main" (reiterated) / "init" (new coverage)
                    "action": action,
                    # Spelled out, so a model reading this doesn't have to
                    # guess what "main" means.
                    "action_label": _ACTION_LABELS.get(action, action),
                    "from_grade": item.get("fromGrade") or "",
                    "date": _iso_from_epoch(when),
                    "price_target": target or None,
                }
            )
            if len(out) >= _MAX_FIRM_ACTIONS:
                break
        return out

    @staticmethod
    def _summarize(entry: dict) -> str:
        """One line for the UI: coverage, rating, and the average price target."""
        bits = []
        count = entry.get("analyst_count")
        if count:
            bits.append(f"{count} analyst{'s' if count != 1 else ''}")
        if entry.get("rating"):
            mean = entry.get("mean")
            bits.append(f"{entry['rating']}{f' ({mean}/5)' if mean else ''}")
        target = entry.get("target") or {}
        if target.get("mean"):
            upside = target.get("upside_pct")
            sign = "+" if upside and upside > 0 else ""
            bits.append(
                f"target ${target['mean']:,.2f}"
                + (f" ({sign}{upside}%)" if upside is not None else "")
            )
        return " · ".join(bits)


def _iso_from_epoch(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None
