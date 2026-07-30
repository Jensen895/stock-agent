#!/usr/bin/env python3
"""Entry point for the personal stock assistant.

This is the single place where the layers are wired together:

    storage backend  ->  service (business logic)  ->  API server  ->  UI

To use a different storage system, swap the one line below for another
StorageBackend implementation. Nothing else changes.
"""

import os

from backend.market_data import MarketDataProvider
from backend.server import run_server
from backend.service import (
    MarketService,
    PortfolioService,
    SalesService,
    SummaryService,
    WishlistService,
)
from backend.storage import JSONStorage

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
WISHLIST_FILE = os.path.join(DATA_DIR, "wishlist.json")
SALES_FILE = os.path.join(DATA_DIR, "sales.json")


def main():
    # <- swap storage backends here. Holdings, wishlist, and the sales log are
    # separate tables.
    portfolio_storage = JSONStorage(PORTFOLIO_FILE)
    # sales log: every sell is recorded here so realized gains can be summed.
    sales = SalesService(JSONStorage(SALES_FILE))
    portfolio = PortfolioService(portfolio_storage, sales=sales)
    # wishlist also reads holdings so it can reject stocks you already own.
    wishlist = WishlistService(JSONStorage(WISHLIST_FILE), holdings=portfolio_storage)
    # live market data (Yahoo Finance) — powers real prices, unrealized gains,
    # and earnings dates. Swap this provider to change data sources.
    market = MarketService(MarketDataProvider(), portfolio)
    # dashboard summary: total worth + realized + real unrealized gains.
    summary = SummaryService(portfolio, sales, market)
    run_server(portfolio, wishlist, summary, market, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
