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
    GroqClient,
    LlamaClient,
    OllamaClient,
)
from backend.market_data import MarketDataProvider
from backend.news_data import (
    CompositeNewsProvider,
    FinnhubNewsProvider,
    GoogleNewsRSSProvider,
    YahooNewsProvider,
)
from backend.server import run_server
from backend.service import (
    MarketService,
    PortfolioService,
    SalesService,
    SummaryService,
    WishlistService,
)
from backend.workspace import WorkspaceManager, WorkspaceStorage

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PORTFOLIOS_DIR = os.path.join(DATA_DIR, "portfolios")


def main():
    # Multiple portfolios: each is its own self-contained workspace (holdings,
    # wishlist, sales, AI suggestions). The manager tracks which one is active
    # and persists that choice, so the app reopens on the portfolio you last
    # used. WorkspaceStorage redirects every table to the active portfolio's
    # files — switching portfolios repoints all the services below at once, with
    # no rewiring. (Legacy single-portfolio data under data/ is migrated in on
    # first run.)
    manager = WorkspaceManager(PORTFOLIOS_DIR)

    # <- swap storage backends here. Holdings, wishlist, and the sales log are
    # separate tables, each scoped to the active portfolio.
    portfolio_storage = WorkspaceStorage(manager, "portfolio.json")
    # sales log: every sell is recorded here so realized gains can be summed.
    sales = SalesService(WorkspaceStorage(manager, "sales.json"))
    portfolio = PortfolioService(portfolio_storage, sales=sales)
    # wishlist also reads holdings so it can reject stocks you already own.
    wishlist = WishlistService(
        WorkspaceStorage(manager, "wishlist.json"), holdings=portfolio_storage
    )
    # live market data (Yahoo Finance) — powers real prices, unrealized gains,
    # and earnings dates. Swap this provider to change data sources.
    market = MarketService(MarketDataProvider(), portfolio)
    # dashboard summary: total worth + realized + real unrealized gains.
    summary = SummaryService(portfolio, sales, market)

    # AI advisor: a daily agent that turns the portfolio + news into buy/hold/
    # sell suggestions for the week.
    #
    # Two models are asked the same question every refresh and their answers are
    # merged into a consensus, so the UI can flag where they disagree. The whole
    # job is a handful of ~1.5k-token calls a day, which every free tier absorbs.
    #
    # AI_PROVIDER is a comma-separated priority list; the first one leads and
    # supplies the prose. Any provider without a key is skipped, so the default
    # works with a Gemini key alone (two different Gemini models) and picks up
    # Groq automatically the moment GROQ_API_KEY appears.
    #   gemini: export GEMINI_API_KEY=...  from https://aistudio.google.com/app/apikey
    #           optional: GEMINI_MODEL, GEMINI_MODEL_B, GEMINI_API_BASE
    #   groq:   export GROQ_API_KEY=...    from https://console.groq.com/keys
    #           free tier, no credit card. optional: GROQ_MODEL, GROQ_API_BASE
    #           (this is Groq the inference provider, not xAI's Grok)
    #   llama:  export LLAMA_API_KEY="LLM|..."   (on VPN/corp network)
    #           key: https://www.internalfb.com/metagen/tools/llm-api-keys
    #   ollama: ollama serve; ollama pull llama3.1   (local, no key)
    #   claude: export ANTHROPIC_API_KEY=...        (public Claude API)
    #
    # News needs no API key: Yahoo Finance first, Google News RSS as the
    # fallback for tickers Yahoo has nothing on. Set FINNHUB_API_KEY to append
    # Finnhub (richer summaries) as a last resort. Feeding real headlines is not
    # optional — given none, the models invent confident, wrong ones.
    def _build_client(name):
        if name == "claude":
            return ClaudeClient(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        if name == "ollama":
            return OllamaClient(
                host=os.environ.get("OLLAMA_HOST"),
                model=os.environ.get("OLLAMA_MODEL", "llama3.1"),
            )
        if name == "llama":
            return LlamaClient(
                api_key=os.environ.get("LLAMA_API_KEY"),
                model=os.environ.get("LLAMA_MODEL", "llama4-scout-17b-16e-instruct"),
                base_url=os.environ.get("LLAMA_API_BASE", "https://api.llama.com"),
            )
        if name == "groq":
            return GroqClient(
                api_key=os.environ.get("GROQ_API_KEY"),
                model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
                base_url=os.environ.get(
                    "GROQ_API_BASE", "https://api.groq.com/openai/v1"
                ),
            )
        if name in ("gemini", "gemini-b"):
            # "gemini-b" is the second opinion: same key, a different model, so
            # consensus works before you have a second provider's key.
            default = (
                "gemini-3.6-flash" if name == "gemini" else "gemini-3.5-flash-lite"
            )
            env = "GEMINI_MODEL" if name == "gemini" else "GEMINI_MODEL_B"
            return GeminiClient(
                api_key=os.environ.get("GEMINI_API_KEY"),
                model=os.environ.get(env, default),
                base_url=os.environ.get(
                    "GEMINI_API_BASE",
                    "https://generativelanguage.googleapis.com/v1beta",
                ),
            )
        return None

    names = [
        n.strip().lower()
        for n in os.environ.get("AI_PROVIDER", "gemini,groq,gemini-b").split(",")
        if n.strip()
    ]
    llms = [c for c in (_build_client(n) for n in names) if c is not None]
    # Keep at most two models: enough for a consensus, no reason to pay for more.
    llms = [c for c in llms if c.available()][:2] or llms[:1]

    news = CompositeNewsProvider(
        [
            YahooNewsProvider(),
            GoogleNewsRSSProvider(),
            FinnhubNewsProvider(api_key=os.environ.get("FINNHUB_API_KEY")),
        ]
    )
    print(f"News: {news.describe()}")
    advisor = AIAdvisorService(
        llms, news, market, portfolio, summary,
        storage=WorkspaceStorage(manager, "ai_suggestions.json"), refresh_hours=2,
    )
    # Refreshes every two hours during market hours (and once on boot).
    advisor.start_scheduler()

    run_server(portfolio, wishlist, summary, market, advisor, manager,
               host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
