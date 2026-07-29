"""Business logic — the reusable core of the stock assistant.

This layer knows nothing about HTTP or the frontend, and nothing about *how*
data is stored (it talks to any StorageBackend). That makes it reusable across
different UIs and different storage backends.

Responsibilities:
  - validate input
  - accumulate shares when a ticker already exists
  - recompute the weighted-average buy price
"""

from backend.storage import StorageBackend


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
