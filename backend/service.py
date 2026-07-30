"""Business logic — the reusable core of the stock assistant.

This layer knows nothing about HTTP or the frontend, and nothing about *how*
data is stored (it talks to any StorageBackend). That makes it reusable across
different UIs and different storage backends.

Responsibilities:
  - validate input
  - buy: accumulate shares when a ticker already exists, recomputing the
    weighted-average buy price
  - sell: reduce shares at a sale price (cost basis per share is unchanged);
    fully selling out removes the position, recording the realized gain/loss
  - delete: drop an entire position outright (for correcting mistakes)
  - wishlist: track tickers you plan to buy but don't yet own (ticker only)
  - sales log: every sale is recorded so realized gains can be summed over
    time windows (1d / 1w / 1m / ytd / 1y)
  - summary: total holdings worth + realized gains + (placeholder) unrealized
    gains, for the dashboard at the top of the UI
"""

import random
from datetime import datetime, timedelta, timezone

from backend.storage import StorageBackend

# Positions are rounded to this many decimals; anything at/under this threshold
# of remaining shares after a sell is treated as "sold out".
_SHARE_EPSILON = 1e-6

# The time windows the dashboard can toggle between, in display order. Each maps
# to a human label used by the UI.
INTERVALS = [
    ("1d", "1D"),
    ("1w", "1W"),
    ("1m", "1M"),
    ("ytd", "YTD"),
    ("1y", "1Y"),
]
INTERVAL_KEYS = [key for key, _ in INTERVALS]


def _interval_cutoffs(now: datetime) -> dict:
    """Map each interval key to the earliest timestamp still inside that window.

    A record counts toward an interval when its timestamp is >= the cutoff.
    """
    return {
        "1d": now - timedelta(days=1),
        "1w": now - timedelta(weeks=1),
        "1m": now - timedelta(days=30),
        "ytd": datetime(now.year, 1, 1, tzinfo=timezone.utc),
        "1y": now - timedelta(days=365),
    }


class ValidationError(ValueError):
    """Raised when user input is invalid (bad ticker / non-positive numbers)."""


class PortfolioService:
    def __init__(self, storage: StorageBackend, sales: "SalesService" = None):
        self.storage = storage
        # Optional sales log. When present, every sale records its realized
        # gain/loss so the dashboard can sum realized gains over time.
        self.sales = sales

    def add_stock(self, ticker: str, shares, price) -> dict:
        """Add a holding. If the ticker exists, accumulate shares and
        recompute the weighted-average price. Returns the updated position."""
        ticker = self._clean_ticker(ticker)
        shares = self._positive_number(shares, "shares")
        price = self._positive_number(price, "average price")

        portfolio = self.storage.load()
        existing = portfolio.get(ticker)

        if existing:
            old_shares = existing["shares"]
            old_avg = existing["avg_price"]
            total_shares = old_shares + shares
            # weighted-average cost basis
            new_avg = (old_shares * old_avg + shares * price) / total_shares
            position = {
                "ticker": ticker,
                "shares": round(total_shares, 6),
                "avg_price": round(new_avg, 4),
            }
        else:
            position = {
                "ticker": ticker,
                "shares": round(shares, 6),
                "avg_price": round(price, 4),
            }

        portfolio[ticker] = position
        self.storage.save(portfolio)
        return position

    def sell_stock(self, ticker: str, shares, price) -> dict:
        """Sell part (or all) of a holding at a given sale price.

        Reduces the share count. The per-share cost basis (avg_price) is
        unchanged by a sale — selling doesn't alter what the remaining shares
        originally cost. Selling the entire position removes it.

        Realized gain/loss = (sale price - average cost) * shares sold. The sale
        is recorded in the sales log (when one is wired in) so it can be summed
        into realized gains over time.

        Returns a result dict describing the sale and what remains:
            {"ticker", "sold_shares", "sale_price", "cost_basis",
             "realized_gain", "proceeds", "remaining", "sold_out"}
        """
        ticker = self._clean_ticker(ticker)
        shares = self._positive_number(shares, "shares")
        price = self._positive_number(price, "sale price")

        portfolio = self.storage.load()
        existing = portfolio.get(ticker)
        if not existing:
            raise ValidationError(f"You don't own any {ticker}.")

        owned = existing["shares"]
        if shares > owned + _SHARE_EPSILON:
            raise ValidationError(
                f"You only own {owned:g} shares of {ticker}; cannot sell {shares:g}."
            )

        # Realized gain is locked in against the average cost of the shares sold.
        avg_price = existing["avg_price"]
        realized_gain = round((price - avg_price) * shares, 2)

        remaining = round(owned - shares, 6)
        sold_out = remaining <= _SHARE_EPSILON

        if sold_out:
            del portfolio[ticker]
            remaining = 0.0
        else:
            existing["shares"] = remaining  # avg_price stays the same
            portfolio[ticker] = existing

        self.storage.save(portfolio)

        result = {
            "ticker": ticker,
            "sold_shares": round(shares, 6),
            "sale_price": round(price, 4),
            "cost_basis": round(avg_price, 4),
            "realized_gain": realized_gain,
            "proceeds": round(shares * price, 2),
            "remaining": remaining,
            "sold_out": sold_out,
        }
        if self.sales is not None:
            self.sales.record(result)
        return result

    def total_cost_basis(self) -> float:
        """Total worth of current holdings: sum of shares * avg_price."""
        portfolio = self.storage.load()
        total = sum(p["shares"] * p["avg_price"] for p in portfolio.values())
        return round(total, 2)

    def delete_stock(self, ticker: str) -> str:
        """Delete an entire holding outright (e.g. to fix a mistyped ticker).

        Unlike selling, this makes no attempt to record a sale — it simply
        removes the position. Returns the deleted ticker.
        """
        ticker = self._clean_ticker(ticker)
        portfolio = self.storage.load()
        if ticker not in portfolio:
            raise ValidationError(f"{ticker} is not in your holdings.")
        del portfolio[ticker]
        self.storage.save(portfolio)
        return ticker

    def list_stocks(self) -> list:
        """Return all positions, sorted by ticker."""
        portfolio = self.storage.load()
        return [portfolio[t] for t in sorted(portfolio)]

    # --- input validation helpers ---------------------------------------

    @staticmethod
    def _clean_ticker(ticker) -> str:
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValidationError("Ticker symbol is required.")
        return ticker.strip().upper()

    @staticmethod
    def _positive_number(value, label) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{label.capitalize()} must be a number.")
        if number <= 0:
            raise ValidationError(f"{label.capitalize()} must be greater than zero.")
        return number


class WishlistService:
    """Stocks you don't own yet but plan to buy.

    Kept deliberately separate from the holdings portfolio: a wishlist entry is
    just a ticker (no shares, no price). This is the table the AI buy/sell
    suggestions will eventually read from.

    Stored as {ticker: {"ticker": ticker}} so it reuses the same dict-shaped
    StorageBackend as the portfolio.

    Given the holdings storage as well, it can guard against wishlisting a stock
    you already own.
    """

    def __init__(self, storage: StorageBackend, holdings: StorageBackend = None):
        self.storage = storage
        self.holdings = holdings

    def add(self, ticker: str) -> dict:
        """Add a ticker to the wishlist. Idempotent — adding an existing
        ticker just returns it. Returns the wishlist entry.

        Rejects tickers you already hold — the wishlist is for stocks you plan
        to buy, not ones already in your portfolio."""
        ticker = PortfolioService._clean_ticker(ticker)
        if self.holdings is not None and ticker in self.holdings.load():
            raise ValidationError(f"You already own {ticker}; it's in your holdings.")
        wishlist = self.storage.load()
        entry = {"ticker": ticker}
        wishlist[ticker] = entry
        self.storage.save(wishlist)
        return entry

    def remove(self, ticker: str) -> str:
        """Remove a ticker from the wishlist. Returns the removed ticker."""
        ticker = PortfolioService._clean_ticker(ticker)
        wishlist = self.storage.load()
        if ticker not in wishlist:
            raise ValidationError(f"{ticker} is not in your wishlist.")
        del wishlist[ticker]
        self.storage.save(wishlist)
        return ticker

    def list_wishlist(self) -> list:
        """Return all wishlist entries, sorted by ticker."""
        wishlist = self.storage.load()
        return [wishlist[t] for t in sorted(wishlist)]


class SalesService:
    """Append-only log of realized sales, used to sum realized gains over time.

    Every completed sale is stored with a UTC timestamp and its realized
    gain/loss. Reuses the same dict-shaped StorageBackend as the rest of the app
    by keeping the log under a single "sales" key: {"sales": [record, ...]}.
    """

    def __init__(self, storage: StorageBackend):
        self.storage = storage

    def record(self, sale: dict) -> dict:
        """Append a sale to the log, stamping it with the current UTC time.

        `sale` is the dict returned by PortfolioService.sell_stock. Returns the
        stored record (a copy with a "timestamp" added).
        """
        entry = {
            "ticker": sale["ticker"],
            "shares": sale["sold_shares"],
            "sale_price": sale["sale_price"],
            "cost_basis": sale["cost_basis"],
            "realized_gain": sale["realized_gain"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        data = self.storage.load()
        log = data.get("sales", [])
        log.append(entry)
        data["sales"] = log
        self.storage.save(data)
        return entry

    def list_sales(self) -> list:
        """Return all recorded sales, newest first."""
        data = self.storage.load()
        return sorted(
            data.get("sales", []), key=lambda s: s["timestamp"], reverse=True
        )

    def realized_gains_by_interval(self, now: datetime = None) -> dict:
        """Sum realized gains within each time window, keyed by interval.

        Returns e.g. {"1d": 0.0, "1w": 12.5, "1m": ..., "ytd": ..., "1y": ...}.
        """
        now = now or datetime.now(timezone.utc)
        cutoffs = _interval_cutoffs(now)
        totals = {key: 0.0 for key in INTERVAL_KEYS}

        for sale in self.storage.load().get("sales", []):
            ts = datetime.fromisoformat(sale["timestamp"])
            for key, cutoff in cutoffs.items():
                if ts >= cutoff:
                    totals[key] += sale["realized_gain"]

        return {key: round(value, 2) for key, value in totals.items()}


# Fixed seeds per interval so the placeholder graph is stable across restarts
# (rather than jumping to new random numbers on every refresh).
_DUMMY_SEEDS = {"1d": 11, "1w": 22, "1m": 33, "ytd": 44, "1y": 55}
# How many points to plot for each interval's line graph.
_DUMMY_POINTS = {"1d": 24, "1w": 14, "1m": 30, "ytd": 24, "1y": 24}


def dummy_unrealized_gains(now: datetime = None) -> dict:
    """PLACEHOLDER unrealized gains + time series for each interval.

    The real thing needs a live price feed (current price vs. average cost),
    which isn't wired up yet. Until then this returns deterministic dummy data
    so the UI — number + line graph — can be built and reviewed.

    Returns {interval: {"value": float, "series": [{"t": iso, "v": float}, ...]}}.
    """
    now = now or datetime.now(timezone.utc)
    cutoffs = _interval_cutoffs(now)
    out = {}

    for key in INTERVAL_KEYS:
        rng = random.Random(_DUMMY_SEEDS[key])
        points = _DUMMY_POINTS[key]
        start = cutoffs[key]
        span = (now - start) / (points - 1)

        value = 0.0
        series = []
        for i in range(points):
            # random walk with a slight bias; sign of the final value varies
            # by seed so some intervals show green and others red.
            value += rng.uniform(-45, 55)
            t = start + span * i
            series.append({"t": t.isoformat(), "v": round(value, 2)})

        out[key] = {"value": series[-1]["v"], "series": series}

    return out


class SummaryService:
    """Composes the dashboard summary from the other services.

    Pulls total holdings worth (from the portfolio), realized gains by interval
    (from the sales log), and placeholder unrealized gains + graph series.
    """

    def __init__(self, portfolio: PortfolioService, sales: SalesService):
        self.portfolio = portfolio
        self.sales = sales

    def summary(self) -> dict:
        return {
            "total_worth": self.portfolio.total_cost_basis(),
            "realized": self.sales.realized_gains_by_interval(),
            "sales": self.sales.list_sales(),  # sell history, newest first
            "unrealized": dummy_unrealized_gains(),
            "intervals": [{"key": k, "label": lbl} for k, lbl in INTERVALS],
        }
