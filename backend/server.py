"""API layer — the ONLY bridge between the UI and the storage system.

Exposes a small REST API over the PortfolioService and WishlistService. Any UI
(this web frontend, a CLI, a mobile app, ...) talks to these endpoints; it never
touches the services or storage directly.

Endpoints:
  GET    /api/stocks        -> list all positions, enriched with live prices,
                               today/total unrealized gains, and earnings dates
  POST   /api/stocks        -> buy / accumulate a position   (write)
                               body: {"ticker","shares","avg_price"}
  POST   /api/stocks/sell   -> sell part/all of a position   (write)
                               body: {"ticker","shares","price"}
  DELETE /api/stocks        -> delete an entire position      (write)
                               body: {"ticker"}
  GET    /api/wishlist      -> list wishlist tickers          (read only)
  POST   /api/wishlist      -> add a ticker to the wishlist   (write)
                               body: {"ticker"}
  DELETE /api/wishlist      -> remove a ticker from wishlist  (write)
                               body: {"ticker"}
  GET    /api/summary       -> dashboard totals               (read only)
                               total worth + realized + unrealized gains

Static files (the web UI) are served from the frontend/ directory.
Implemented with the Python standard library only — no dependencies.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.service import (
    MarketService,
    PortfolioService,
    SummaryService,
    WishlistService,
    ValidationError,
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


def make_handler(
    portfolio: PortfolioService,
    wishlist: WishlistService,
    summary: SummaryService,
    market: MarketService,
):
    """Build a request handler bound to the given service instances."""

    def wishlist_tickers():
        return [entry["ticker"] for entry in wishlist.list_wishlist()]

    class Handler(BaseHTTPRequestHandler):
        # --- routing ----------------------------------------------------

        def do_GET(self):
            if self.path == "/api/stocks":
                # Enriched with live prices, unrealized gains, and earnings dates.
                self._send_json(200, {"stocks": market.holdings_view()})
            elif self.path == "/api/summary":
                self._send_json(200, summary.summary())
            elif self.path == "/api/wishlist":
                # Enriched with live open/price + change and earnings dates.
                self._send_json(200, {"wishlist": market.wishlist_view(wishlist_tickers())})
            elif self.path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif self.path.lstrip("/") in ("style.css", "app.js"):
                self._serve_static(self.path.lstrip("/"))
            else:
                self._send_json(404, {"error": "Not found"})

        def do_POST(self):
            if self.path == "/api/stocks":
                self._handle_buy()
            elif self.path == "/api/stocks/sell":
                self._handle_sell()
            elif self.path == "/api/wishlist":
                self._handle_wishlist_add()
            else:
                self._send_json(404, {"error": "Not found"})

        def do_DELETE(self):
            if self.path == "/api/stocks":
                self._handle_delete()
            elif self.path == "/api/wishlist":
                self._handle_wishlist_remove()
            else:
                self._send_json(404, {"error": "Not found"})

        # --- API handlers -----------------------------------------------

        def _handle_buy(self):
            def action(body):
                return {"stock": portfolio.add_stock(
                    ticker=body.get("ticker"),
                    shares=body.get("shares"),
                    price=body.get("avg_price"),
                )}
            self._run(action)

        def _handle_sell(self):
            def action(body):
                return {"sale": portfolio.sell_stock(
                    ticker=body.get("ticker"),
                    shares=body.get("shares"),
                    price=body.get("price"),
                )}
            self._run(action)

        def _handle_delete(self):
            def action(body):
                return {"deleted": portfolio.delete_stock(ticker=body.get("ticker"))}
            self._run(action)

        def _handle_wishlist_add(self):
            def action(body):
                return {"entry": wishlist.add(ticker=body.get("ticker"))}
            self._run(action)

        def _handle_wishlist_remove(self):
            def action(body):
                return {"removed": wishlist.remove(ticker=body.get("ticker"))}
            self._run(action)

        def _run(self, action):
            """Read the JSON body, run a write action, and send its result.
            Centralizes the shared validation/error handling for all writes."""
            try:
                body = self._read_json_body()
                payload = action(body)
            except ValidationError as e:
                self._send_json(400, {"error": str(e)})
            except (json.JSONDecodeError, TypeError):
                self._send_json(400, {"error": "Invalid request body."})
            else:
                self._send_json(200, payload)

        # --- helpers ----------------------------------------------------

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))

        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, filename: str):
            path = os.path.join(FRONTEND_DIR, filename)
            if not os.path.isfile(path):
                self._send_json(404, {"error": "Not found"})
                return
            with open(path, "rb") as f:
                body = f.read()
            ext = os.path.splitext(filename)[1]
            self.send_response(200)
            self.send_header("Content-Type", _CONTENT_TYPES.get(ext, "text/plain"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            # concise one-line access log
            print(f"  {self.command} {self.path} -> {args[1]}")

    return Handler


def run_server(
    portfolio: PortfolioService,
    wishlist: WishlistService,
    summary: SummaryService,
    market: MarketService,
    host: str = "127.0.0.1",
    port: int = 8000,
):
    handler = make_handler(portfolio, wishlist, summary, market)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Stock assistant running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()
