"""What the market is actually talking about — Reddit, the news, and the WSJ.

A self-contained I/O boundary onto *chatter*, in the same shape as
``news_data.py``: the rest of the app asks "what is being talked about right
now" and never touches a feed itself.

The difference from ``news_data.py`` is the direction of the question. There,
you name a ticker and ask what was written about it. Here, nobody has named a
ticker yet — the tickers are the *answer*, mined out of what three very
different rooms are talking about:

    reddit   retail chatter — what individual investors are posting about
    news     the general financial press — market-wide headlines
    wsj      the Wall Street Journal specifically — the institutional read

Three lanes rather than one merged feed, because they disagree in useful ways.
A name that only Reddit is shouting about is a different proposition from one
the WSJ has run three pieces on, and both are different from one that shows up
in all three. The board reports the lanes separately and the UI shows them, so
that distinction survives all the way to the screen.

How a headline becomes a ticker
-------------------------------
Extraction, then *resolution*, and the second step is what makes this usable:

  1. ``_extract`` pulls candidates out of a headline — ``$NVDA`` cashtags,
     parenthesised symbols like "(NASDAQ: AMD)", "MU stock", and capitalised
     name phrases ("Berkshire Hathaway"), each with a weight reflecting how
     strong a signal it is.
  2. ``SymbolResolver`` asks Yahoo whether the candidate is a real US-listed
     equity, and takes the symbol and company name from the answer.

Step 2 is not a formality. Headlines are full of things that look exactly like
tickers and aren't — CNBC, FOREX, GDP, the WSJ itself — and a naive extractor
ranks the news network above the news. Yahoo answers "no equity" for every one
of those, so they never reach the board. The remaining false positives are
words that genuinely are also companies, which is why ``_NAME_STOP`` exists and
why a resolved name must share a word with the phrase that found it.

Ranking (``TrendingBoard.top``)
-------------------------------
Lane scores are normalised before they are added: a ticker's score in a lane is
its mentions as a fraction of that lane's busiest ticker. Without that, whichever
lane happened to return the most headlines would decide the board on volume
alone — the WSJ query returns hundreds of items and the Reddit lane a few dozen,
and that is a fact about feeds, not about attention. Normalising makes "loudest
on Reddit" and "loudest in the WSJ" worth the same, which is what someone
reading a three-lane board expects. Appearing in more than one lane then earns a
bonus, because that genuinely is a stronger signal than being loud in one place.

Robustness, as everywhere else in this app: every provider degrades to an empty
list rather than raising, each lane is independent, and a board built from two
lanes is still a board. ``describe()`` reports which sources actually answered,
so a blocked feed shows up as a note on screen instead of a silent zero.

A note on Reddit
----------------
Reddit's public endpoints (both ``.json`` and ``.rss``) are blocked outright from
some networks — they answer 403 with a "Blocked" page regardless of User-Agent.
That is why the retail lane is a *composite*: it tries Reddit first and falls
back to StockTwits, which is the same kind of signal (individual investors
talking about tickers, self-tagged) from a source that answers. If Reddit is
reachable where you run this, Reddit is what you get; if it isn't, the lane
still has something to say instead of dropping out and letting the two
professional feeds decide the whole board.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from backend.news_data import _BaseNewsProvider, _google_rss_url, _parse_google_rss

# Chatter is worth looking at over days, not weeks: "most talked about right
# now" means this week's story, and a fortnight's window would keep last
# month's blow-up at the top long after the room moved on.
_LOOKBACK_DAYS = 4

# Per lane. Enough to rank on without turning one busy feed into the board.
_MAX_HEADLINES = 240

# Chatter moves faster than company news, but not minute to minute, and a
# refresh is a few dozen requests. Fifteen minutes keeps it live and cheap.
_TTL_CHATTER = 900


# --- turning headlines into tickers -------------------------------------

# The strong forms: someone wrote the symbol down on purpose.
_CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")
_PAREN = re.compile(r"\(\s*(?:NYSE|NASDAQ|NYSEARCA|AMEX|OTC)?\s*:?\s*([A-Z]{1,5})\s*\)")
# Deliberately NOT case-insensitive. The symbol must be written in capitals;
# only the word after it may be capitalised or not. An ``re.I`` here makes
# ``[A-Z]{2,5}`` match lowercase too, which turns "Surges As Earnings Beat" into
# a mention of Amer Sports (AS) and "Tech Stocks Rally" into one of Bio-Techne
# (TECH) — both of which duly resolve to real companies and top the board.
_SUFFIXED = re.compile(
    r"\b([A-Z]{2,5})\b(?=\s+(?:[Ss]tocks?|[Ss]hares?|[Ee]arnings)\b)"
)

# The weak form: a capitalised phrase that might be a company. Up to three
# words, so "Advanced Micro Devices" survives and a whole clause doesn't.
_NAME = re.compile(
    r"\b([A-Z][a-zA-Z&'\-]*(?:\s+(?:[A-Z][a-zA-Z&'\-]*|of|and|&)){0,2})\b"
)

# How much each form of mention counts. A cashtag is someone naming the stock;
# a capitalised phrase is a guess that survived resolution. Rating them the same
# would let headline boilerplate outvote people actually discussing a ticker.
_W_CASHTAG = 3.0
_W_PAREN = 3.0
_W_SUFFIXED = 2.0
_W_NAME = 1.0
_W_TAGGED = 3.0  # a symbol the poster tagged themselves (StockTwits)

# Financial-headline furniture. A phrase built only from these words is never a
# company, so it is dropped before it costs a resolution request. This is a
# filter on *noise*, not a blocklist of companies: a phrase keeps its place the
# moment it contains one word that isn't in here.
_BOILERPLATE = set(
    """
    a about after ai amid an analysis analyst analysts and april are aren as at
    august before best beat beats big biggest billion buy by can close congress
    could cpi cut cuts day days deal deals december demand did does dow down
    drivers drop drops earnings economy eps etf europe even exclusive fall falls
    february fed federal for forecast friday from full futures gains gdp
    government growth guidance has have here higher history hold how i if in
    inflation into investing investment investors is it its january jobs july
    june keep know less lower loss lose loses market markets may miss misses
    million monday money more most moved movement movers need new news
    november now october of on open opened or out outlook over percent
    plunges premarket president price prices profit q1 q2 q3 q4 quarter
    quarterly quote quotes raise raises rally rating ratings report reports
    reserve results revenue rises roundup sales saturday says sell selloff
    senate september share shares should signal sinks soars stock stocks
    street sunday surge surges takes talk target targets tariff tariffs tech
    that the these this those thursday to today top trade traders trading
    treasury trillion tuesday tumbles under unveiled up update updates us usa
    was watch we wednesday week weeks were what when which who why will with
    worst would year years yesterday
    """.split()
)

# Phrases that resolve to a real listed company but are not what the headline is
# about. "Trump" finds Trump Media, "Berkshire" is fine but "Wall Street" is
# not. Short and deliberately so — the resolver catches almost everything else.
_NAME_STOP = {
    "dow jones", "federal reserve", "morning brief", "market talk", "new york",
    "s&p", "stock market", "trump", "united states", "wall street",
    "white house", "world",
}

# Symbols people write in headlines that are not the company being discussed.
_SYMBOL_STOP = {
    "AI", "AM", "APP", "CEO", "CFO", "CPI", "EPS", "ETF", "EU", "FDA", "FED",
    "GDP", "IPO", "IT", "NEW", "OK", "PM", "PPI", "SEC", "UK", "US", "USA",
    "USD", "WSJ",
}


def _clean_phrase(phrase: str):
    """A capitalised phrase, or None when it is headline furniture.

    Cheap rejection before the expensive step: every phrase that survives here
    costs one HTTP request to resolve, so anything built entirely out of the
    words financial headlines are made of is dropped now.
    """
    phrase = phrase.strip(" '&-")
    if len(phrase) < 3:
        return None
    low = phrase.lower()
    if low in _NAME_STOP:
        return None
    words = [w for w in re.split(r"[\s.]+", low) if w]
    # Needs at least one substantial word that isn't boilerplate — that word is
    # the only thing that could make this a company name.
    if not any(len(w) > 2 and w not in _BOILERPLATE for w in words):
        return None
    return phrase


def _name_variants(phrase: str) -> list:
    """A capitalised run, plus its leading one- and two-word prefixes.

    Headlines in the professional press lead with the company and follow it
    with a verb — "Airbnb Boosts Full-Year Forecast", "Thomson Reuters Lifts
    Revenue Guidance". A three-word grab therefore lands on "Airbnb Boosts
    Full-Year", which resolves to nothing, and the WSJ lane goes quiet while
    every one of its headlines names a company in its first word or two.

    Emitting the prefixes as well costs at most three candidates per run, and
    the extra ones are heavily deduplicated by the resolver's cache. Only one
    of them can score: ``_fold`` counts a headline once per *resolved ticker*,
    so "Thomson" and "Thomson Reuters" both landing on TRI is one mention.
    """
    words = phrase.split()
    seen, out = set(), []
    for length in (len(words), 2, 1):
        if length > len(words):
            continue
        cleaned = _clean_phrase(" ".join(words[:length]))
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _extract(text: str) -> dict:
    """One headline -> {candidate: weight}, before resolution.

    Symbol-shaped candidates are keyed by the bare symbol; name-shaped ones are
    prefixed with ``~`` so ``SymbolResolver`` knows which kind of lookup to do
    and a company called "MU" can't be confused with the ticker MU.
    """
    found = {}
    if not text:
        return found

    def bump(key, weight):
        found[key] = max(found.get(key, 0.0), weight)

    for match in _CASHTAG.findall(text):
        symbol = match.upper()
        if symbol not in _SYMBOL_STOP:
            bump(symbol, _W_CASHTAG)
    for match in _PAREN.findall(text):
        symbol = match.upper()
        if symbol not in _SYMBOL_STOP:
            bump(symbol, _W_PAREN)
    for match in _SUFFIXED.findall(text):
        symbol = match.upper()
        if symbol not in _SYMBOL_STOP:
            bump(symbol, _W_SUFFIXED)
    for match in _NAME.findall(text):
        for phrase in _name_variants(match):
            bump("~" + phrase, _W_NAME)
    return found


# --- resolution: is this candidate a real company? ----------------------

# Yahoo's own search, the same host family ``market_data.py`` and
# ``news_data.py`` already use. No key.
_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"

# US listings only. A German or Indian line of the same company would price and
# report in another currency, and the rest of the app assumes USD.
_US_EXCHANGES = {
    "NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS", "NSI", "NYS", "AMX",
}

# Resolutions barely change — a company's ticker is not news. Cache them for the
# process lifetime; the miss cost is one request and the hit rate is very high
# because the same names recur across lanes and across refreshes.
_TTL_RESOLVE = 86400


class SymbolResolver:
    """Turns a candidate string into a real US-listed equity, or nothing.

    This is the filter that makes headline mining work. "CNBC", "Wall Street",
    "Weak Jobs Report" and "GDP" all look plausible to a regular expression and
    all fail here, because Yahoo has no US equity by those names. What survives
    is a symbol that can be quoted, charted and scored like any other holding.
    """

    def __init__(self, timeout: int = 8, max_workers: int = 8):
        self.timeout = timeout
        self.max_workers = max_workers
        self._cache = {}  # candidate -> (expires_at, resolved-or-None)

    def resolve_many(self, candidates) -> dict:
        """{candidate: {ticker, name}} for the ones that are real companies."""
        candidates = list(candidates)
        if not candidates:
            return {}
        workers = min(self.max_workers, len(candidates))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(self.resolve, candidates)
        return {c: r for c, r in zip(candidates, results) if r}

    def resolve(self, candidate: str):
        """{ticker, name} for one candidate, or None if it isn't a company."""
        cached = self._cache.get(candidate)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            resolved = self._lookup(candidate)
        except Exception:  # a search outage costs one candidate, not the board
            resolved = None
        self._cache[candidate] = (time.monotonic() + _TTL_RESOLVE, resolved)
        return resolved

    # --- internals ------------------------------------------------------

    def _lookup(self, candidate: str):
        is_name = candidate.startswith("~")
        query = candidate[1:] if is_name else candidate
        params = urllib.parse.urlencode(
            {
                "q": query,
                "quotesCount": 4,
                "newsCount": 0,
                "enableFuzzyQuery": "false",
            }
        )
        raw = _fetch_bytes(_SEARCH_URL + "?" + params, self.timeout)
        if raw is None:
            return None
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None

        for quote in data.get("quotes") or []:
            if not isinstance(quote, dict):
                continue
            if quote.get("quoteType") != "EQUITY":
                continue
            symbol = (quote.get("symbol") or "").upper()
            # Foreign lines carry a suffix ("SIE.DE"); we want the US listing.
            if not symbol or "." in symbol:
                continue
            if quote.get("exchange") not in _US_EXCHANGES:
                continue
            name = quote.get("shortname") or quote.get("longname") or symbol
            if is_name:
                # Fuzzy search will happily return *something* for "Watch" or
                # "Demand". Insist the company it found actually contains the
                # word that found it, or this is a coincidence, not a match.
                if not _name_matches(query, name):
                    continue
            elif symbol != query.upper():
                # A symbol-shaped candidate has to be that symbol. Yahoo
                # helpfully suggests neighbours; we don't want them.
                continue
            return {"ticker": symbol, "name": name}
        return None


# Corporate-form words, which carry no identity: every third company is an Inc.
_CORPORATE = {
    "and", "co", "company", "corp", "corporation", "group", "holdings", "inc",
    "incorporated", "limited", "ltd", "llc", "nv", "of", "plc", "sa", "the",
}


def _name_words(text: str) -> list:
    """The identifying words of a name, in order, lowercased."""
    return [
        w
        for w in re.split(r"[^a-z0-9]+", text.lower())
        if len(w) > 2 and w not in _CORPORATE
    ]


def _name_matches(phrase: str, name: str) -> bool:
    """True when a resolved company really is the one the phrase named.

    Two rules, because a one-word candidate is a much weaker claim than a
    three-word one:

      - Multi-word phrases only have to share a substantive word. "Berkshire
        Hathaway" against "Berkshire Hathaway Inc. New" is obviously the same
        company, and so is "Thomson Reuters" against "Thomson Reuters Corp".
      - A single word has to be the company's *first* word. A company known by
        one word is known by the word it starts with — Airbnb, Uber, Atlassian,
        Nvidia. Merely appearing somewhere in the name is not enough: it lets
        the headline furniture in "Key Drivers Unveiled" match BIO-key
        International, which then rides three unrelated headlines onto the
        board. That one is not a hypothetical; it happened.
    """
    wanted = _name_words(phrase)
    have = _name_words(name)
    if not wanted or not have:
        return False
    if len(wanted) == 1:
        return wanted[0] == have[0]
    return bool(set(wanted) & set(have))


def _fetch_bytes(url: str, timeout: int, retries: int = 3):
    """GET a URL, retrying transient failures. Returns bytes or None.

    The same policy as ``news_data._BaseNewsProvider._fetch_bytes``, repeated
    here because the resolver is not a news provider and inheriting the whole
    per-ticker caching machinery to borrow one method would be worse.
    """
    backoff = 0.5
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                return None
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(backoff)
            backoff *= 2
    return None


# --- the lanes ----------------------------------------------------------


class _ChatterSource:
    """One place people talk about stocks. Returns headlines, never raises.

    Each source produces the same headline dict the news providers do —
    ``{headline, summary, source, url, datetime}`` — optionally with a
    ``symbols`` list when the source tags tickers itself, which spares that
    source the extraction guesswork entirely.
    """

    lane = ""
    label = ""

    def __init__(self, timeout: int = 8, max_workers: int = 6):
        self.timeout = timeout
        self.max_workers = max_workers

    def available(self) -> bool:
        return True

    def collect(self) -> list:
        """Recent posts/headlines from this source. [] on any failure."""
        try:
            return self._collect() or []
        except Exception:
            return []

    def _collect(self) -> list:  # pragma: no cover - abstract
        raise NotImplementedError

    # --- helpers --------------------------------------------------------

    def _cutoff(self) -> float:
        return (
            datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
        ).timestamp()

    def _rss(self, queries) -> list:
        """Several Google News RSS queries, merged newest-first."""
        cutoff = self._cutoff()
        workers = min(self.max_workers, len(queries)) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            feeds = pool.map(
                lambda q: _parse_google_rss(
                    _fetch_bytes(_google_rss_url(q), self.timeout), cutoff
                ),
                queries,
            )
            merged = [item for feed in feeds for item in feed]
        seen, out = set(), []
        for item in sorted(merged, key=lambda d: d.get("_ts") or 0, reverse=True):
            key = item["headline"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            item.pop("_ts", None)
            out.append(item)
        return out[:_MAX_HEADLINES]


class RedditChatterSource(_ChatterSource):
    """Hot posts from the investing subreddits, via Reddit's public JSON.

    No key: ``https://www.reddit.com/r/<sub>/hot.json`` is the same data the
    website renders. Post titles are where retail names tickers, usually as
    cashtags, so extraction has a lot to work with.

    Reddit blocks some networks outright (403 with a "Blocked" page, whatever
    User-Agent you send). That is not an error we can fix from here, so it
    degrades to an empty list and ``RetailChatterSource`` falls through to
    StockTwits.
    """

    lane = "reddit"
    label = "Reddit"

    SUBREDDITS = ("wallstreetbets", "stocks", "investing", "StockMarket")
    POSTS_PER_SUB = 60

    def _collect(self) -> list:
        cutoff = self._cutoff()
        workers = min(self.max_workers, len(self.SUBREDDITS))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            batches = pool.map(
                lambda sub: self._subreddit(sub, cutoff), self.SUBREDDITS
            )
            posts = [p for batch in batches for p in batch]
        posts.sort(key=lambda d: d.get("_ts") or 0, reverse=True)
        for post in posts:
            post.pop("_ts", None)
        return posts[:_MAX_HEADLINES]

    def _subreddit(self, sub: str, cutoff: float) -> list:
        url = (
            f"https://www.reddit.com/r/{sub}/hot.json"
            f"?limit={self.POSTS_PER_SUB}&raw_json=1"
        )
        raw = _fetch_bytes(url, self.timeout)
        if raw is None:
            return []
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return []

        out = []
        for child in (data.get("data") or {}).get("children") or []:
            post = child.get("data") or {}
            title = (post.get("title") or "").strip()
            if not title or post.get("stickied"):
                continue
            when = post.get("created_utc")
            if when and float(when) < cutoff:
                continue
            out.append(
                {
                    "headline": title,
                    # The flair is often the ticker when the title isn't.
                    "summary": (post.get("link_flair_text") or "").strip(),
                    "source": f"r/{sub}",
                    "url": "https://www.reddit.com" + (post.get("permalink") or ""),
                    "datetime": _iso_day(when),
                    "score": post.get("score"),
                    "_ts": when or 0,
                }
            )
        return out


class StockTwitsChatterSource(_ChatterSource):
    """Trending tickers and messages from StockTwits — no key.

    The stand-in for Reddit when Reddit won't answer, and the same signal:
    individual investors posting about specific stocks. Better in one respect —
    posters tag the tickers themselves, so this source hands us ``symbols``
    directly and skips the extraction guesswork.

      GET https://api.stocktwits.com/api/2/trending/symbols.json
      GET https://api.stocktwits.com/api/2/streams/trending.json
    """

    lane = "reddit"
    label = "StockTwits"

    TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"
    STREAM_URL = "https://api.stocktwits.com/api/2/streams/trending.json"

    def _collect(self) -> list:
        out = self._trending() + self._stream()
        return out[:_MAX_HEADLINES]

    def _trending(self) -> list:
        """The trending-symbols board itself, as one synthetic item per name.

        StockTwits has already done the counting, so this arrives ranked. It is
        turned into headline-shaped items with an explicit ``symbols`` list, and
        given a weight by position: the top of a trending board is a much
        stronger statement than the bottom of it.
        """
        data = self._json(self.TRENDING_URL)
        symbols = (data or {}).get("symbols") or []
        out = []
        for rank, entry in enumerate(symbols):
            symbol = (entry.get("symbol") or "").upper()
            if not symbol or _is_not_equity(symbol):
                continue
            title = entry.get("title") or symbol
            out.append(
                {
                    "headline": f"{symbol} — {title} is trending on StockTwits",
                    "summary": "",
                    "source": "StockTwits trending",
                    "url": f"https://stocktwits.com/symbol/{symbol}",
                    "datetime": _iso_day(time.time()),
                    "symbols": [symbol],
                    # Rank 0 counts triple, tailing off down the board.
                    "weight": max(1.0, 3.0 - rank * 0.1),
                    # This is a board position, not something a person wrote.
                    # It scores like chatter but reads poorly as evidence, so
                    # ``_pick_evidence`` shows real posts ahead of it.
                    "synthetic": True,
                }
            )
        return out

    def _stream(self) -> list:
        """Individual trending messages — the actual posts behind the board."""
        data = self._json(self.STREAM_URL)
        messages = (data or {}).get("messages") or []
        out = []
        for message in messages:
            body = (message.get("body") or "").strip()
            if not body:
                continue
            symbols = [
                (s.get("symbol") or "").upper()
                for s in message.get("symbols") or []
                if s.get("symbol") and not _is_not_equity(s["symbol"])
            ]
            if not symbols:
                continue
            user = (message.get("user") or {}).get("username") or "StockTwits"
            out.append(
                {
                    "headline": body[:180],
                    "summary": "",
                    "source": f"StockTwits · @{user}",
                    "url": f"https://stocktwits.com/message/{message.get('id')}",
                    "datetime": (message.get("created_at") or "")[:10] or None,
                    "symbols": symbols,
                }
            )
        return out

    def _json(self, url: str):
        raw = _fetch_bytes(url, self.timeout)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None


def _is_not_equity(symbol: str) -> bool:
    """StockTwits tags crypto as ``BTC.X`` and futures as ``^GSPC``."""
    return "." in symbol or symbol.startswith("^")


class RetailChatterSource(_ChatterSource):
    """The retail lane: Reddit if it answers, StockTwits if it doesn't.

    Same "first non-empty wins" idea as ``CompositeNewsProvider``, for the same
    reason — an unreachable source should cost you that source, not the lane.
    ``label`` reports whichever one actually spoke, so the UI can say "Reddit"
    or "StockTwits" honestly rather than claiming Reddit either way.
    """

    lane = "reddit"

    def __init__(self, sources=None, **kwargs):
        super().__init__(**kwargs)
        self.sources = list(sources) if sources is not None else [
            RedditChatterSource(**kwargs),
            StockTwitsChatterSource(**kwargs),
        ]
        # Before the lane has run, the honest label is the chain, not a claim
        # about which link will answer — the boot log prints this, and saying
        # "Reddit" there when Reddit is blocked is exactly the silent failure
        # the lane labels exist to prevent.
        self.label = self._chain()

    def _chain(self) -> str:
        return " → ".join(s.label for s in self.sources) or "Reddit"

    def _collect(self) -> list:
        for source in self.sources:
            if not source.available():
                continue
            items = source.collect()
            if items:
                self.label = source.label
                return items
        self.label = self._chain()
        return []


class NewsChatterSource(_ChatterSource):
    """The general financial press, via Google News RSS — no key.

    The queries are deliberately about *movement* rather than about any
    company: "biggest movers", "surges", "most active". Asking who moved is how
    you find out who is being talked about without naming anyone first, which is
    the whole trick — a query that named companies could only ever return the
    companies you already thought of.

    ``when:`` bounds each query server-side so the feed is fresh before the
    date filter ever runs.
    """

    lane = "news"
    label = "Google News"

    QUERIES = (
        "stock market movers when:2d",
        "stock surges earnings beat when:2d",
        "stock plunges falls sharply when:2d",
        "most active stocks today when:2d",
        "tech stocks rally when:2d",
        "analyst upgrade price target raised when:3d",
    )

    def _collect(self) -> list:
        return self._rss(self.QUERIES)


class WSJChatterSource(_ChatterSource):
    """The Wall Street Journal, via Google News restricted to wsj.com.

    Not the obvious route, and the obvious route does not work: the Dow Jones
    RSS feeds (``feeds.a.dj.com/rss/RSSMarketsMain.xml`` and friends) still
    serve 200 OK with twenty items, but the items are frozen more than a year in
    the past. A board built on them would rank whatever was in the news then and
    look perfectly healthy doing it.

    So the WSJ lane goes through Google News with ``site:wsj.com``, which
    returns current WSJ headlines. The direct feed is still queried alongside
    it: it costs one request, the date cutoff drops its stale items on the
    floor, and if Dow Jones ever unfreezes it the lane picks the feed back up
    with no change here.
    """

    lane = "wsj"
    label = "The Wall Street Journal"

    QUERIES = (
        "site:wsj.com stocks shares when:5d",
        "site:wsj.com earnings company results when:5d",
        "site:wsj.com markets when:5d",
    )
    DIRECT_FEEDS = ("https://feeds.a.dj.com/rss/RSSMarketsMain.xml",)

    def _collect(self) -> list:
        items = self._rss(self.QUERIES)
        cutoff = self._cutoff()
        for url in self.DIRECT_FEEDS:
            for item in _parse_google_rss(_fetch_bytes(url, self.timeout), cutoff):
                item.pop("_ts", None)
                item["source"] = item.get("source") or "WSJ"
                items.append(item)
        return items[:_MAX_HEADLINES]


def _iso_day(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


# --- the board ----------------------------------------------------------

# How many resolution requests one refresh may spend on guessed company names,
# PER LANE. Symbol-shaped candidates are cheap and nearly always real, so they
# aren't capped; name-shaped ones are the speculative half and each costs a
# request. The budget is per lane rather than shared because the lanes produce
# wildly different numbers of guesses — the news queries throw off hundreds and
# the WSJ a few dozen — and a shared pool is spent by the loud lane before the
# quiet one is reached, which silently drops WSJ names like "Berkshire
# Hathaway" that would have resolved perfectly well.
_MAX_NAME_LOOKUPS_PER_LANE = 20

# The denominator a lane's scores are measured against is at least this, even
# when its busiest ticker is quieter than that. Without a floor, normalising
# hands a perfect 1.0 to whoever leads a nearly-empty lane: on a slow WSJ day
# that is a single "WPP Shares Surge" headline, which then outranks a name a
# hundred people are posting about. The floor says a lane has to be *carrying* a
# conversation before it can crown its winner.
_LANE_FLOOR = 6.0

# A ticker seen in more than one room is a stronger signal than one shouted
# loudly in a single room, so each extra lane adds this to the score. Set well
# below 1.0: breadth is a tiebreaker, not a way to outrank a name that a lane is
# genuinely dominated by.
_MULTI_LANE_BONUS = 0.3

# Headlines kept per ticker for the UI to show as evidence. Three is enough to
# see what the story is; more turns a compact panel into a feed reader.
_EVIDENCE_PER_LANE = 3


class TrendingBoard:
    """Ranks what the lanes are talking about, most-discussed first.

    Composes the lane sources with a ``SymbolResolver``. One call to ``top``
    collects every lane concurrently, extracts and resolves candidates, scores
    them, and returns the leaders with the evidence behind each — which lanes
    mentioned it how often, and the actual headlines.
    """

    def __init__(self, sources=None, resolver=None):
        self.sources = list(sources) if sources is not None else [
            RetailChatterSource(),
            NewsChatterSource(),
            WSJChatterSource(),
        ]
        self.resolver = resolver or SymbolResolver()
        self._cache = None  # (expires_at, board)

    # --- public API -----------------------------------------------------

    def available(self) -> bool:
        return any(s.available() for s in self.sources)

    def describe(self) -> str:
        """The lanes, named by whichever source actually answers each."""
        return " · ".join(s.label for s in self.sources) or "none"

    def lanes(self) -> list:
        return [s.lane for s in self.sources]

    def top(self, count: int = 3, exclude=None) -> dict:
        """The most-talked-about tickers, excluding the ones you already know.

        ``exclude`` is the set of symbols to leave out — holdings and wishlist,
        in this app. They are excluded *after* scoring rather than before, so
        the board still reports honestly how far down the list the picks came
        from, and one very loud holding can't quietly promote three quiet names.

        Returns ``{picks, considered, lanes, headline_counts}``.
        """
        board = self._board()
        exclude = {t.upper() for t in (exclude or [])}
        ranked = board["ranked"]
        picks = [entry for entry in ranked if entry["ticker"] not in exclude][:count]
        return {
            "picks": picks,
            # What was on the board before the exclusions, for the UI's "we
            # looked at N names" line.
            "considered": len(ranked),
            "skipped_known": sum(1 for e in ranked if e["ticker"] in exclude),
            "lanes": board["lanes"],
            "generated_at": board["generated_at"],
        }

    # --- internals ------------------------------------------------------

    def _board(self) -> dict:
        cached = self._cache
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            board = self._build()
        except Exception as e:  # chatter is a nice-to-have, never a crash
            print(f"Discover: could not build the trending board: {e}")
            board = {"ranked": [], "lanes": [], "generated_at": _now_iso()}
        self._cache = (time.monotonic() + _TTL_CHATTER, board)
        return board

    def _build(self) -> dict:
        harvest = self._collect_lanes()

        # Per lane, per headline: {candidate: weight}. Kept headline by headline
        # rather than summed up front, because the fold below has to know which
        # candidates came from the same headline to avoid counting it twice.
        hits_by_lane = {
            lane: [(item, _hits_for(item)) for item in items]
            for lane, items in harvest.items()
        }

        resolved = self._resolve(hits_by_lane)
        by_ticker = _fold(hits_by_lane, resolved)

        return {
            "ranked": self._rank(by_ticker),
            "lanes": [
                {
                    "lane": source.lane,
                    "label": source.label,
                    "headlines": len(harvest.get(source.lane, [])),
                }
                for source in self.sources
            ],
            "generated_at": _now_iso(),
        }

    def _collect_lanes(self) -> dict:
        """Every lane, fetched concurrently. {lane: [headline, ...]}."""
        with ThreadPoolExecutor(max_workers=max(1, len(self.sources))) as pool:
            batches = list(pool.map(lambda s: s.collect(), self.sources))
        harvest = {}
        for source, items in zip(self.sources, batches):
            harvest.setdefault(source.lane, []).extend(items)
        return harvest

    def _resolve(self, hits_by_lane: dict) -> dict:
        """Resolve every candidate the lanes produced. {candidate: {ticker, name}}.

        Symbol-shaped candidates all go through; name-shaped ones are ranked by
        weight *within their lane* and capped there, because each is a request
        and the long tail is almost entirely "Higher Costs" and "Pricier Steaks".
        """
        symbols, names = set(), set()
        for entries in hits_by_lane.values():
            totals = {}
            for _, hits in entries:
                for candidate, weight in hits.items():
                    totals[candidate] = totals.get(candidate, 0.0) + weight
            symbols.update(c for c in totals if not c.startswith("~"))
            lane_names = sorted(
                (c for c in totals if c.startswith("~")),
                key=lambda c: totals[c],
                reverse=True,
            )
            names.update(lane_names[:_MAX_NAME_LOOKUPS_PER_LANE])
        return self.resolver.resolve_many(sorted(symbols) + sorted(names))

    @staticmethod
    def _rank(by_ticker: dict) -> list:
        """Score and sort. See the module docstring for why lanes normalise."""
        # The busiest ticker in each lane, which is what that lane's scores are
        # measured against.
        lane_max = {}
        for entry in by_ticker.values():
            for lane, score in entry["lane_scores"].items():
                lane_max[lane] = max(lane_max.get(lane, 0.0), score)

        ranked = []
        for entry in by_ticker.values():
            shares = {
                lane: score / max(lane_max.get(lane) or 0.0, _LANE_FLOOR)
                for lane, score in entry["lane_scores"].items()
            }
            present = [lane for lane, share in shares.items() if share > 0]
            score = sum(shares.values()) + _MULTI_LANE_BONUS * max(
                0, len(present) - 1
            )
            ranked.append(
                {
                    "ticker": entry["ticker"],
                    "name": entry["name"],
                    "score": round(score, 3),
                    "lanes": sorted(present),
                    # Raw mention weights, so the UI can say "12 on Reddit"
                    # rather than showing a normalised number nobody can read.
                    "mentions": {
                        lane: round(value, 1)
                        for lane, value in entry["lane_scores"].items()
                    },
                    "headlines": _pick_evidence(entry["headlines"]),
                }
            )
        ranked.sort(key=lambda e: (-e["score"], e["ticker"]))
        return ranked


def _hits_for(item: dict) -> dict:
    """One headline -> {candidate: weight}.

    A source that tags its own symbols has already done this better than a
    regular expression can, so trust the tags and skip extraction entirely.
    """
    if item.get("symbols"):
        weight = item.get("weight") or _W_TAGGED
        return {symbol: weight for symbol in item["symbols"]}
    return _extract(
        " ".join(filter(None, [item.get("headline"), item.get("summary")]))
    )


def _fold(hits_by_lane: dict, resolved: dict) -> dict:
    """Candidates -> tickers, counting each headline once per ticker.

    "$NVDA", "NVDA" and "Nvidia" are three ways of saying one thing, and
    ``_name_variants`` deliberately produces several spellings of the same
    company. Summing the candidates would let a headline that happens to name a
    stock twice outweigh two headlines that each name it once — so a headline
    contributes its single strongest mention of a ticker and nothing more.
    """
    by_ticker = {}
    for lane, entries in hits_by_lane.items():
        for item, hits in entries:
            per_ticker = {}
            for candidate, weight in hits.items():
                match = resolved.get(candidate)
                if not match:
                    continue
                ticker = match["ticker"]
                if weight > per_ticker.get(ticker, (0.0, None))[0]:
                    per_ticker[ticker] = (weight, match["name"])
            for ticker, (weight, name) in per_ticker.items():
                entry = by_ticker.setdefault(
                    ticker,
                    {
                        "ticker": ticker,
                        "name": name,
                        "lane_scores": {},
                        "headlines": {},
                    },
                )
                entry["lane_scores"][lane] = entry["lane_scores"].get(lane, 0.0) + weight
                entry["headlines"].setdefault(lane, []).append(item)
    return by_ticker


def _pick_evidence(headlines_by_lane: dict) -> list:
    """A few real headlines per lane, de-duplicated, newest lane order kept.

    Evidence, not a feed: the point is to let a reader check that the ticker is
    on this board for a reason, in the space a narrow column has.
    """
    seen, out = set(), []
    for lane, items in headlines_by_lane.items():
        kept = 0
        # Something a person actually wrote beats a synthesised "X is trending"
        # line, which tells the reader nothing they can't see from the score.
        items = sorted(items, key=lambda i: bool(i.get("synthetic")))
        for item in items:
            key = (item.get("headline") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "lane": lane,
                    "headline": item.get("headline"),
                    "source": item.get("source"),
                    "url": item.get("url"),
                    "datetime": item.get("datetime"),
                }
            )
            kept += 1
            if kept >= _EVIDENCE_PER_LANE:
                break
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
