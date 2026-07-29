#!/usr/bin/env python3
"""Entry point for the personal stock assistant.

This is the single place where the layers are wired together:

    storage backend  ->  service (business logic)  ->  API server  ->  UI

To use a different storage system, swap the one line below for another
StorageBackend implementation. Nothing else changes.
"""

import os

from backend.server import run_server
from backend.service import PortfolioService
from backend.storage import JSONStorage

DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "portfolio.json")


def main():
    storage = JSONStorage(DATA_FILE)          # <- swap storage backend here
    service = PortfolioService(storage)
    run_server(service, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
