"""Market data provider — real-time price, open, history, and earnings date.

This is the app's window onto the outside world. Like the storage layer, it is a
self-contained I/O boundary: the rest of the app asks it for quotes / history /
earnings and never talks to any external service directly. Swap this class for a
different data source (a paid API, a mock in tests) and nothing else changes.

Source: Yahoo Finance's public (unofficial) endpoints, reached with the Python
standard library only — no third-party packages, matching the rest of the app.

  - chart   (v8/finance/chart)         -> current price, previous close, the
                                          day's open, the company's full name,
                                          and the price history that drives the
                                          unrealized-gains graph.
  - crumb   (v1/test/getcrumb)         -> a token, obtained via a cookie, that
                                          the quoteSummary endpoint now requires.
  - summary (v10/finance/quoteSummary) -> the earnings calendar and the
                                          actual-vs-expected EPS history, and
                                          (for ``analyst_data.py``) the Wall
                                          Street ratings modules.

``fetch_quote_summary`` is deliberately public: quoteSummary is the one endpoint
that needs a crumb, and minting one costs a cookie round-trip, so other Yahoo
providers compose this class instead of duplicating that dance.

Robustness (Yahoo is unofficial and rate-limits aggressively):
  - a cookie session + browser-like User-Agent,
  - retry with backoff on 429 / transient errors,
  - short-lived caching so a dashboard refresh doesn't hammer Yahoo,
  - graceful degradation: any failure yields None rather than an exception, so
    the app keeps working (showing "—") when a quote can't be fetched.
"""

import http.cookiejar
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
_COOKIE_SEED_URL = "https://fc.yahoo.com"

# A minimal User-Agent. Counter-intuitively, Yahoo rate-limits (429) elaborate
# browser-like UA strings far more aggressively than a plain one, so keep it
# simple — this reaches the endpoints reliably.
_USER_AGENT = "Mozilla/5.0"

# How long each kind of answer stays fresh in the cache. Quotes move constantly;
# daily history and earnings dates barely change intraday, so they're cached far
# longer to keep the number of outbound requests small.
_TTL_INTRADAY = 60        # seconds — current price + today's minute-by-minute
_TTL_DAILY = 3600         # seconds — daily closes for the "total" graph
_TTL_EARNINGS = 21600     # seconds (6h) — next earnings date
_TTL_SUMMARY = 21600      # seconds (6h) — default for other quoteSummary modules

# Fetch this much daily history for the long-horizon ("total") graph.
_TOTAL_RANGE = "1y"

# A report this fresh is worth more than the next scheduled date: right after a
# company reports, what it actually earned says more than a date three months
# out. Inside this window the Earnings column shows the report just delivered,
# with its result, instead of the next one.
_RECENT_REPORT_DAYS = 7

# Yahoo's earnings *history* can lag its calendar by a day or two after a
# report. Only pair a recent report with a history row whose fiscal quarter
# plausibly precedes it, so last quarter's numbers are never captioned as this
# week's result.
_MAX_QUARTER_LAG_DAYS = 120


def _number(value):
    """Unwrap a Yahoo number ({"raw": 1.23, "fmt": "1.23"}) as a float, or None
    when it's missing or not a number."""
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, 4)


class MarketDataProvider:
    def __init__(self, timeout=8, max_retries=3, max_workers=8):
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_workers = max_workers

        self._cache = {}                 # key -> (expires_at, value)
        self._cache_lock = threading.Lock()
        self._crumb = None
        self._crumb_lock = threading.Lock()

        # One cookie jar shared across requests; Yahoo hands out a cookie that
        # both cuts down on 429s and is required to mint a crumb.
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies)
        )
        self._opener.addheaders = [("User-Agent", _USER_AGENT), ("Accept", "*/*")]

    # --- public API -----------------------------------------------------

    def get_quote(self, ticker: str):
        """Return {price, open, previous_close, currency, name} for one ticker,
        or None if it can't be fetched.

        ``name`` is the company's full name ("Apple Inc."). It rides along on
        the chart response the price already comes from, so naming a holding
        costs no extra request.
        """
        chart = self._chart(ticker, rng="1d", interval="1m", ttl=_TTL_INTRADAY)
        if not chart or chart.get("price") is None:
            return None
        return {
            "price": chart["price"],
            "open": chart["open"],
            "previous_close": chart["previous_close"],
            "currency": chart["currency"],
            "name": chart["name"],
        }

    def get_quotes(self, tickers) -> dict:
        """Fetch several quotes concurrently. Returns {ticker: quote-or-None}."""
        return self._parallel(self.get_quote, tickers)

    def get_intraday_series(self, ticker: str):
        """Return (previous_close, [(ts_ms, close), ...]) for today, or
        (None, []) if unavailable. Drives the intraday "today" graph."""
        chart = self._chart(ticker, rng="1d", interval="1m", ttl=_TTL_INTRADAY)
        if not chart:
            return None, []
        return chart["previous_close"], chart["series"]

    def get_intraday_many(self, tickers) -> dict:
        """Fetch intraday series for several tickers concurrently.
        Returns {ticker: (previous_close, [(ts_ms, close), ...])}."""
        return self._parallel(self.get_intraday_series, tickers)

    def get_daily_series(self, ticker: str):
        """Return [(ts_ms, close), ...] of daily closes over the total-graph
        horizon, or [] if unavailable."""
        chart = self._chart(ticker, rng=_TOTAL_RANGE, interval="1d", ttl=_TTL_DAILY)
        return chart["series"] if chart else []

    def get_daily_many(self, tickers) -> dict:
        """Fetch daily series for several tickers concurrently.
        Returns {ticker: [(ts_ms, close), ...]}."""
        return self._parallel(self.get_daily_series, tickers)

    def get_earnings_info(self, ticker: str):
        """Return what's worth knowing about this ticker's earnings, or None:

            {"date": "YYYY-MM-DD", "reported": bool,
             "eps_actual": float, "eps_estimate": float, "surprise_pct": float}

        Usually ``date`` is the next scheduled report and ``reported`` is False.
        When the company reported within the last week, that date is returned
        instead with ``reported`` True and the quarter's actual-vs-expected EPS
        attached — a result just in beats a date months away. The EPS keys are
        absent when Yahoo hasn't published the numbers yet.
        """
        cache_key = ("earnings", ticker.upper())
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached or None  # cached "" means "looked up, none found"

        value = self._fetch_earnings_info(ticker)
        self._cache_set(cache_key, value or "", _TTL_EARNINGS)
        return value

    def get_earnings_infos(self, tickers) -> dict:
        """Fetch several earnings entries concurrently. {ticker: info-or-None}."""
        return self._parallel(self.get_earnings_info, tickers)

    def get_earnings_date(self, ticker: str):
        """Just the date from ``get_earnings_info`` — an ISO 'YYYY-MM-DD'
        string, or None if unknown."""
        info = self.get_earnings_info(ticker)
        return info["date"] if info else None

    def get_earnings_dates(self, tickers) -> dict:
        """Fetch several earnings dates concurrently. {ticker: iso-date-or-None}."""
        return self._parallel(self.get_earnings_date, tickers)

    def fetch_quote_summary(self, ticker: str, modules, ttl: int = _TTL_SUMMARY):
        """Fetch one or more Yahoo ``quoteSummary`` modules for a ticker.

        ``modules`` is a list of module names (e.g. ``["financialData",
        "recommendationTrend"]``); the return value is the result object keyed
        by module name, or None if it couldn't be fetched.

        Public because quoteSummary is the crumb-protected endpoint: any other
        provider that needs it (``analyst_data.py``) reuses this session rather
        than minting a second crumb of its own. Cached per (ticker, modules).
        """
        ticker = ticker.upper()
        names = sorted(modules)
        cache_key = ("summary", ticker, ",".join(names))
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached or None  # cached "" means "tried, failed"

        result = self._fetch_quote_summary(ticker, names)
        # Cache failures briefly so a flaky moment doesn't wedge every request.
        self._cache_set(cache_key, result or "", ttl if result else 60)
        return result

    def _fetch_quote_summary(self, ticker: str, names):
        # A crumb goes stale after a while and Yahoo then rejects the request,
        # so on a miss mint a fresh one and try exactly once more.
        for attempt in range(2):
            crumb = self._get_crumb()
            if not crumb:
                return None
            params = urllib.parse.urlencode(
                {"modules": ",".join(names), "crumb": crumb}
            )
            url = _SUMMARY_URL.format(ticker=urllib.parse.quote(ticker))
            data = self._fetch_json(url + "?" + params)
            if data:
                try:
                    return data["quoteSummary"]["result"][0]
                except (KeyError, IndexError, TypeError):
                    return None
            self._crumb = None
        return None

    # --- chart fetch + parse -------------------------------------------

    def _chart(self, ticker: str, rng: str, interval: str, ttl: int):
        """Fetch and parse a Yahoo chart response, cached by (ticker, rng,
        interval). Returns a dict {price, open, previous_close, currency,
        series:[(ts_ms, close)]} or None on failure."""
        ticker = ticker.upper()
        cache_key = ("chart", ticker, rng, interval)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached or None  # cached "" means "tried, failed"

        params = urllib.parse.urlencode(
            {"range": rng, "interval": interval, "includePrePost": "false"}
        )
        url = _CHART_URL.format(ticker=urllib.parse.quote(ticker)) + "?" + params
        data = self._fetch_json(url)

        parsed = self._parse_chart(data) if data else None
        # Cache successes for `ttl`; cache failures briefly so a flaky moment
        # doesn't wedge every request but we still retry soon.
        self._cache_set(cache_key, parsed or "", ttl if parsed else 15)
        return parsed

    @staticmethod
    def _parse_chart(data: dict):
        try:
            result = data["chart"]["result"][0]
        except (KeyError, IndexError, TypeError):
            return None

        meta = result.get("meta", {}) or {}
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        opens = quote.get("open") or []

        series = [
            (int(ts) * 1000, float(c))
            for ts, c in zip(timestamps, closes)
            if c is not None
        ]

        price = meta.get("regularMarketPrice")
        if price is None and series:
            price = series[-1][1]

        previous_close = meta.get("previousClose")
        if previous_close is None:
            previous_close = meta.get("chartPreviousClose")

        # Yahoo's chart meta omits the day's open, so take the first real open
        # from the intraday series; fall back to previous close.
        day_open = next((float(o) for o in opens if o is not None), None)
        if day_open is None:
            day_open = previous_close

        if price is None:
            return None

        return {
            "price": float(price),
            "open": float(day_open) if day_open is not None else None,
            "previous_close": (
                float(previous_close) if previous_close is not None else None
            ),
            "currency": meta.get("currency"),
            # longName is the full legal-ish name ("Apple Inc."); shortName is
            # the exchange's abbreviation, which is all some symbols carry.
            "name": meta.get("longName") or meta.get("shortName"),
            "series": series,
        }

    # --- earnings fetch -------------------------------------------------

    def _fetch_earnings_info(self, ticker: str):
        result = self.fetch_quote_summary(
            ticker, ["calendarEvents", "earningsHistory"], ttl=_TTL_EARNINGS
        )
        if not result:
            return None
        try:
            earnings = result["calendarEvents"]["earnings"] or {}
        except (KeyError, TypeError):
            return None

        # Take both date fields as candidates. Yahoo rolls ``earningsDate``
        # forward to the next quarter the moment a company reports, and leaves
        # ``earningsCallDate`` sitting on the call just held — so the pair is
        # what tells us a report has only just happened.
        stamps = sorted(
            set(
                self._stamps(earnings.get("earningsDate"))
                + self._stamps(earnings.get("earningsCallDate"))
            )
        )
        if not stamps:
            return None

        now = time.time()
        latest_past = max((s for s in stamps if s < now), default=None)
        upcoming = next((s for s in stamps if s >= now), None)

        if latest_past is not None and now - latest_past <= _RECENT_REPORT_DAYS * 86400:
            info = {"date": self._iso_date(latest_past), "reported": True}
            info.update(self._last_result(result, latest_past))
            return info

        chosen = upcoming if upcoming is not None else stamps[-1]
        return {"date": self._iso_date(chosen), "reported": chosen < now}

    @staticmethod
    def _stamps(raw_dates):
        """Unix timestamps out of a Yahoo date list — one or two {"raw": <unix>}
        entries (a single day, or an estimated window)."""
        return [
            d["raw"]
            for d in (raw_dates or [])
            if isinstance(d, dict) and d.get("raw")
        ]

    @staticmethod
    def _iso_date(stamp):
        return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _last_result(result: dict, reported_at: float) -> dict:
        """Actual vs. expected EPS for the quarter just reported, as
        {eps_actual, eps_estimate, surprise_pct}. Empty when Yahoo's history
        hasn't caught up with the calendar."""
        history = (result.get("earningsHistory") or {}).get("history") or []
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            quarter = item.get("quarter")
            end = quarter.get("raw") if isinstance(quarter, dict) else None
            # The quarter has to end before the report and not long before it;
            # anything older is a row Yahoo simply hasn't replaced yet.
            if end is None or not 0 <= reported_at - end <= _MAX_QUARTER_LAG_DAYS * 86400:
                continue
            actual = _number(item.get("epsActual"))
            if actual is None:
                continue
            estimate = _number(item.get("epsEstimate"))
            out = {"eps_actual": actual}
            if estimate is not None:
                out["eps_estimate"] = estimate
            # Yahoo's surprisePercent is a fraction (0.0674 = a 6.74% beat).
            surprise = _number(item.get("surprisePercent"))
            if surprise is not None:
                out["surprise_pct"] = round(surprise * 100, 2)
            elif estimate:
                out["surprise_pct"] = round(
                    (actual - estimate) / abs(estimate) * 100, 2
                )
            return out
        return {}

    def _get_crumb(self):
        """Lazily obtain (and cache) the crumb quoteSummary requires. Seeds a
        cookie first, since the crumb endpoint needs one."""
        if self._crumb:
            return self._crumb
        with self._crumb_lock:
            if self._crumb:
                return self._crumb
            # Seed cookies — this often 404s/errors but still sets the cookie.
            try:
                self._opener.open(_COOKIE_SEED_URL, timeout=self.timeout).read()
            except Exception:
                pass
            crumb = self._fetch_text(_CRUMB_URL)
            # A valid crumb is a short token; an HTML/error body is not.
            if crumb and "<" not in crumb and len(crumb) < 64:
                self._crumb = crumb.strip()
            return self._crumb

    # --- low-level HTTP with retry -------------------------------------

    def _fetch_json(self, url: str):
        text = self._fetch_text(url)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _fetch_text(self, url: str):
        """GET a URL, returning the body text, or None after exhausting retries.
        Backs off on 429 (rate limit) and transient network errors."""
        backoff = 0.5
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(url)
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                # 429 = rate limited; 5xx = transient. Retry those; give up on
                # the rest (404 for a bad ticker, etc.).
                if e.code not in (429, 500, 502, 503, 504):
                    return None
            except Exception:
                pass  # timeout, DNS, connection reset — retry
            if attempt < self.max_retries - 1:
                time.sleep(backoff)
                backoff *= 2
        return None

    # --- caching + concurrency helpers ---------------------------------

    def _cache_get(self, key):
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry and entry[0] > time.monotonic():
                return entry[1]
            if entry:
                del self._cache[key]
        return None

    def _cache_set(self, key, value, ttl):
        with self._cache_lock:
            self._cache[key] = (time.monotonic() + ttl, value)

    def _parallel(self, fn, tickers) -> dict:
        tickers = list(tickers)
        if not tickers:
            return {}
        workers = min(self.max_workers, len(tickers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(fn, tickers)
            return dict(zip(tickers, results))
