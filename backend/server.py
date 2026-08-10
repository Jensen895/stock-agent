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
  GET    /api/ai            -> latest AI suggestions           (read only)
                               summary + per-stock detail, low & high risk,
                               each score broken down by the five agents
  POST   /api/ai/refresh    -> trigger a background regeneration of suggestions
  GET    /api/discover      -> three trending stocks you neither hold nor watch
                               (read only) each with why it's being talked
                               about, what the company is, and the same
                               five-agent confidence score as everything else
  POST   /api/discover/refresh -> trigger a background regeneration of picks
  POST   /api/ai/weights    -> set how much each AI agent counts     (write)
                               body: {"weights": {"<agent key>": number, ...}}
                               Re-blends the cached scores immediately and
                               returns the same payload as GET /api/ai — no
                               model is called, so this is instant and free.
  GET    /api/portfolios    -> list portfolios + which is active (read only)
  POST   /api/portfolios    -> create a new portfolio (and switch to it) (write)
                               body: {"name"}
  POST   /api/portfolios/switch -> switch the active portfolio     (write)
                               body: {"id"}
  POST   /api/portfolios/rename -> rename a portfolio              (write)
                               body: {"id","name"}
  POST   /api/portfolios/risk   -> remember the AI risk toggle     (write)
                               body: {"risk"}   (for the active portfolio)
  DELETE /api/portfolios    -> delete (archive) a portfolio        (write)
                               body: {"id"}

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
from backend.workspace import WorkspaceError

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
    advisor=None,
    workspace=None,
    discover=None,
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
            elif self.path == "/api/ai":
                # Latest AI suggestions (cached); empty state if unconfigured.
                if advisor is None:
                    self._send_json(200, {"configured": False, "risk_profiles": None})
                else:
                    self._send_json(200, advisor.get())
            elif self.path == "/api/discover":
                # Trending stocks you don't own or watch; empty state if off.
                if discover is None:
                    self._send_json(
                        200,
                        {"configured": False, "model_configured": False,
                         "picks": None},
                    )
                else:
                    self._send_json(200, discover.get())
            elif self.path == "/api/portfolios":
                # Every portfolio + which one is active (the app's "memory").
                if workspace is None:
                    self._send_json(200, {"portfolios": [], "active": None})
                else:
                    self._send_json(200, workspace.state())
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
            elif self.path == "/api/ai/refresh":
                # Fire-and-forget: start a regeneration, report whether it began.
                started = advisor.request_refresh() if advisor else False
                self._send_json(200, {"started": started})
            elif self.path == "/api/ai/weights":
                self._handle_ai_weights()
            elif self.path == "/api/discover/refresh":
                started = discover.request_refresh() if discover else False
                self._send_json(200, {"started": started})
            elif self.path == "/api/portfolios":
                self._handle_portfolio_create()
            elif self.path == "/api/portfolios/switch":
                self._handle_portfolio_switch()
            elif self.path == "/api/portfolios/rename":
                self._handle_portfolio_rename()
            elif self.path == "/api/portfolios/risk":
                self._handle_portfolio_risk()
            else:
                self._send_json(404, {"error": "Not found"})

        def do_DELETE(self):
            if self.path == "/api/stocks":
                self._handle_delete()
            elif self.path == "/api/wishlist":
                self._handle_wishlist_remove()
            elif self.path == "/api/portfolios":
                self._handle_portfolio_delete()
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

        def _handle_ai_weights(self):
            # Reweighting is arithmetic over scores we already have, so this
            # returns the fully re-blended panel rather than kicking off a job
            # and making the UI poll for it.
            def action(body):
                if advisor is None:
                    return {"configured": False, "risk_profiles": None}
                advisor.set_weights(body.get("weights"))
                return advisor.get()
            self._run(action)

        # --- portfolio (workspace) handlers -----------------------------
        #
        # Each portfolio is a separate workspace; switching repoints all the
        # storage above at once. The AI advisor caches its suggestions in
        # memory, so after any change to the *active* portfolio we tell it to
        # reload from the now-active portfolio's file.

        def _handle_portfolio_create(self):
            def action(body):
                entry = workspace.create(name=body.get("name"))
                self._reload_advisor()  # create() switches to the new portfolio
                return {"portfolio": entry, "state": workspace.state()}
            self._run(action)

        def _handle_portfolio_switch(self):
            def action(body):
                workspace.switch(pid=body.get("id"))
                self._reload_advisor()
                return {"state": workspace.state()}
            self._run(action)

        def _handle_portfolio_rename(self):
            def action(body):
                entry = workspace.rename(pid=body.get("id"), name=body.get("name"))
                return {"portfolio": entry, "state": workspace.state()}
            self._run(action)

        def _handle_portfolio_risk(self):
            # Persist the AI-advisor risk toggle for the active portfolio. No
            # advisor reload needed — both risk profiles are already generated;
            # this only remembers which one to show.
            def action(body):
                risk = workspace.set_risk(risk=body.get("risk"))
                return {"risk": risk, "state": workspace.state()}
            self._run(action)

        def _handle_portfolio_delete(self):
            def action(body):
                workspace.delete(pid=body.get("id"))
                self._reload_advisor()  # delete() may move the active pointer
                return {"state": workspace.state()}
            self._run(action)

        def _reload_advisor(self):
            # Both panels cache the active portfolio's results in memory, so
            # both have to be repointed when the active portfolio moves.
            if advisor is not None:
                advisor.reload()
            if discover is not None:
                discover.reload()

        def _run(self, action):
            """Read the JSON body, run a write action, and send its result.
            Centralizes the shared validation/error handling for all writes."""
            try:
                body = self._read_json_body()
                payload = action(body)
            except (ValidationError, WorkspaceError) as e:
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
    advisor=None,
    workspace=None,
    discover=None,
    host: str = "127.0.0.1",
    port: int = 8000,
):
    handler = make_handler(
        portfolio, wishlist, summary, market, advisor, workspace, discover
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Stock assistant running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()
