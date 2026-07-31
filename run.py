#!/usr/bin/env python3
"""Entry point for the personal stock assistant.

This is the single place where the layers are wired together:

    storage backend  ->  service (business logic)  ->  API server  ->  UI

To use a different storage system, swap the one line below for another
StorageBackend implementation. Nothing else changes.
"""

import os


def _load_dotenv():
    """Load .env file from project root into os.environ (no external deps).

    Never overwrites existing env vars, so shell exports take precedence.
    Ignores malformed lines and comments.
    """
    root = os.path.dirname(__file__)
    for name in (".env", ".env.local"):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    # Strip trailing comma if user pasted with comma
                    if v.endswith(","):
                        v = v[:-1].strip()
                    if k and v and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


_load_dotenv()

from backend.ai_advisor import (
    AIAdvisorService,
    ClaudeClient,
    GeminiClient,
    LlamaClient,
    OllamaClient,
)
from backend.market_data import MarketDataProvider
from backend.news_data import NewsProvider
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
AI_FILE = os.path.join(DATA_DIR, "ai_suggestions.json")


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

    # AI advisor: a daily agent that turns the portfolio + news into buy/hold/
    # sell suggestions for the week.
    #
    # Provider is chosen by AI_PROVIDER (default "gemini" — Google's free public
    # API, no card needed, generous quota, strong news grounding).
    #   gemini (default): export GEMINI_API_KEY=... from
    #                     https://aistudio.google.com/app/apikey
    #                     optional: GEMINI_MODEL (default gemini-2.0-flash),
    #                               GEMINI_API_BASE (default
    #                               https://generativelanguage.googleapis.com/v1beta)
    #                     free tier: 15 RPM / 1M TPM / 200-1000 RPD, 1M context,
    #                     500 RPD Google Search grounding included.
    #   llama:            export LLAMA_API_KEY="LLM|..."   (on VPN/corp network)
    #                     key: https://www.internalfb.com/metagen/tools/llm-api-keys
    #                     optional: LLAMA_MODEL, LLAMA_API_BASE
    #   ollama:           ollama serve; ollama pull llama3.1   (local, no key)
    #                     optional: OLLAMA_MODEL, OLLAMA_HOST
    #   claude:           export ANTHROPIC_API_KEY=...        (public Claude API)
    # News (optional, any provider): export FINNHUB_API_KEY=...  — without it
    # suggestions run on price/history data alone.
    provider = os.environ.get("AI_PROVIDER", "gemini").lower()
    if provider == "claude":
        llm = ClaudeClient(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    elif provider == "ollama":
        llm = OllamaClient(
            host=os.environ.get("OLLAMA_HOST"),
            model=os.environ.get("OLLAMA_MODEL", "llama3.1"),
        )
    elif provider == "llama":
        llm = LlamaClient(
            api_key=os.environ.get("LLAMA_API_KEY"),
            model=os.environ.get("LLAMA_MODEL", "llama4-scout-17b-16e-instruct"),
            base_url=os.environ.get("LLAMA_API_BASE", "https://api.llama.com"),
        )
    else:  # "gemini" — Google Gemini (free public API)
        llm = GeminiClient(
            api_key=os.environ.get("GEMINI_API_KEY"),
            model=os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest"),
            base_url=os.environ.get(
                "GEMINI_API_BASE",
                "https://generativelanguage.googleapis.com/v1beta",
            ),
        )
    news = NewsProvider(api_key=os.environ.get("FINNHUB_API_KEY"))
    advisor = AIAdvisorService(
        llm, news, market, portfolio, summary,
        storage=JSONStorage(AI_FILE), refresh_hours=2,
    )
    # Refreshes every two hours during market hours (and once on boot).
    advisor.start_scheduler()

    run_server(portfolio, wishlist, summary, market, advisor,
               host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
