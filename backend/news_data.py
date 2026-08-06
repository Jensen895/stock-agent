"""News data providers — recent company headlines for the AI advisor.

A self-contained I/O boundary onto finance news, mirroring the design of
``market_data.py``: the rest of the app asks for headlines and never talks to a
news service directly. Swap a provider for another and nothing else changes.

Why this matters: the advisor's models will happily invent plausible headlines
if you give them none. Real headlines are what keep the suggestions honest, so
the default stack needs no API key at all and always has something to say.

Providers, all reached with the Python standard library only:

  - ``YahooNewsProvider``     — no key. Yahoo Finance's search endpoint, the
                                same host family ``market_data.py`` already uses
                                for prices.
  - ``GoogleNewsRSSProvider`` — no key. Google News RSS; broader coverage, used
                                as the fallback when Yahoo has nothing.
  - ``FinnhubNewsProvider``   — needs ``FINNHUB_API_KEY``. Richer summaries.
  - ``CompositeNewsProvider`` — tries providers in order per ticker and keeps
                                the first non-empty result.

Robustness, shared by all of them:
  - short-lived caching so repeated advisor runs don't hammer a source,
  - retry with backoff on 429 / transient errors,
  - graceful degradation: any failure yields an empty list rather than an
    exception, so the advisor still runs on price and history data alone.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# How far back to look for headlines, and how many to keep per ticker. The
# advisor reasons over the next one to three months, so a month of context is
# the right match — a week's worth would weight whatever happened to break in
# the last few days far above the quarter's actual story. Headlines are still
# capped per ticker, newest first, to keep the prompt small.
_LOOKBACK_DAYS = 30
_MAX_HEADLINES = 8

# Headlines barely change minute to minute; cache for a while to keep the number
# of outbound requests small even if the advisor is refreshed often.
_TTL_NEWS = 1800  # seconds (30 min)

# A minimal User-Agent. As in market_data.py, plain beats elaborate here.
_USER_AGENT = "Mozilla/5.0"


class _BaseNewsProvider:
    """Shared caching, concurrency, and HTTP retry for every news provider.

    Subclasses implement ``_fetch_company_news(ticker) -> list`` and return
    headline dicts of ``{headline, summary, source, url, datetime}``.
    """

    def __init__(self, timeout=8, max_retries=3, max_workers=8):
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_workers = max_workers
        self._cache = {}  # ticker -> (expires_at, headlines)

    # --- public API -----------------------------------------------------

    def available(self) -> bool:
        """True when this provider can be used at all (keyless ones always can)."""
        return True

    def get_company_news(self, ticker: str) -> list:
        """Return up to a handful of recent headlines for one ticker.

        Each headline is {headline, summary, source, url, datetime} (datetime is
        an ISO date string). Returns [] on any failure or when unconfigured.
        """
        if not self.available():
            return []

        ticker = ticker.upper()
        cached = self._cache.get(ticker)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        try:
            headlines = self._fetch_company_news(ticker) or []
        except Exception:  # a news outage must never break the advisor
            headlines = []
        self._cache[ticker] = (time.monotonic() + _TTL_NEWS, headlines)
        return headlines

    def get_news_many(self, tickers) -> dict:
        """Fetch news for several tickers concurrently. {ticker: [headline, …]}."""
        tickers = list(tickers)
        if not tickers:
            return {}
        if not self.available():
            return {t: [] for t in tickers}
        workers = min(self.max_workers, len(tickers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(self.get_company_news, tickers)
            return dict(zip(tickers, results))

    # --- to implement ---------------------------------------------------

    def _fetch_company_news(self, ticker: str) -> list:  # pragma: no cover
        raise NotImplementedError

    # --- low-level HTTP with retry --------------------------------------

    def _fetch_bytes(self, url: str):
        """GET a URL, retrying transient failures. Returns bytes or None."""
        backoff = 0.5
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"}
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                # 429 = rate limited; 5xx = transient. Retry those; give up on
                # the rest (401 for a bad key, 404 for an unknown symbol, ...).
                if e.code not in (429, 500, 502, 503, 504):
                    return None
            except Exception:
                pass  # timeout, DNS, connection reset — retry
            if attempt < self.max_retries - 1:
                time.sleep(backoff)
                backoff *= 2
        return None

    def _fetch_json(self, url: str):
        raw = self._fetch_bytes(url)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _iso_from_epoch(ts):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _cutoff_epoch() -> float:
        return (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).timestamp()


class YahooNewsProvider(_BaseNewsProvider):
    """Recent headlines from Yahoo Finance — no API key required.

    Uses the same search endpoint the Yahoo web UI calls for a quote page:

      GET https://query2.finance.yahoo.com/v1/finance/search?q=AAPL&newsCount=6
          -> {"news": [{title, publisher, link, providerPublishTime,
                        relatedTickers, ...}, ...]}

    Yahoo returns no article body here, so ``summary`` is left empty; the
    headline plus publisher is what the advisor reasons over.

    Important: this endpoint pads thin results with generic market filler — ask
    it about a symbol it doesn't know and it will hand back unrelated business
    news. Every item carries ``relatedTickers``, so we keep only the ones
    actually tagged with our symbol and return nothing otherwise, letting the
    composite fall through to Google News rather than feed the model noise.
    """

    SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"

    def _fetch_company_news(self, ticker: str) -> list:
        params = urllib.parse.urlencode(
            {
                "q": ticker,
                # Ask for a few extra so the filters still leave us enough.
                "newsCount": _MAX_HEADLINES * 3,
                "quotesCount": 0,
                "enableFuzzyQuery": "false",
            }
        )
        data = self._fetch_json(self.SEARCH_URL + "?" + params)
        if not isinstance(data, dict):
            return []

        cutoff = self._cutoff_epoch()
        items = []
        for item in data.get("news") or []:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            related = {
                str(t).upper() for t in (item.get("relatedTickers") or [])
            }
            if ticker not in related:
                continue  # generic filler, not news about this company
            ts = item.get("providerPublishTime")
            # Keep undated items — better a headline with no date than none.
            if ts and float(ts) < cutoff:
                continue
            items.append(item)

        items.sort(key=lambda d: d.get("providerPublishTime") or 0, reverse=True)
        return [
            {
                "headline": item.get("title"),
                "summary": "",
                "source": item.get("publisher"),
                "url": item.get("link"),
                "datetime": self._iso_from_epoch(item.get("providerPublishTime")),
            }
            for item in items[:_MAX_HEADLINES]
        ]


class GoogleNewsRSSProvider(_BaseNewsProvider):
    """Recent headlines from Google News RSS — no API key required.

    Broader (and noisier) than Yahoo, which makes it a good fallback for tickers
    Yahoo has nothing on.

      GET https://news.google.com/rss/search?q=AAPL+stock&hl=en-US&gl=US&ceid=US:en

    Item titles arrive as "Headline - Publisher"; the publisher is also in a
    ``<source>`` element, so we prefer that and strip the suffix.
    """

    RSS_URL = "https://news.google.com/rss/search"

    def _fetch_company_news(self, ticker: str) -> list:
        params = urllib.parse.urlencode(
            {
                "q": f"{ticker} stock",
                "hl": "en-US",
                "gl": "US",
                "ceid": "US:en",
            }
        )
        raw = self._fetch_bytes(self.RSS_URL + "?" + params)
        if not raw:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []

        cutoff = self._cutoff_epoch()
        out = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            when = self._parse_rfc822(item.findtext("pubDate"))
            if when is not None and when < cutoff:
                continue

            source_el = item.find("source")
            source = (source_el.text or "").strip() if source_el is not None else None
            # Titles read "Headline - Publisher"; drop the redundant suffix.
            if source and title.endswith(f" - {source}"):
                title = title[: -len(f" - {source}")]

            out.append(
                {
                    "headline": title,
                    "summary": "",
                    "source": source,
                    "url": (item.findtext("link") or "").strip() or None,
                    "datetime": self._iso_from_epoch(when) if when else None,
                    "_ts": when or 0,
                }
            )

        out.sort(key=lambda d: d["_ts"], reverse=True)
        for d in out:
            d.pop("_ts", None)
        return out[:_MAX_HEADLINES]

    @staticmethod
    def _parse_rfc822(value):
        """Parse an RSS pubDate ("Fri, 31 Jul 2026 08:13:58 GMT") to an epoch."""
        if not value:
            return None
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, IndexError):
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


class FinnhubNewsProvider(_BaseNewsProvider):
    """Recent headlines from Finnhub's ``company-news`` endpoint.

    Needs a free API key. Unlike the keyless sources it also returns an article
    summary, which gives the advisor more to work with.

      GET https://finnhub.io/api/v1/company-news?symbol=AAPL&from=…&to=…&token=…
    """

    NEWS_URL = "https://finnhub.io/api/v1/company-news"

    def __init__(self, api_key: str = None, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key

    def available(self) -> bool:
        """True when a Finnhub API key is configured."""
        return bool(self.api_key)

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
        data = self._fetch_json(self.NEWS_URL + "?" + params)
        if not isinstance(data, list):
            return []

        # Newest first, then keep only the most recent few with a real headline.
        items = sorted(
            (d for d in data if isinstance(d, dict) and d.get("headline")),
            key=lambda d: d.get("datetime", 0),
            reverse=True,
        )
        return [
            {
                "headline": item.get("headline"),
                "summary": (item.get("summary") or "")[:400],
                "source": item.get("source"),
                "url": item.get("url"),
                "datetime": self._iso_from_epoch(item.get("datetime")),
            }
            for item in items[:_MAX_HEADLINES]
        ]


# Backwards-compatible alias: this module used to expose only Finnhub.
NewsProvider = FinnhubNewsProvider


class CompositeNewsProvider(_BaseNewsProvider):
    """Tries several providers in order and keeps the first non-empty result.

    Per ticker, not per run — so a ticker Yahoo knows nothing about can still
    fall back to Google News while the rest keep using Yahoo.
    """

    def __init__(self, providers, **kwargs):
        super().__init__(**kwargs)
        self.providers = [p for p in providers if p is not None]

    def available(self) -> bool:
        return any(p.available() for p in self.providers)

    def describe(self) -> str:
        """Human-readable provider chain, for the boot log."""
        names = [
            type(p).__name__.replace("NewsProvider", "").replace("Provider", "")
            for p in self.providers
            if p.available()
        ]
        return " → ".join(names) if names else "none"

    def _fetch_company_news(self, ticker: str) -> list:
        for provider in self.providers:
            if not provider.available():
                continue
            # Delegate to the child's own cache/retry, not just its fetch.
            headlines = provider.get_company_news(ticker)
            if headlines:
                return headlines
        return []
