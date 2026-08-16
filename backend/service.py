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
  - available to trade: the cash on hand to buy with — the one figure the app
    cannot derive, so it is entered by hand and may be left vacant
  - sales log: every sale is recorded so realized gains can be summed over
    time windows (1d / 1w / 1m / ytd / 1y)
  - market: enrich holdings/wishlist with live prices and earnings dates, and
    compute real unrealized gains (today + total) for the dashboard
  - summary: total holdings worth + realized gains + real unrealized gains, for
    the dashboard at the top of the UI
"""

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
    def __init__(self, storage: StorageBackend, sales: "SalesService" = None,
                 available: "AvailableCashService" = None):
        self.storage = storage
        # Optional sales log. When present, every sale records its realized
        # gain/loss so the dashboard can sum realized gains over time.
        self.sales = sales
        # Optional "available to trade" balance. When present, buying draws the
        # cost of the buy out of it — money spent is money no longer available.
        self.available = available

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

        # Buying spends money, so the "available to trade" balance follows it
        # down by what this buy actually cost (the shares bought at the price
        # paid — not the recomputed average, which is history). Only the buy
        # side moves it: the proceeds of a sale land in a brokerage account the
        # app cannot see, and crediting them automatically would turn a
        # note-to-self into a ledger that claims to track the real account.
        if self.available is not None:
            self.available.spend(shares * price)
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


class AvailableCashService:
    """How much money is on hand to buy stocks with — or nothing entered at all.

    This is the one figure in the app that cannot be derived. Holdings, gains
    and realized returns all fall out of what has been recorded; the cash
    sitting in a brokerage account waiting to be spent is known only because
    the investor said so. So it has three states rather than two — an amount,
    zero, and **vacant** (never entered, or removed) — and vacant is the
    default. Nothing here guesses.

    The distinction between zero and vacant is the point of the class, and it
    is load-bearing downstream:

      vacant  "I haven't told you." The AI Actions plan falls back to its
              stand-in balance and says so, and the agents are told nothing
              about a budget — exactly as before this existed.
      0       "I have nothing to invest right now." A real instruction, and a
              different answer from the plan: no buy can be sized.
      n > 0   Real money. The plan is sized against it instead of the stand-in,
              and every agent is told what it has to work with.

    Stored as ``{"amount": n}`` in its own per-portfolio file — a trading
    balance belongs to a portfolio the same way its holdings do. Removing the
    figure deletes the key rather than writing a 0, so the two states can never
    be confused on disk.
    """

    KEY = "amount"

    def __init__(self, storage: StorageBackend):
        self.storage = storage

    # --- reads ----------------------------------------------------------

    def get(self):
        """The available balance, or None when the section is vacant.

        Never raises and never propagates a bad file: this figure is read on
        every dashboard load and by every AI refresh, and an unreadable one
        should cost the feature, not the page. A missing, malformed or negative
        stored value all read as vacant — "we don't know" is the honest answer
        for each of them.
        """
        try:
            raw = (self.storage.load() or {}).get(self.KEY)
        except Exception:
            return None
        if raw is None or isinstance(raw, bool):
            return None
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            return None
        return round(amount, 2) if amount >= 0 else None

    def state(self) -> dict:
        """The shape every caller reads: the amount, and whether it is set."""
        amount = self.get()
        return {"amount": amount, "vacant": amount is None}

    # --- writes ---------------------------------------------------------

    def set(self, amount) -> dict:
        """Record how much is available to trade. Returns the new state.

        Zero is allowed — see the class note. Negative is not: an overdrawn
        brokerage account is not a thing this app models, and silently flooring
        it would hide a typo.
        """
        try:
            value = float(amount)
        except (TypeError, ValueError):
            raise ValidationError("Available to trade must be a number.")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValidationError("Available to trade must be a number.")
        if value < 0:
            raise ValidationError("Available to trade can't be negative.")
        return self._write(value)

    def clear(self) -> dict:
        """Remove the figure entirely — back to vacant. Returns the new state.

        Deletes the key rather than storing 0, so "I removed it" and "I have
        nothing" stay distinguishable on the next read.
        """
        data = self._load()
        data.pop(self.KEY, None)
        self.storage.save(data)
        return self.state()

    def spend(self, amount) -> dict:
        """Draw the cost of a buy out of the balance. A no-op while vacant.

        Floors at zero rather than going negative. Running the balance out
        means "there is nothing left to deploy", which the plan can act on; a
        negative number would assert a debt the app has no way to know about.
        Buying more than you said you had isn't an error either — this figure
        is a note to self, not a ledger with the standing to veto a trade — so
        it is recorded as an empty balance rather than a rejection.
        """
        current = self.get()
        if current is None:
            return self.state()  # vacant stays vacant; a buy doesn't create it
        try:
            cost = float(amount)
        except (TypeError, ValueError):
            return self.state()
        return self._write(max(0.0, current - cost))

    # --- internals ------------------------------------------------------

    def _load(self) -> dict:
        try:
            return self.storage.load() or {}
        except Exception:
            return {}

    def _write(self, value: float) -> dict:
        data = self._load()
        data[self.KEY] = round(float(value), 2)
        self.storage.save(data)
        return self.state()


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


# The views the unrealized-gains section toggles between (in display order). The
# 1D/1W/1M/YTD/1Y windows show the gain accrued over that period (current price
# vs. the price at the window's start); "total" is measured against average cost.
# 1D is measured against the previous close (today's move).
UNREALIZED_VIEWS = INTERVALS + [("total", "Total")]

# The windowed unrealized views that use daily history (everything but 1D, which
# uses intraday data, and "total", which is measured against cost basis).
_UNREALIZED_WINDOW_KEYS = ["1w", "1m", "ytd", "1y"]


def _gain(price, base, shares):
    """A signed dollar change and its percentage: (price - base) * shares, and
    (price - base) / base. Returns None if either input is missing/zero."""
    if price is None or not base:
        return None
    return {
        "value": round((price - base) * shares, 2),
        "pct": round((price - base) / base * 100, 2),
    }


def _combine_series(series_by_ticker, shares_by_ticker, baseline_by_ticker):
    """Sum per-ticker price series into one portfolio gain-over-time series.

    Each ticker contributes shares * (price_at_t - baseline). Tickers are
    sampled onto the union of all their timestamps, forward-filling each
    ticker's last known price (its baseline before its first point), so the
    combined line stays continuous even when tickers report on different grids.

    Returns [(ts_ms, gain), ...] sorted by time.
    """
    all_ts = sorted({ts for s in series_by_ticker.values() for ts, _ in s})
    if not all_ts:
        return []

    idx = {t: 0 for t in series_by_ticker}
    last = {t: None for t in series_by_ticker}
    combined = []
    for ts in all_ts:
        total = 0.0
        for t, series in series_by_ticker.items():
            while idx[t] < len(series) and series[idx[t]][0] <= ts:
                last[t] = series[idx[t]][1]
                idx[t] += 1
            base = baseline_by_ticker.get(t)
            if base is None:
                continue
            price = last[t] if last[t] is not None else base
            total += shares_by_ticker[t] * (price - base)
        combined.append((ts, round(total, 2)))
    return combined


class MarketService:
    """Live-market layer: enriches holdings and wishlist with real prices and
    earnings dates, and computes real unrealized gains for the dashboard.

    Composes the portfolio (for what's owned and its cost basis) with a market
    data provider (for live prices, history, and earnings). Everything degrades
    gracefully: when a quote can't be fetched the enriched fields are None and
    callers render "—".
    """

    def __init__(self, provider, portfolio: PortfolioService):
        self.provider = provider
        self.portfolio = portfolio

    def holdings_view(self) -> list:
        """Holdings enriched with the company name, live price, today's and
        total unrealized gain (value + %), market value, and earnings.

        ``earnings`` is the whole entry from the provider (the date, whether it
        has been reported, and the result if it landed this past week);
        ``earnings_date`` repeats just the date, which is all the AI agents
        take.
        """
        positions = self.portfolio.list_stocks()
        tickers = [p["ticker"] for p in positions]
        quotes = self.provider.get_quotes(tickers)
        earnings = self.provider.get_earnings_infos(tickers)

        rows = []
        for p in positions:
            ticker, shares, avg = p["ticker"], p["shares"], p["avg_price"]
            quote = quotes.get(ticker)
            entry = earnings.get(ticker)
            row = {
                "ticker": ticker,
                "name": None,
                "shares": shares,
                "avg_price": avg,
                "cost_basis": round(shares * avg, 2),
                "earnings": entry,
                "earnings_date": entry["date"] if entry else None,
                "price": None,
                "previous_close": None,
                "market_value": None,
                "today": None,
                "total": None,
                "quote_ok": False,
            }
            if quote and quote.get("price") is not None:
                price = quote["price"]
                prev = quote.get("previous_close")
                row.update(
                    name=quote.get("name"),
                    price=round(price, 4),
                    previous_close=round(prev, 4) if prev else None,
                    market_value=round(price * shares, 2),
                    today=_gain(price, prev, shares),
                    total=_gain(price, avg, shares),
                    quote_ok=True,
                )
            rows.append(row)
        return rows

    def wishlist_view(self, tickers) -> list:
        """Wishlist tickers enriched with the company name, today's open, the
        live price, the change vs. the open (value + %), and earnings (same
        shape as ``holdings_view``)."""
        tickers = list(tickers)
        quotes = self.provider.get_quotes(tickers)
        earnings = self.provider.get_earnings_infos(tickers)

        rows = []
        for ticker in tickers:
            quote = quotes.get(ticker)
            entry = earnings.get(ticker)
            row = {
                "ticker": ticker,
                "name": None,
                "open": None,
                "price": None,
                "change": None,
                "change_pct": None,
                "earnings": entry,
                "earnings_date": entry["date"] if entry else None,
                "quote_ok": False,
            }
            if quote and quote.get("price") is not None:
                price = quote["price"]
                day_open = quote.get("open")
                row.update(name=quote.get("name"), price=round(price, 4), quote_ok=True)
                if day_open:
                    row.update(
                        open=round(day_open, 4),
                        change=round(price - day_open, 2),
                        change_pct=round((price - day_open) / day_open * 100, 2),
                    )
            rows.append(row)
        return rows

    def unrealized_summary(self) -> dict:
        """Real unrealized gains for the dashboard, one entry per view:

          1D            : gain vs. each holding's previous close (today's move),
                          with an intraday graph over today's session.
          1W/1M/YTD/1Y  : gain accrued over the window — current price vs. the
                          price at the window's start — with a graph over it.
          total         : gain vs. average cost, graphed over the past year.

        Each view is {"value", "pct", "series": [{"t": iso, "v": float}, ...]}.
        Values come straight from live quotes so they match the per-holding rows;
        the series provide the graph shape. Missing data -> value None.
        """
        positions = self.portfolio.list_stocks()
        tickers = [p["ticker"] for p in positions]
        shares = {p["ticker"]: p["shares"] for p in positions}
        avg = {p["ticker"]: p["avg_price"] for p in positions}
        quotes = self.provider.get_quotes(tickers)
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)

        prev_close = {}
        price = {}
        for t in tickers:
            q = quotes.get(t)
            if q and q.get("price") is not None:
                price[t] = q["price"]
                prev_close[t] = q.get("previous_close")

        # Fetch history once, concurrently, and reuse it across every window.
        intraday = self.provider.get_intraday_many(tickers)
        daily = self.provider.get_daily_many(tickers)
        cutoffs = _interval_cutoffs(now)

        out = {
            "1d": self._today_view(tickers, shares, prev_close, price, intraday, now_ms),
            "total": self._total_view(tickers, shares, avg, price, daily, now_ms),
        }
        for key in _UNREALIZED_WINDOW_KEYS:
            cutoff_ms = int(cutoffs[key].timestamp() * 1000)
            out[key] = self._window_view(tickers, shares, price, daily, cutoff_ms, now_ms)
        return out

    # --- unrealized helpers ---------------------------------------------

    def _today_view(self, tickers, shares, prev_close, price, intraday, now_ms):
        # Headline figures come from live quotes so they match the holdings rows.
        base = sum(
            shares[t] * prev_close[t] for t in tickers if prev_close.get(t)
        )
        value = sum(
            shares[t] * (price[t] - prev_close[t])
            for t in tickers
            if prev_close.get(t) and t in price
        )
        series_by_ticker = {}
        baseline = {}
        for t in tickers:
            prev, series = intraday.get(t, (None, []))
            if prev and series:
                series_by_ticker[t] = series
                baseline[t] = prev
        combined = _combine_series(series_by_ticker, shares, baseline)
        combined = self._append_now(combined, value, now_ms)
        return self._view(value if base else None, value, base, combined)

    def _window_view(self, tickers, shares, price, daily, cutoff_ms, now_ms):
        # Baseline per holding is the first close on/after the window's start;
        # the gain is the current price measured against that.
        series_by_ticker = {}
        baseline = {}
        for t in tickers:
            points = [(ts, c) for ts, c in daily.get(t, []) if ts >= cutoff_ms]
            if points:
                series_by_ticker[t] = points
                baseline[t] = points[0][1]
        base = sum(shares[t] * baseline[t] for t in baseline)
        value = sum(
            shares[t] * (price[t] - baseline[t])
            for t in baseline
            if t in price
        )
        combined = _combine_series(series_by_ticker, shares, baseline)
        combined = self._append_now(combined, value, now_ms)
        return self._view(value if baseline else None, value, base, combined)

    def _total_view(self, tickers, shares, avg, price, daily, now_ms):
        base = sum(shares[t] * avg[t] for t in tickers)  # cost basis
        value = sum(
            shares[t] * (price[t] - avg[t]) for t in tickers if t in price
        )
        series_by_ticker = {t: daily[t] for t in tickers if daily.get(t)}
        combined = _combine_series(series_by_ticker, shares, avg)
        combined = self._append_now(combined, value, now_ms)
        return self._view(value if series_by_ticker or price else None,
                          value, base, combined)

    @staticmethod
    def _append_now(combined, value, now_ms):
        """End the graph at the live headline value so the line agrees with the
        number shown above it."""
        if not combined:
            return combined
        if combined[-1][0] < now_ms:
            combined = combined + [(now_ms, round(value, 2))]
        else:
            combined = combined[:-1] + [(combined[-1][0], round(value, 2))]
        return combined

    @staticmethod
    def _view(headline, value, base, combined):
        series = [
            {"t": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
             "v": v}
            for ts, v in combined
        ]
        if headline is None:
            return {"value": None, "pct": None, "series": series}
        pct = round(value / base * 100, 2) if base else None
        return {"value": round(value, 2), "pct": pct, "series": series}


class SummaryService:
    """Composes the dashboard summary from the other services.

    Pulls total holdings worth (from the portfolio), realized gains by interval
    (from the sales log), real unrealized gains + graph series (today/total)
    from the market service, and the available-to-trade balance.

    The balance rides along here rather than getting its own read because it is
    shown in the same block as total worth, and the two want to change together:
    a buy moves both, and one request keeps them from disagreeing on screen for
    a frame.
    """

    def __init__(
        self,
        portfolio: PortfolioService,
        sales: SalesService,
        market: "MarketService" = None,
        available: AvailableCashService = None,
    ):
        self.portfolio = portfolio
        self.sales = sales
        self.market = market
        self.available = available

    def summary(self) -> dict:
        unrealized = self.market.unrealized_summary() if self.market else {}
        return {
            "total_worth": self.portfolio.total_cost_basis(),
            # {"amount": n|None, "vacant": bool}. Vacant when never entered or
            # removed — the UI shows the section empty rather than a zero.
            "available": (
                self.available.state()
                if self.available
                else {"amount": None, "vacant": True}
            ),
            "realized": self.sales.realized_gains_by_interval(),
            "sales": self.sales.list_sales(),  # sell history, newest first
            "unrealized": unrealized,
            "intervals": [{"key": k, "label": lbl} for k, lbl in INTERVALS],
            "unrealized_views": [
                {"key": k, "label": lbl} for k, lbl in UNREALIZED_VIEWS
            ],
        }
