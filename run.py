#!/usr/bin/env python3
"""Entry point for the personal stock assistant.

    storage backend  ->  service (business logic)  ->  API server  ->  UI

The wiring itself lives in ``backend/wiring.py``, because a second entry point
needs the same object graph: ``competition.py`` builds it and drives it from a
script instead of serving it. This file is what is left once that moved out —
build the graph, start the two daily schedulers, open the port.
"""

from backend.wiring import build, load_dotenv
from backend.server import run_server

load_dotenv()


def main():
    services = build()

    # Refreshes once per trading day, at the opening bell (plus once on boot if
    # nothing is cached). The Refresh button in the UI forces one any time.
    services.advisor.start_scheduler()

    # Discover shares the advisor's daily bell but waits for it to finish first,
    # to keep the burst off a free tier's per-minute quota. Cost: five agents x
    # two risk profiles, so up to ten more model calls per day on top of the
    # advisor's twenty.
    services.discover.start_scheduler()

    run_server(
        services.portfolio, services.wishlist, services.summary, services.market,
        services.advisor, services.manager, services.discover, services.actions,
        services.available, host="127.0.0.1", port=8000,
    )


if __name__ == "__main__":
    main()
