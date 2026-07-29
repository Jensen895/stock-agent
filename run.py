#!/usr/bin/env python3
"""Entry point for the personal stock assistant.

This is the single place where the layers are wired together:

    storage backend  ->  service (business logic)  ->  API server  ->  UI

To use a different storage system, swap the one line below for another
StorageBackend implementation. Nothing else changes.
"""

import os

from backend.server import run_server
from backend.service import PortfolioService, WishlistService
from backend.storage import JSONStorage

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
WISHLIST_FILE = os.path.join(DATA_DIR, "wishlist.json")


def main():
    # <- swap storage backends here. Holdings and wishlist are separate tables.
    portfolio_storage = JSONStorage(PORTFOLIO_FILE)
    portfolio = PortfolioService(portfolio_storage)
    # wishlist also reads holdings so it can reject stocks you already own.
    wishlist = WishlistService(JSONStorage(WISHLIST_FILE), holdings=portfolio_storage)
    run_server(portfolio, wishlist, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
