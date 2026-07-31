# stock-agent

A personal stock assistant, an agent that updates the positions and news of your
holding stocks and provides financial suggestions daily.

A web UI to **buy**, **sell**, **delete**, and **view** stocks — plus a separate
**wishlist** of tickers you plan to buy — backed by a local storage system, with
a clean **API layer** in between. **Live market data** (real-time prices,
history, and earnings dates) is pulled from the internet to show real unrealized
gains and today's moves. The UI and storage never talk to each other directly —
everything goes through the API — so either side can be swapped independently.

## Architecture

```
   UI (frontend/)          API layer (backend/server.py)      Storage (backend/storage/)
  ┌──────────────┐  HTTP  ┌──────────────────────────────┐   ┌────────────────────────┐
  │ index.html   │ ─────► │ GET/POST /api/stocks          │─► │ StorageBackend         │
  │ app.js       │        │ GET/POST /api/wishlist        │   │   └─ JSONStorage       │
  └──────────────┘ ◄───── │ GET      /api/summary         │◄─ │      (data/*.json)     │
                    JSON   │  ▼ PortfolioService / Sales   │   └────────────────────────┘
                          │  ▼ MarketService (enrich)     │   ┌────────────────────────┐
                          │      business logic           │─► │ MarketDataProvider     │
                          └──────────────────────────────┘◄─ │   (Yahoo Finance)      │
                                                              └────────────────────────┘
```

- **UI layer** (`frontend/`) — a plain HTML/CSS/JS page. Speaks only HTTP.
- **API layer** (`backend/server.py`) — the only bridge. Exposes a REST API.
- **Business logic** (`backend/service.py`) — accumulation + weighted-average
  price, plus `MarketService`, which enriches holdings/wishlist with live prices
  and computes real unrealized gains. Independent of UI and storage.
- **Storage layer** (`backend/storage/`) — an abstract `StorageBackend`
  interface plus a local `JSONStorage` implementation.
- **Market data layer** (`backend/market_data.py`) — `MarketDataProvider`, an
  I/O boundary onto Yahoo Finance's public endpoints (stdlib `urllib` only). Like
  storage, it's swappable: implement the same methods against another data
  source and change one line in `run.py`. Every call degrades gracefully — if a
  quote can't be fetched the UI shows "—" rather than breaking.
- **AI advisor layer** (`backend/ai_advisor.py`, `backend/news_data.py`) — a
  daily agent that composes the portfolio + market data with recent news and
  asks an LLM for weekly buy/hold/sell suggestions. The LLM is pluggable via
  `AI_PROVIDER`: `GeminiClient` (Google's free public API, the default),
  `LlamaClient` (Meta's internal Llama API), `OllamaClient` (a local model —
  no key, nothing leaves the machine), or `ClaudeClient` (the public Claude
  API). All four, plus `NewsProvider`, are I/O boundaries (stdlib `urllib`
  only, like the market layer);
  `AIAdvisorService` holds the logic and the every-2h refresh. Served over
  `/api/ai`; degrades gracefully when the model or keys aren't available.

### Why it's flexible / reusable

- **New UI?** Build anything that calls `GET/POST /api/stocks` (CLI, mobile,
  another web app). No backend changes.
- **New storage?** Implement `StorageBackend` (e.g. SQLite, Postgres, cloud)
  and change one line in `run.py`. Nothing else changes.

## API

### Holdings

| Method | Path               | Purpose                                    | Body                              |
| ------ | ------------------ | ------------------------------------------ | --------------------------------- |
| GET    | `/api/stocks`      | List positions, enriched with live data    | —                                 |
| POST   | `/api/stocks`      | Buy / accumulate a position                | `{"ticker","shares","avg_price"}` |
| POST   | `/api/stocks/sell` | Sell part/all of a position                | `{"ticker","shares","price"}`     |
| DELETE | `/api/stocks`      | Delete an entire position                  | `{"ticker"}`                      |

`GET /api/stocks` returns each position enriched with live market data:

```jsonc
{
  "ticker": "AAPL", "shares": 5.0, "avg_price": 269.09,
  "cost_basis": 1345.45,          // shares * avg_price
  "price": 333.43,                // live price (null if unavailable)
  "previous_close": 338.19,
  "market_value": 1667.15,        // shares * price
  "today": { "value": -23.80, "pct": -1.41 },   // vs. previous close
  "total": { "value": 321.70, "pct": 23.91 },   // vs. average cost
  "earnings_date": "2026-07-30",  // next earnings, ISO date (null if unknown)
  "quote_ok": true                // false when the live quote couldn't be fetched
}
```

Each holding shows **today's** unrealized gain (current price vs. previous
close) and **total** unrealized gain (current price vs. average cost), both as a
dollar amount and a percentage — green when positive, red when negative — plus
the next earnings date.

### Dashboard

| Method | Path           | Purpose                                  | Body |
| ------ | -------------- | ---------------------------------------- | ---- |
| GET    | `/api/summary` | Total worth + realized/unrealized gains  | —    |

`/api/summary` returns:

```jsonc
{
  "total_worth": 2856.45,             // sum of shares * avg_price across holdings
  "realized":   { "1d": 0, "1w": 0, "1m": 0, "ytd": 0, "1y": 0 },
  "unrealized": {                     // REAL data, from live prices
    "1d":    { "value": 65.28,   "pct": 2.16,  "series": [ { "t": "…", "v": -22.9 }, … ] },
    "1w":    { "value": -100.40, "pct": -3.16, "series": [ … ] },
    "1m":    { "value": -36.25,  "pct": -1.16, "series": [ … ] },
    "ytd":   { "value": 759.96,  "pct": 32.74, "series": [ … ] },
    "1y":    { "value": 1160.41, "pct": 60.42, "series": [ … ] },
    "total": { "value": 224.39,  "pct": 7.86,  "series": [ … ] }
  },
  "intervals":        [ { "key": "1d", "label": "1D" }, … ],  // realized windows
  "unrealized_views": [ { "key": "1d", "label": "1D" }, …, { "key": "total", "label": "Total" } ]
}
```

- **Total stock worth** — the sum of every holding's cost basis
  (`shares * avg_price`). Shown as the largest text in the UI, in green.
- **Realized gains** — summed from the sales log. Each sell records
  `(sale_price - avg_price) * shares` with a UTC timestamp; the summary buckets
  those into rolling windows (1D / 1W / 1M / YTD / 1Y). Green when positive, red
  when negative. The UI remembers the selected window across restarts.
- **Unrealized gains** — now **real**, computed from live prices, with the same
  1D / 1W / 1M / YTD / 1Y toggle as realized, plus a **Total** view:
  - **1D** — `Σ (price − previous_close) × shares` (today's move), with an
    intraday line graph over today's session.
  - **1W / 1M / YTD / 1Y** — the gain accrued over the window: current price vs.
    the price at the window's start, with a daily line graph over that window.
  - **Total** — `Σ (price − avg_cost) × shares`, with a line graph over the past
    year.

  Each view shows the dollar value **and** the percentage, green when positive
  and red when negative, and the graph line/fill matches. The graph ends at the
  live headline value so the line agrees with the number above it. The UI
  remembers the selected view across restarts.

### Wishlist

| Method | Path            | Purpose                                   | Body         |
| ------ | --------------- | ----------------------------------------- | ------------ |
| GET    | `/api/wishlist` | List wishlist tickers, enriched with data | —            |
| POST   | `/api/wishlist` | Add a ticker to the wishlist              | `{"ticker"}` |
| DELETE | `/api/wishlist` | Remove a ticker from the wishlist         | `{"ticker"}` |

`GET /api/wishlist` returns each ticker with today's open, the live price, and
the change **vs. the open** (green when the price is above the open, red below):

```jsonc
{
  "ticker": "NVDA",
  "open": 193.45,                 // today's opening price (shown first)
  "price": 195.04,                // live price
  "change": 1.59, "change_pct": 0.82,   // price vs. open
  "earnings_date": "2026-08-26",
  "quote_ok": true
}
```

**Buy** — when a ticker already exists, the shares are accumulated and the
weighted-average cost basis is recomputed:

```
new_avg = (old_shares * old_avg + added_shares * added_price) / (old_shares + added_shares)
```

**Sell** — reduces the share count at the given sale price. The per-share cost
basis is *unchanged* (selling doesn't alter what your remaining shares
originally cost); selling the full position removes it. Each sale is appended to
a **sales log** (`data/sales.json`) with its realized gain/loss
(`(sale_price - avg_price) * shares`) and a timestamp, so realized gains can be
summed over time by `/api/summary`.

**Delete** — drops an entire holding outright, for fixing a mistyped ticker. It
records no sale.

**Wishlist** — a separate table of tickers you don't own yet but plan to buy
(only the ticker is stored — no shares or price). In the UI it's shown as a full
table with today's open, the live price, the change vs. the open, and the next
earnings date. Kept apart from holdings so it can later feed the AI's buy
suggestions.

**Earnings dates** — every stock in both the holdings and wishlist tables shows
its next upcoming earnings report date, fetched from the market data provider.

### AI Advisor

A daily AI agent that turns your portfolio into concrete buy / hold / sell calls
for the week ahead. It sits in its own column on the right of everything else.

| Method | Path             | Purpose                                          | Body |
| ------ | ---------------- | ------------------------------------------------ | ---- |
| GET    | `/api/ai`        | Latest suggestions (cached) + status             | —    |
| POST   | `/api/ai/refresh`| Trigger a background regeneration                 | —    |

**What it reads** — everything the app already computes, plus news:

- all your holdings, share counts, average cost, and cost basis,
- the current market price and today's move (momentum),
- the full unrealized-gain history (1D / 1W / 1M / YTD / 1Y / Total),
- realized gains from the sales log,
- recent news headlines per ticker (from a finance-news API).

**What it suggests** — for each holding: **buy more**, **hold**, **sell part**,
or **sell all**, with a horizon of **at most a week**, a concrete price trigger
where relevant (e.g. "if it drops below $320 this week, sell before it does"),
and the reasoning behind the call.

**Risk toggle** — every refresh generates two sets, one **low-risk** and one
**high-risk**; the toggle switches between them instantly with no new API call.
Low risk prioritises protecting capital; high risk prioritises upside.

**The UI**

- A **summary** at the very top: one bullet per stock — ticker, the call, and in
  how many days — plus a one-line rationale and an overall portfolio note.
- A **See details** button switches to a details tab: the ~10-line reasoning for
  each stock, its price trigger, and its main risk. **Back to summary** returns.

**Refresh** — regenerates automatically **every two hours during US market
hours** (and once on startup), and you can force a refresh with the button. The
latest result is cached (and persisted to `data/ai_suggestions.json`) so it shows
instantly and survives restarts.

`GET /api/ai` returns:

```jsonc
{
  "configured": true,          // whether a model is configured (Ollama: always true)
  "news_configured": true,     // false when FINNHUB_API_KEY isn't set
  "refreshing": false,         // true while a regeneration is in flight
  "market_open": true,
  "refresh_hours": 2,
  "generated_at": "2026-07-30T14:00:00+00:00",
  "risk_profiles": {
    "low":  { "portfolio_note": "…", "suggestions": [ /* per stock */ ] },
    "high": { "portfolio_note": "…", "suggestions": [ … ] }
  },
  "error": null
}
```

Each suggestion is `{ticker, action, horizon_days, headline, reasoning,
price_trigger, risks}` where `action` ∈ `buy | hold | trim | sell` and
`horizon_days` is 1–7.

> **Not financial advice.** Suggestions are generated by a model and can be
> wrong — verify before you trade.

## Run it

Requires only Python 3 (no third-party packages):

```bash
python3 run.py
```

Then open http://127.0.0.1:8000 in your browser.

**AI advisor (optional).** The advisor's LLM is pluggable via `AI_PROVIDER`.
The default is **`gemini`** — Google's free public Gemini API (no credit card,
generous quota, strong news grounding, package-free via stdlib `urllib`).

*Provider 1 — Google Gemini API (default, free).* No credit card, public:

```bash
# 1. Get a key: https://aistudio.google.com/app/apikey
export GEMINI_API_KEY="AIza..."
# Optional: GEMINI_MODEL (default gemini-2.0-flash), GEMINI_API_BASE
#   - gemini-2.0-flash:        15 RPM / 1M TPM / 200 RPD  (balanced, default)
#   - gemini-2.0-flash-lite:   30 RPM / 1M TPM / 200 RPD  (fastest RPM)
#   - gemini-2.5-flash-lite:   15 RPM / 250K TPM / 1000 RPD (highest RPD)
#   - Context: 1M tokens, supports Google Search grounding (500 RPD free)
python3 run.py
```

Optional: `GEMINI_API_BASE` (default `https://generativelanguage.googleapis.com/v1beta`).
For OpenAI-compatible mode: `GEMINI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai`.

*Provider 2 — Meta Llama API (internal).* One-time setup, on the corp network / VPN:

```bash
AI_PROVIDER=llama
# 1. Create a key:        https://www.internalfb.com/metagen/tools/llm-api-keys
# 2. Create an entitlement: https://www.internalfb.com/metagen/tools/entitlements
export LLAMA_API_KEY="LLM|<app-id>|<secret>"
export LLAMA_MODEL="llama4-scout-17b-16e-instruct"   # optional
python3 run.py
```

Optional: `LLAMA_API_BASE` (default `https://api.llama.com`). Must be on VPN.

*Provider 3 — local model via Ollama* (`AI_PROVIDER=ollama`). Free, offline,
nothing leaves your machine:

```bash
# Install Ollama (https://ollama.com/download), then:
ollama serve                 # background server
ollama pull llama3.1         # a model
AI_PROVIDER=ollama python3 run.py
```

Optional: `OLLAMA_MODEL` (default `llama3.1`), `OLLAMA_HOST`
(default `http://localhost:11434`).

*Provider 4 — public Claude API* (`AI_PROVIDER=claude`):

```bash
export AI_PROVIDER=claude
export ANTHROPIC_API_KEY=sk-ant-...
python3 run.py
```

**News (optional, any provider)** — a free Finnhub key adds news context; without
it the advisor runs on your price and history data alone:

```bash
export FINNHUB_API_KEY=...
```

Everything degrades gracefully: if the model isn't reachable the advisor column
shows the error and the rest of the app works as before. Every client talks to
its API over the standard library only — no SDK, matching the rest of the app.

Live prices, history, and earnings dates are fetched from Yahoo Finance, so an
internet connection is needed for those. If the network (or Yahoo) is
unavailable, the app still runs — live fields just show "—" until it recovers.

Holdings are stored locally in `data/portfolio.json`, the wishlist in
`data/wishlist.json`, and the sales log in `data/sales.json` — separate tables.
