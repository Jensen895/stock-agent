"""API layer — the ONLY bridge between the UI and the storage system.

Exposes a small REST API over the PortfolioService. Any UI (this web
frontend, a CLI, a mobile app, ...) talks to these endpoints; it never
touches the service or storage directly.

Endpoints:
  GET  /api/stocks   -> list all positions          (read only)
  POST /api/stocks   -> add/accumulate a position    (write only)
                        body: {"ticker","shares","avg_price"}

Static files (the web UI) are served from the frontend/ directory.
Implemented with the Python standard library only — no dependencies.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.service import PortfolioService, ValidationError

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


def make_handler(service: PortfolioService):
    """Build a request handler bound to a given service instance."""

    class Handler(BaseHTTPRequestHandler):
        # --- routing ----------------------------------------------------

        def do_GET(self):
            if self.path == "/api/stocks":
                self._handle_list()
            elif self.path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif self.path.lstrip("/") in ("style.css", "app.js"):
                self._serve_static(self.path.lstrip("/"))
            else:
                self._send_json(404, {"error": "Not found"})

        def do_POST(self):
            if self.path == "/api/stocks":
                self._handle_add()
            else:
                self._send_json(404, {"error": "Not found"})

        # --- API handlers -----------------------------------------------

        def _handle_list(self):
            self._send_json(200, {"stocks": service.list_stocks()})

        def _handle_add(self):
            try:
                body = self._read_json_body()
                position = service.add_stock(
                    ticker=body.get("ticker"),
                    shares=body.get("shares"),
                    price=body.get("avg_price"),
                )
            except ValidationError as e:
                self._send_json(400, {"error": str(e)})
            except (json.JSONDecodeError, TypeError):
                self._send_json(400, {"error": "Invalid request body."})
            else:
                self._send_json(200, {"stock": position})

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


def run_server(service: PortfolioService, host: str = "127.0.0.1", port: int = 8000):
    handler = make_handler(service)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Stock assistant running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()
