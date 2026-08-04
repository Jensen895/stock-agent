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
  `AI_PROVIDER`: `GeminiClient` (Google's free API, the default),
  `GroqClient` (Groq's free API), `LlamaClient` (Meta's internal Llama API),
  `OllamaClient` (a local model — no key, nothing leaves the machine), or
  `ClaudeClient` (the public Claude API). `AI_PROVIDER` takes a *list*, and
  **two models are asked every refresh** so their answers can be cross-checked;
  each suggestion is tagged `agree` / `split` / `single` and the UI flags
  disagreement. News comes from `CompositeNewsProvider` — Yahoo Finance first,
  Google News RSS as a fallback, **neither needing an API key**. Every client
  and provider is an I/O boundary (stdlib `urllib` only, like the market
  layer); `AIAdvisorService` holds the logic and the every-2h refresh. Served
  over `/api/ai`; degrades gracefully when a model or key isn't available.

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
- recent news headlines per ticker (Yahoo Finance, falling back to Google News
  RSS — no API key needed for either).

**Why the news matters.** The models are never asked to search; they are handed
headlines. Two reasons. Free-tier Gemini *cannot* use Google Search grounding —
adding the `google_search` tool makes the request fail outright with
`429 RESOURCE_EXHAUSTED` while the same ungrounded request succeeds. And a model
asked about "recent news" with none supplied does not say "I don't know": it
invents specific, confident, wrong headlines. Real headlines are what keep the
suggestions honest.

**What it suggests** — for each holding: **buy more**, **hold**, **sell part**,
or **sell all**, with a horizon of **at most a week**, a concrete price trigger
where relevant (e.g. "if it drops below $320 this week, sell before it does"),
and the reasoning behind the call.

**Risk toggle** — every refresh generates two sets, one **low-risk** and one
**high-risk**; the toggle switches between them instantly with no new API call.
Low risk prioritises protecting capital; high risk prioritises upside.

**Consensus** — two models are asked the same question every refresh and their
answers are merged per ticker. Where they pick the same action the call is
marked **Both agree**; where they differ it is marked **Split** and the detail
card shows what each model said. The first model in `AI_PROVIDER` leads and
supplies the prose. This is affordable because the job is tiny — two risk
profiles times two models, a few times a day, at roughly 1.5k input tokens a
call — and all four calls run concurrently, so a refresh takes about 20s rather
than the sum of its parts. If only one model is configured, suggestions are
marked `single` and nothing else changes.

**The UI**

- A **summary** at the very top: one bullet per stock — ticker, the call, the
  consensus badge, and in how many days — plus a one-line rationale and an
  overall portfolio note. The status line reads e.g. "2 models · 2/3 agreed ·
  1 split".
- A **See details** button switches to a details tab: the ~10-line reasoning for
  each stock, its price trigger, its main risk, and the per-model breakdown.
  **Back to summary** returns.

**Refresh** — regenerates automatically **every two hours during US market
hours** (and once on startup), and you can force a refresh with the button. The
latest result is cached (and persisted to `data/ai_suggestions.json`) so it shows
instantly and survives restarts.

`GET /api/ai` returns:

```jsonc
{
  "configured": true,          // whether a model is configured (Ollama: always true)
  "models": ["gemini:gemini-3.6-flash", "groq:llama-3.3-70b-versatile"],
  "news_configured": true,     // the keyless sources make this true by default
  "news_sources": "Yahoo → GoogleNewsRSS",
  "refreshing": false,         // true while a regeneration is in flight
  "market_open": true,
  "refresh_hours": 2,
  "generated_at": "2026-07-30T14:00:00+00:00",
  "model_errors": null,        // non-null if a model failed but others answered
  "risk_profiles": {
    "low": {
      "portfolio_note": "…",
      "models": ["gemini:gemini-3.6-flash", "groq:llama-3.3-70b-versatile"],
      "agreement": { "agreed": 2, "split": 1, "total": 3 },
      "suggestions": [
        {
          "ticker": "AAPL", "action": "trim", "horizon_days": 5,
          "headline": "…", "reasoning": "…", "price_trigger": "…", "risks": "…",
          "consensus": "agree",          // agree | split | single
          "votes": [ { "model": "…", "action": "trim", "horizon_days": 5,
                       "headline": "…" } ]
        }
      ]
    },
    "high": { … }
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

**AI advisor (optional).** `AI_PROVIDER` is a comma-separated **priority list**;
the first entry leads and supplies the prose, and the first two that actually
have a key are used as the consensus pair. Anything without a key is skipped, so
one key is enough to start.

```bash
AI_PROVIDER=gemini,groq,gemini-b     # the default
```

With only a Gemini key that resolves to `gemini` + `gemini-b` — two *different*
Gemini models cross-checking each other. Add `GROQ_API_KEY` and Groq slots in
as the second opinion automatically, giving you two independent vendors.

Sizing note: this app makes roughly **8 model calls and ~15k tokens a day**.
Every free tier below clears that by orders of magnitude, so choose on accuracy,
not quota.

*Provider 1 — Google Gemini API (default, free).* No credit card:

```bash
# 1. Get a key: https://aistudio.google.com/app/apikey
export GEMINI_API_KEY="AIza..."
# Optional: GEMINI_MODEL   (default gemini-3.6-flash      — the lead)
#           GEMINI_MODEL_B (default gemini-3.5-flash-lite — the second opinion)
python3 run.py
```

Free-tier request limits vary by model and change often — check
<https://ai.google.dev/gemini-api/docs/rate-limits> rather than trusting a
number written down here. Two things worth knowing:

- **Google Search grounding is not usable on a free key.** Sending
  `tools: [{"google_search": {}}]` returns `429 RESOURCE_EXHAUSTED` immediately
  — a zero-quota entitlement, not burst limiting — while the identical
  ungrounded request succeeds. This app therefore supplies headlines itself.
- Model names come and go: `gemini-2.5-flash` now returns `404 … no longer
  available to new users`. Use `models?key=…` to list what your key can see.

Optional: `GEMINI_API_BASE` (default `https://generativelanguage.googleapis.com/v1beta`).
For OpenAI-compatible mode: `GEMINI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai`.

*Provider 2 — Groq API (free, recommended second opinion).* Free key, no credit
card. Note this is **Groq**, the inference provider running open-weight models —
not xAI's **Grok**, which has no comparable free tier:

```bash
# 1. Get a key: https://console.groq.com/keys
export GROQ_API_KEY="gsk_..."
export GROQ_MODEL="llama-3.3-70b-versatile"   # optional
python3 run.py
```

Optional: `GROQ_API_BASE` (default `https://api.groq.com/openai/v1`). Groq
retires models fairly often; if you get a 404 the error names the fix, and the
current list is at <https://console.groq.com/docs/models>.

*Provider 3 — Meta Llama API (internal).* One-time setup, on the corp network / VPN:

```bash
AI_PROVIDER=llama
# 1. Create a key:        https://www.internalfb.com/metagen/tools/llm-api-keys
# 2. Create an entitlement: https://www.internalfb.com/metagen/tools/entitlements
export LLAMA_API_KEY="LLM|<app-id>|<secret>"
export LLAMA_MODEL="llama4-scout-17b-16e-instruct"   # optional
python3 run.py
```

Optional: `LLAMA_API_BASE` (default `https://api.llama.com`). Must be on VPN.

*Provider 4 — local model via Ollama* (`AI_PROVIDER=ollama`). Free, offline,
nothing leaves your machine:

```bash
# Install Ollama (https://ollama.com/download), then:
ollama serve                 # background server
ollama pull llama3.1         # a model
AI_PROVIDER=ollama python3 run.py
```

Optional: `OLLAMA_MODEL` (default `llama3.1`), `OLLAMA_HOST`
(default `http://localhost:11434`).

*Provider 5 — public Claude API* (`AI_PROVIDER=claude`):

```bash
export AI_PROVIDER=claude
export ANTHROPIC_API_KEY=sk-ant-...
python3 run.py
```

**News — no key required.** The advisor pulls headlines from Yahoo Finance
first, falling back to Google News RSS per ticker when Yahoo has nothing. Both
are keyless, so news works out of the box.

Yahoo pads thin results with unrelated market filler — ask it about a symbol it
doesn't know and it returns generic business news — so headlines are kept only
when Yahoo's own `relatedTickers` field tags them with your symbol. A ticker
with no genuine coverage falls through to Google News rather than feeding the
model noise.

Optionally add Finnhub as a last-resort source (richer summaries, needs a free
key):

```bash
export FINNHUB_API_KEY=...
```

Everything degrades gracefully: one model failing still leaves the other's
answer (marked `single`, with the failure reported in `model_errors`); every
model failing leaves the last good suggestions on screen; a news outage leaves
the advisor running on price and history alone. Every client talks to its API
over the standard library only — no SDK, matching the rest of the app.

Live prices, history, and earnings dates are fetched from Yahoo Finance, so an
internet connection is needed for those. If the network (or Yahoo) is
unavailable, the app still runs — live fields just show "—" until it recovers.

Holdings are stored locally in `data/portfolio.json`, the wishlist in
`data/wishlist.json`, and the sales log in `data/sales.json` — separate tables.
