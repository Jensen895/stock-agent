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
from backend.actions import AiActionsService
from backend.ai_agents import AGENT_KEYS

# Personal, untracked provider: the Claude Code CLI installed on this machine,
# with every agent's answer written to a fixed file under data/claude_responses/
# and read back from there. It is gitignored, so a fresh clone simply won't have
# it — hence the guarded import. AI_PROVIDER=local-claude selects it; without
# the file that name is skipped like any other unconfigured provider.
try:
    from backend.ai_local_claude import LocalClaudeClient
except ImportError:  # pragma: no cover - the file is optional by design
    LocalClaudeClient = None
from backend.analyst_data import YahooAnalystProvider
from backend.discover import DiscoverService
from backend.fundamentals_data import YahooFundamentalsProvider
from backend.market_data import MarketDataProvider
from backend.news_data import (
    CompositeNewsProvider,
    FinnhubNewsProvider,
    GoogleNewsRSSProvider,
    MacroNewsProvider,
    YahooNewsProvider,
)
from backend.server import run_server
from backend.trending_data import TrendingBoard
from backend.service import (
    AvailableCashService,
    MarketService,
    PortfolioService,
    SalesService,
    SummaryService,
    WishlistService,
)
from backend.workspace import WorkspaceManager, WorkspaceStorage

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PORTFOLIOS_DIR = os.path.join(DATA_DIR, "portfolios")


def _agent_weights():
    """Read the default agent weights from AI_AGENT_WEIGHTS, if set.

    The advisor scores each stock 0-100 by averaging five independent agents,
    which count equally by default. To start a fresh portfolio leaning on some
    of them more than others, key by agent::

        AI_AGENT_WEIGHTS="statistics=2,macro=0.5"
        AI_AGENT_WEIGHTS="expert=0"        # ignore Wall Street entirely

    Valid keys are the five agent keys in ``backend/ai_agents.py``:
    company_perspective, personal, statistics, expert, macro.

    This is only the *default*. Each portfolio stores its own weights in
    ``ai_weights.json`` the moment you move a slider in the UI, and those take
    precedence — so this variable is for setting a house default, not for
    day-to-day tuning. Returns None when unset; a malformed or unknown entry is
    skipped rather than crashing the app on boot.
    """
    raw = os.environ.get("AI_AGENT_WEIGHTS", "").strip()
    if not raw:
        return None
    weights = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.rsplit("=", 1)
        key = key.strip()
        if key not in AGENT_KEYS:
            print(
                f"AI advisor: ignoring unknown agent '{key}' — "
                f"expected one of {', '.join(AGENT_KEYS)}."
            )
            continue
        try:
            weights[key] = float(value)
        except ValueError:
            print(f"AI advisor: ignoring bad weight '{part.strip()}'.")
    if weights:
        print(f"AI advisor: default agent weights {weights}")
    return weights or None


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
    # "Available to trade": the cash on hand to buy with. The one figure the app
    # can't derive, so it's typed in — and left *vacant* until it is, which is a
    # third state distinct from $0 (see the service). Three things read it:
    # buying draws it down, the AI Actions plan is sized against it instead of
    # the stand-in $10,000, and every agent is told what there is to spend.
    available = AvailableCashService(WorkspaceStorage(manager, "available.json"))
    portfolio = PortfolioService(portfolio_storage, sales=sales, available=available)
    # wishlist also reads holdings so it can reject stocks you already own.
    wishlist = WishlistService(
        WorkspaceStorage(manager, "wishlist.json"), holdings=portfolio_storage
    )
    # live market data (Yahoo Finance) — powers real prices, unrealized gains,
    # and earnings dates. Swap this provider to change data sources.
    market_data = MarketDataProvider()
    market = MarketService(market_data, portfolio)
    # dashboard summary: total worth + available to trade + realized + real
    # unrealized gains.
    summary = SummaryService(portfolio, sales, market, available=available)

    # AI advisor: five independent agents turn the portfolio into one 0-100
    # confidence score per stock for the next one to three months
    # (100 = buy hard, 50 = hold, 0 = sell out).
    #
    # The score is the weighted average of the five agents in
    # backend/ai_agents.py — company perspective, your own position and price
    # history, raw statistics, Wall Street, and macro/policy news. Each is a
    # separate call over its own disjoint slice of the evidence; none of them
    # sees another's work. The weights are per-portfolio and editable from the
    # UI, and changing one re-blends the cached scores without calling a model.
    #
    # AI_PROVIDER is a comma-separated list of MODELS, not of opinions: agents
    # are handed out round-robin across whatever is configured, so a second and
    # third provider buy parallelism and quota headroom rather than a second
    # vote. Any provider without a key is skipped, so the default works with a
    # Gemini key alone and picks up Groq the moment GROQ_API_KEY appears.
    #   gemini: export GEMINI_API_KEY=...  from https://aistudio.google.com/app/apikey
    #           optional: GEMINI_MODEL, GEMINI_MODEL_B, GEMINI_API_BASE
    #   groq:   export GROQ_API_KEY=...    from https://console.groq.com/keys
    #           free tier, no credit card. optional: GROQ_MODEL, GROQ_API_BASE
    #           (this is Groq the inference provider, not xAI's Grok)
    #   llama:  export LLAMA_API_KEY="LLM|..."   (on VPN/corp network)
    #           key: https://www.internalfb.com/metagen/tools/llm-api-keys
    #   ollama: ollama serve; ollama pull llama3.1   (local, no key)
    #   claude: export ANTHROPIC_API_KEY=...        (public Claude API)
    #   local-claude: the `claude` CLI already installed on this machine — no
    #           key, it uses whatever account Claude Code is logged into, and
    #           every agent's answer is saved to a fixed file under
    #           data/claude_responses/ and read back from there. Untracked;
    #           see backend/ai_local_claude.py and LOCAL_CLAUDE.md.
    #
    # News needs no API key: Yahoo Finance first, Google News RSS as the
    # fallback for tickers Yahoo has nothing on. Set FINNHUB_API_KEY to append
    # Finnhub (richer summaries) as a last resort. Feeding real headlines is not
    # optional — given none, the agents invent confident, wrong ones.
    def _build_client(name):
        if name in ("local-claude", "claude-cli"):
            if LocalClaudeClient is None:
                print(
                    "AI advisor: 'local-claude' requested but "
                    "backend/ai_local_claude.py isn't present (it's gitignored) "
                    "— skipping."
                )
                return None
            return LocalClaudeClient()
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
    # Cap at one model per agent. Beyond that there is nothing left to spread:
    # models are execution capacity here, and a fifth would sit idle.
    llms = [c for c in llms if c.available()][: len(AGENT_KEYS)] or llms[:1]

    # Company news — the company-perspective agent's evidence.
    news = CompositeNewsProvider(
        [
            YahooNewsProvider(),
            GoogleNewsRSSProvider(),
            FinnhubNewsProvider(api_key=os.environ.get("FINNHUB_API_KEY")),
        ]
    )
    print(f"News: {news.describe()}")

    # The other kind of news — rates, tariffs, war, policy — with nothing
    # company-specific in it. This is the macro agent's entire evidence base,
    # and keeping it in its own provider is what guarantees the macro agent
    # can't quietly re-derive the company agent's answer. No API key.
    macro_news = MacroNewsProvider()
    print(f"Macro news: {macro_news.describe()}")

    # What the big financial firms conclude — the consensus rating, the
    # bull/bear split, how far apart the price targets are, and who upgraded or
    # downgraded lately. The expert agent's evidence, and nobody else's. No API
    # key — it reuses the Yahoo session the market data holds.
    analysts = YahooAnalystProvider(market_data)
    print(f"Analysts: {analysts.describe()}")

    # The figures, split by ai_agents.py between the company agent (growth, the
    # earnings record, what the business does) and the statistics agent (the
    # multiples and the balance sheet). Neither sees the other's fields.
    fundamentals = YahooFundamentalsProvider(market_data)
    print(f"Fundamentals: {fundamentals.describe()}")

    advisor = AIAdvisorService(
        llms, news, market, portfolio, summary,
        storage=WorkspaceStorage(manager, "ai_suggestions.json"),
        analysts=analysts, agent_weights=_agent_weights(),
        fundamentals=fundamentals, wishlist=wishlist, macro_news=macro_news,
        weights_storage=WorkspaceStorage(manager, "ai_weights.json"),
        sales=sales, cash=available,
    )
    # Refreshes once per trading day, at the opening bell (plus once on boot if
    # nothing is cached). The Refresh button in the UI forces one any time.
    advisor.start_scheduler()

    # Discover: the one panel that starts from outside your list. It reads what
    # three rooms are talking about — retail chatter, the financial press, and
    # the WSJ — drops everything already held or watched, and sends the top
    # three through the *same* five agents the advisor uses, so a stock found
    # on Reddit and a stock you've held for a year carry comparable scores.
    #
    # No API key: the lanes are Reddit's public JSON (StockTwits when Reddit is
    # blocked, which some networks do), Google News RSS, and the WSJ via Google
    # News. Candidate symbols are validated against Yahoo before they can reach
    # the board, which is what keeps CNBC and "Weak Jobs Report" off it.
    #
    # Cost: five agents x two risk profiles, so up to ten more model calls per
    # day on top of the advisor's twenty. It shares the advisor's daily bell but
    # waits for it to finish first, to keep the burst off a free tier's
    # per-minute quota.
    board = TrendingBoard()
    print(f"Discover: {board.describe()}")
    discover = DiscoverService(
        board, advisor, market, news, wishlist, portfolio,
        fundamentals=fundamentals, analysts=analysts,
        storage=WorkspaceStorage(manager, "discover.json"),
    )
    discover.start_scheduler()

    # AI Actions: the last step after all the scoring — what to actually do
    # today, in shares, against the cash you have available. It calls no model
    # and stores nothing; it is arithmetic over the suggestions the advisor and
    # discover panels have already produced, so it costs a page load and moves
    # the moment either of them (or an agent weight, or the balance) changes.
    #
    # Sized against "Available to trade" when that has been filled in — then the
    # share counts are orders you could actually place. Until it is, it falls
    # back to a pretend $10,000 and labels it as one: the app doesn't know
    # what's in your brokerage account and won't invent it, so the dollar
    # figures are a scale and the proportions are the answer. Change the
    # fallback by passing budget= here.
    actions = AiActionsService(advisor, discover, market, portfolio,
                               available=available)

    run_server(portfolio, wishlist, summary, market, advisor, manager, discover,
               actions, available, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
