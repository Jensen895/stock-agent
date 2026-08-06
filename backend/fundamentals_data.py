"""Fundamentals provider — what the business is actually doing.

This module exists to close a specific gap. The advisor's two AI models used to
see only price, momentum, cost basis and headlines, while the analyst research
in ``analyst_data.py`` comes from desks that have read the filings. A model
asked to weigh a Strong Buy rating without knowing the stock trades at 160x
trailing earnings is not really weighing anything. So the models get the
numbers too: valuation, margins, returns, growth, leverage, cash generation.

Feeding these turned the reasoning concrete. Across a real 23-holding portfolio
the share of calls citing a specific figure went from 9/23 to 23/23 — from
"momentum turned negative, take profits" to "trailing P/E of 160.7x and
EV/EBITDA of 105.2x leave no room for execution missteps".

What crosses into the prompt is decided by an **allowlist** (``_FIELDS``
below), not by filtering out what we don't want. Yahoo's ``quoteSummary``
returns well over a hundred fields per module, most of them empty, fund-only,
or redundant; naming the thirty-odd that carry signal keeps the prompt focused
and its token cost predictable. A denylist would quietly grow every time Yahoo
added a field.

Note on ``surprisePercent``: it is measured against analyst estimates rather
than being pure company data. It is included because it is a historical
*outcome* — did this company beat what was expected of it, four quarters
running — which is exactly the kind of track record a reader should have. The
raw consensus estimate itself is dropped as noise; the beat/miss is the signal.

Source: the same Yahoo ``quoteSummary`` endpoint the rest of the app uses,
reached through ``MarketDataProvider.fetch_quote_summary`` so there is one
cookie/crumb session for all of Yahoo. Fundamentals move on filings, so results
are cached for six hours. Any failure yields None and the models simply reason
without them.
"""

from concurrent.futures import ThreadPoolExecutor

# Fundamentals change when a company reports. Cache accordingly.
_TTL_FUNDAMENTALS = 21600  # seconds (6h)

# Quarters of earnings-surprise history to keep.
_MAX_QUARTERS = 4

_MODULES = [
    "summaryDetail",
    "defaultKeyStatistics",
    "financialData",
    "earningsHistory",
]

# THE ALLOWLIST. Nothing reaches a prompt unless it is named here.
#
#   output_key: (yahoo_module, yahoo_field, scale)
#
# ``scale`` of 100 converts Yahoo's fractions (0.53) to percentages (53.0);
# None means pass the number through untouched.
_FIELDS = {
    # --- valuation ------------------------------------------------------
    # Forward P/E, forward EPS and PEG are built from consensus forecasts, so
    # they carry a trace of the street's view. That used to be a reason to
    # exclude them, back when the models were kept blind to analyst opinion;
    # now that they read the research directly, withholding the two most-used
    # forward multiples would be purity for its own sake.
    "market_cap": ("summaryDetail", "marketCap", None),
    "trailing_pe": ("summaryDetail", "trailingPE", None),
    "forward_pe": ("summaryDetail", "forwardPE", None),
    "peg_ratio": ("defaultKeyStatistics", "pegRatio", None),
    "trailing_eps": ("defaultKeyStatistics", "trailingEps", None),
    "forward_eps": ("defaultKeyStatistics", "forwardEps", None),
    "price_to_sales": ("summaryDetail", "priceToSalesTrailing12Months", None),
    "price_to_book": ("defaultKeyStatistics", "priceToBook", None),
    "ev_to_ebitda": ("defaultKeyStatistics", "enterpriseToEbitda", None),
    "ev_to_revenue": ("defaultKeyStatistics", "enterpriseToRevenue", None),
    # --- profitability --------------------------------------------------
    "gross_margin_pct": ("financialData", "grossMargins", 100),
    "operating_margin_pct": ("financialData", "operatingMargins", 100),
    "profit_margin_pct": ("financialData", "profitMargins", 100),
    "return_on_equity_pct": ("financialData", "returnOnEquity", 100),
    "return_on_assets_pct": ("financialData", "returnOnAssets", 100),
    # --- growth (trailing year-over-year) -------------------------------
    "revenue_growth_pct": ("financialData", "revenueGrowth", 100),
    "earnings_growth_pct": ("financialData", "earningsGrowth", 100),
    "earnings_quarterly_growth_pct": (
        "defaultKeyStatistics", "earningsQuarterlyGrowth", 100,
    ),
    # --- cash, debt, scale ----------------------------------------------
    "total_revenue": ("financialData", "totalRevenue", None),
    "ebitda": ("financialData", "ebitda", None),
    "total_cash": ("financialData", "totalCash", None),
    "total_debt": ("financialData", "totalDebt", None),
    "debt_to_equity": ("financialData", "debtToEquity", None),
    "current_ratio": ("financialData", "currentRatio", None),
    "free_cash_flow": ("financialData", "freeCashflow", None),
    "operating_cash_flow": ("financialData", "operatingCashflow", None),
    # --- price context and risk -----------------------------------------
    "beta": ("summaryDetail", "beta", None),
    "week52_high": ("summaryDetail", "fiftyTwoWeekHigh", None),
    "week52_low": ("summaryDetail", "fiftyTwoWeekLow", None),
    "fifty_day_average": ("summaryDetail", "fiftyDayAverage", None),
    "two_hundred_day_average": ("summaryDetail", "twoHundredDayAverage", None),
    "change_52w_pct": ("defaultKeyStatistics", "52WeekChange", 100),
    "dividend_yield_pct": ("summaryDetail", "dividendYield", None),
    "short_pct_of_float": ("defaultKeyStatistics", "shortPercentOfFloat", 100),
    "held_pct_institutions": (
        "defaultKeyStatistics", "heldPercentInstitutions", 100,
    ),
}

def _raw(value, scale=None):
    """Unwrap a Yahoo number ({"raw": 1.23, "fmt": "1.23"}), optionally scaled."""
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if scale:
        number *= scale
    # Long decimals add tokens and no information.
    return round(number, 4)


class YahooFundamentalsProvider:
    """Company fundamentals for a ticker, from Yahoo Finance.

    Composes a ``MarketDataProvider`` for HTTP — it already holds the cookie
    session and crumb ``quoteSummary`` requires. Any object exposing
    ``fetch_quote_summary(ticker, modules, ttl)`` works.
    """

    def __init__(self, session, max_workers: int = 8):
        self.session = session
        self.max_workers = max_workers

    # --- public API -----------------------------------------------------

    def available(self) -> bool:
        """Keyless, so always usable — outages surface as missing figures."""
        return self.session is not None

    def describe(self) -> str:
        """Human-readable source name, for the boot log and the UI."""
        return "Yahoo Finance fundamentals"

    def get_fundamentals(self, ticker: str):
        """Return the allowlisted fundamentals for one ticker, or None.

        Only fields named in ``_FIELDS`` are ever returned, plus the actual-EPS
        history. Keys with no value are omitted rather than sent as nulls, so a
        thinly covered symbol costs a few tokens instead of a wall of "None".
        """
        if not self.available():
            return None
        try:
            result = self.session.fetch_quote_summary(
                ticker, _MODULES, ttl=_TTL_FUNDAMENTALS
            )
        except Exception:  # never break the advisor over missing fundamentals
            return None
        if not result:
            return None
        return self._parse(result)

    def get_fundamentals_many(self, tickers) -> dict:
        """Fetch fundamentals for several tickers concurrently.
        Returns {ticker: fundamentals-or-None}."""
        tickers = list(tickers)
        if not tickers or not self.available():
            return {t: None for t in tickers}
        workers = min(self.max_workers, len(tickers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(zip(tickers, pool.map(self.get_fundamentals, tickers)))

    # --- parsing --------------------------------------------------------

    def _parse(self, result: dict):
        out = {}
        for key, (module, field, scale) in _FIELDS.items():
            value = _raw((result.get(module) or {}).get(field), scale)
            if value is not None:
                out[key] = value

        surprises = self._surprises(result)
        if surprises:
            out["earnings_surprises"] = surprises

        return out or None

    @staticmethod
    def _surprises(result: dict) -> list:
        """Recent quarters as {quarter, eps_actual, surprise_pct} — the beat /
        miss record. The consensus estimate itself is deliberately dropped."""
        history = (result.get("earningsHistory") or {}).get("history") or []
        out = []
        for item in history[-_MAX_QUARTERS:]:
            if not isinstance(item, dict):
                continue
            quarter = item.get("quarter")
            when = quarter.get("fmt") if isinstance(quarter, dict) else None
            actual = _raw(item.get("epsActual"))
            surprise = _raw(item.get("surprisePercent"), 100)
            if actual is None and surprise is None:
                continue
            entry = {}
            if when:
                entry["quarter"] = when
            if actual is not None:
                entry["eps_actual"] = actual
            if surprise is not None:
                entry["surprise_pct"] = surprise
            out.append(entry)
        return out
