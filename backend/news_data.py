"""News data provider — recent company headlines for the AI advisor.

A self-contained I/O boundary onto a finance-news API, mirroring the design of
``market_data.py``: the rest of the app asks it for headlines and never talks to
the news service directly. Swap this class for a different source (another news
API, a mock in tests) and nothing else changes.

Source: Finnhub's ``company-news`` endpoint (free tier), reached with the Python
standard library only — no third-party packages, matching the rest of the app.

  GET https://finnhub.io/api/v1/company-news?symbol=AAPL&from=…&to=…&token=…
      -> a JSON list of {headline, summary, source, url, datetime, …}

Robustness:
  - short-lived caching so repeated advisor runs don't hammer the API,
  - retry with backoff on 429 / transient errors,
  - graceful degradation: any failure (including a missing API key) yields an
    empty list rather than an exception, so the AI advisor still runs on price
    and history data alone when news is unavailable.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

_NEWS_URL = "https://finnhub.io/api/v1/company-news"

# How far back to look for headlines, and how many to keep per ticker. A week of
# context is plenty for a suggestion horizon that is itself capped at a week.
_LOOKBACK_DAYS = 7
_MAX_HEADLINES = 6

# Headlines barely change minute to minute; cache for a while to keep the number
# of outbound requests small even if the advisor is refreshed often.
_TTL_NEWS = 1800  # seconds (30 min)

_USER_AGENT = "Mozilla/5.0"


class NewsProvider:
    """Recent company news headlines, keyed by ticker.

    Requires a Finnhub API key (``token``). Without one, every call degrades to
    an empty list so the advisor keeps working on market data alone.
    """

    def __init__(self, api_key: str = None, timeout=8, max_retries=3, max_workers=8):
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_workers = max_workers
        self._cache = {}  # ticker -> (expires_at, headlines)

    # --- public API -----------------------------------------------------

    def available(self) -> bool:
        """True when a news API key is configured."""
        return bool(self.api_key)

    def get_company_news(self, ticker: str) -> list:
        """Return up to a handful of recent headlines for one ticker.

        Each headline is {headline, summary, source, url, datetime} (datetime is
        an ISO date string). Returns [] on any failure or when unconfigured.
        """
        if not self.api_key:
            return []

        ticker = ticker.upper()
        cached = self._cache.get(ticker)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        headlines = self._fetch_company_news(ticker)
        self._cache[ticker] = (time.monotonic() + _TTL_NEWS, headlines)
        return headlines

    def get_news_many(self, tickers) -> dict:
        """Fetch news for several tickers concurrently. {ticker: [headline, …]}."""
        tickers = list(tickers)
        if not tickers or not self.api_key:
            return {t: [] for t in tickers}
        workers = min(self.max_workers, len(tickers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(self.get_company_news, tickers)
            return dict(zip(tickers, results))

    # --- fetch + parse --------------------------------------------------

    def _fetch_company_news(self, ticker: str) -> list:
        now = datetime.now(timezone.utc)
        params = urllib.parse.urlencode(
            {
                "symbol": ticker,
                "from": (now - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
                "to": now.strftime("%Y-%m-%d"),
                "token": self.api_key,
            }
        )
        data = self._fetch_json(_NEWS_URL + "?" + params)
        if not isinstance(data, list):
            return []

        # Newest first, then keep only the most recent few with a real headline.
        items = sorted(
            (d for d in data if isinstance(d, dict) and d.get("headline")),
            key=lambda d: d.get("datetime", 0),
            reverse=True,
        )
        out = []
        for item in items[:_MAX_HEADLINES]:
            ts = item.get("datetime")
            iso = (
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                if ts
                else None
            )
            out.append(
                {
                    "headline": item.get("headline"),
                    "summary": (item.get("summary") or "")[:400],
                    "source": item.get("source"),
                    "url": item.get("url"),
                    "datetime": iso,
                }
            )
        return out

    # --- low-level HTTP with retry -------------------------------------

    def _fetch_json(self, url: str):
        backoff = 0.5
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"}
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                # 429 = rate limited; 5xx = transient. Retry those; give up on
                # the rest (401 for a bad key, etc.).
                if e.code not in (429, 500, 502, 503, 504):
                    return None
            except (json.JSONDecodeError, Exception):
                pass  # bad body, timeout, DNS, connection reset — retry
            if attempt < self.max_retries - 1:
                time.sleep(backoff)
                backoff *= 2
        return None
