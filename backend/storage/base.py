"""Storage layer — abstract interface.

Any storage backend (JSON file, SQLite, cloud DB, ...) implements this
interface. The rest of the app depends only on these methods, never on a
concrete backend, so swapping storage means writing one new class and
changing a single line in run.py.

A "position" is a dict of the shape:
    {"ticker": "AAPL", "shares": 10.0, "avg_price": 150.0}
The portfolio is a dict keyed by ticker: {ticker: position}.
"""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    @abstractmethod
    def load(self) -> dict:
        """Return the full portfolio as {ticker: position}. Empty dict if none."""
        raise NotImplementedError

    @abstractmethod
    def save(self, portfolio: dict) -> None:
        """Persist the full portfolio ({ticker: position})."""
        raise NotImplementedError
