"""Business logic — the reusable core of the stock assistant.

This layer knows nothing about HTTP or the frontend, and nothing about *how*
data is stored (it talks to any StorageBackend). That makes it reusable across
different UIs and different storage backends.

Responsibilities:
  - validate input
  - buy: accumulate shares when a ticker already exists, recomputing the
    weighted-average buy price
  - sell: reduce shares at a sale price (cost basis per share is unchanged);
    fully selling out removes the position
  - delete: drop an entire position outright (for correcting mistakes)
  - wishlist: track tickers you plan to buy but don't yet own (ticker only)
"""

from backend.storage import StorageBackend

# Positions are rounded to this many decimals; anything at/under this threshold
# of remaining shares after a sell is treated as "sold out".
_SHARE_EPSILON = 1e-6


class ValidationError(ValueError):
    """Raised when user input is invalid (bad ticker / non-positive numbers)."""


class PortfolioService:
    def __init__(self, storage: StorageBackend):
        self.storage = storage

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

        Returns a result dict describing the sale and what remains:
            {"ticker", "sold_shares", "sale_price", "proceeds",
             "remaining", "sold_out"}
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

        remaining = round(owned - shares, 6)
        sold_out = remaining <= _SHARE_EPSILON

        if sold_out:
            del portfolio[ticker]
            remaining = 0.0
        else:
            existing["shares"] = remaining  # avg_price stays the same
            portfolio[ticker] = existing

        self.storage.save(portfolio)
        return {
            "ticker": ticker,
            "sold_shares": round(shares, 6),
            "sale_price": round(price, 4),
            "proceeds": round(shares * price, 2),
            "remaining": remaining,
            "sold_out": sold_out,
        }

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
