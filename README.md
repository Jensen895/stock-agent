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
- **Portfolios layer** (`backend/workspace.py`) — lets you keep several separate
  portfolios (holdings + wishlist + sales + AI suggestions each), switch between
  them, and name/create/delete them. `WorkspaceManager` tracks which portfolio is
  active and **persists that choice**, so the app reopens on whichever one you
  last used. `WorkspaceStorage` is a `StorageBackend` that redirects every read/
  write to the *active* portfolio's files, so switching portfolios repoints every
  service at once with no rewiring — and no two portfolios ever mix. Served over
  `/api/portfolios`. Deleted portfolios are archived (moved to
  `data/portfolios/_archive/`), never erased.
- **Market data layer** (`backend/market_data.py`) — `MarketDataProvider`, an
  I/O boundary onto Yahoo Finance's public endpoints (stdlib `urllib` only). Like
  storage, it's swappable: implement the same methods against another data
  source and change one line in `run.py`. Every call degrades gracefully — if a
  quote can't be fetched the UI shows "—" rather than breaking.
- **Analyst data layer** (`backend/analyst_data.py`) — `YahooAnalystProvider`,
  an I/O boundary onto what the big financial firms **conclude**: the consensus
  rating, the bull / hold / bear head count, the price-target spread, and the
  recent calls of individual desks (Goldman Sachs, JP Morgan, Morgan Stanley,
  …), plus the disagreement between them made explicit. **No API key** — it
  reuses the Yahoo session the market layer already holds. This is *evidence
  fed to both models*, not a scoring source — see
  [Wall Street is an input, not a vote](#wall-street-is-an-input-not-a-vote).
- **Fundamentals layer** (`backend/fundamentals_data.py`) —
  `YahooFundamentalsProvider`, an I/O boundary onto what those firms are
  **looking at**: trailing valuation multiples, margins and returns, growth,
  cash and debt, beta, the 52-week range, short interest, and the actual EPS
  printed over the last four quarters. This one *does* go into the models'
  prompt, so a model weighing a Strong Buy rating knows whether the stock
  trades at 12x earnings or 160x. An **allowlist** of 35 named fields decides
  what crosses over, keeping the prompt focused and its token cost
  predictable.
- **AI advisor layer** (`backend/ai_advisor.py`, `backend/news_data.py`) — a
  daily agent that composes the portfolio + market data with recent news, asks
  the LLMs for a one-to-three-month view, and blends their answers with the analyst
  consensus into **one 0-100 confidence score per holding**. The LLM is
  pluggable via `AI_PROVIDER`: `GeminiClient` (Google's free API, the default),
  `GroqClient` (Groq's free API), `LlamaClient` (Meta's internal Llama API),
  `OllamaClient` (a local model — no key, nothing leaves the machine), or
  `ClaudeClient` (the public Claude API). `AI_PROVIDER` takes a *list*, and
  **two models are asked every refresh** and the score is the average of their
  two answers. News comes from
  `CompositeNewsProvider` — Yahoo Finance first, Google News RSS as a fallback,
  **neither needing an API key**. Every client and provider is an I/O boundary
  (stdlib `urllib` only, like the market layer); `AIAdvisorService` holds the
  logic and the every-2h refresh. Served over `/api/ai`; degrades gracefully
  when a model, key, or source isn't available.

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

A daily AI agent that turns your portfolio into a **confidence score** per
holding for the next one to three months. It sits in its own column on the right of everything
else.

| Method | Path             | Purpose                                          | Body |
| ------ | ---------------- | ------------------------------------------------ | ---- |
| GET    | `/api/ai`        | Latest suggestions (cached) + status             | —    |
| POST   | `/api/ai/refresh`| Trigger a background regeneration                 | —    |

#### The confidence score

Every holding gets **one number from 0 to 100**, and the number *is* the call:

```
  0 ──────────── 25 ──────────── 50 ──────────── 75 ──────────── 100
 sell it all    trim it        HOLD            buy more      buy hard
```

A score in the **mid-40s to mid-50s means hold**; the further it sits from 50,
the stronger the buy (above) or sell (below) signal. The panel shows the number,
the band it falls in ("Strong buy", "Lean trim", …), and a meter marking how far
from neutral it is.

That score is the **weighted average of the two AI models, and nothing else**.

| # | Source     | Where it comes from                                     |
| - | ---------- | -------------------------------------------------------- |
| 1 | AI model A | first entry in `AI_PROVIDER`, scores each holding 0-100    |
| 2 | AI model B | second entry, asked the same question independently        |

**Weights** are equal by default. To lean on one model, set `AI_SOURCE_WEIGHTS`
keyed by its label:

```bash
AI_SOURCE_WEIGHTS="gemini:gemini-3.6-flash=2"   # double this one
```

A model that doesn't answer for a ticker drops out and the remaining weight is
**renormalised**, so a lone survivor's score passes through unchanged rather
than being dragged toward neutral by the gap.

#### Wall Street is an input, not a vote

The sell-side research — Goldman Sachs, JP Morgan, Morgan Stanley and the rest
— goes **into both models' prompts** as evidence. Each model weighs it against
the fundamentals, the momentum and the news, then reaches its own number. The
firms never score anything directly.

|                                                              | Read by the models | Scores anything |
| ------------------------------------------------------------ | :----------------: | :-------------: |
| Consensus rating and 1-5 mean, bull/hold/bear head count       | ✅ | ❌ |
| Price targets: mean, high, low, and what each implies from here | ✅ | ❌ |
| Which firms upgraded, downgraded, initiated or reiterated       | ✅ | ❌ |
| Fundamentals, momentum, gain history, news                      | ✅ | ❌ |
| **The two AI models' own 0-100 conviction**                     | —  | ✅ |

**Why it changed.** An earlier version averaged the street in as a third,
independent source. Mechanical averaging turned out to be the wrong tool.
Measured across a real 23-holding portfolio, the consensus mean only ever ranged
**1.3 to 3.5 of a nominal 1-5 scale**, and **22 of 23 stocks scored above 50** —
the desks essentially never publish a bearish rating. Any fixed mapping onto a
0-100 conviction scale is therefore mis-centred by construction, and the blend
inherited a systematic upward bias that no choice of weights really fixed. (The
horizon was the other suspect and it was not the cause: matching the models'
timescale to the street's twelve-month view moved the gap by less than a point.)

A model can do what an average cannot — notice the skew and discount it. So the
prompt states plainly that sell-side ratings are structurally bullish, that a
"Buy" is closer to their neutral than to real enthusiasm, and that a fresh
downgrade tells you more than a standing rating. Each model must say in its
reasoning what it made of the street's view and whether it sided with it.

**Surfacing the contradictions.** A consensus label hides the interesting part.
"Buy, 2.07/5" can mean forty desks quietly agreeing or a genuine fight, and the
second is a far weaker signal. So the models also get the disagreement spelled
out — the bull/bear split, how wide the price targets are spread relative to
their mean, what the extremes imply from today's price, how many firms upgraded
versus downgraded — plus a one-line `disagreement_note`:

> 41 of 51 ratings are buy with none saying sell and 10 on hold; price targets
> run $320 to $1,250 (161% of the mean target), implying anything from −34% to
> +159% from here

It works. Every call now engages with the street explicitly — for example
*"While Wall Street rates AMD a Strong Buy with a mean target of $579.11, the
vast spread ($320 to $1,250) indicates high uncertainty"*, against a model that
agreed elsewhere: *"We align with this view as Broadcom boasts phenomenal
fundamentals: 48.98% operating margins, 37.28% ROE."*

**The trade-off, stated plainly.** The two remaining sources are no longer
independent of each other, since both read the same street view. When they
agree, that is weaker evidence than it was when one of the three opinions was
formed without seeing the others. What is gained is that the street's view is
now *reasoned about* rather than averaged in.

**What it reads** — everything the app already computes, plus fundamentals,
analyst research and news:

- all your holdings, share counts, average cost, and cost basis,
- the current market price and today's move (momentum),
- the full unrealized-gain history (1D / 1W / 1M / YTD / 1Y / Total),
- realized gains from the sales log,
- **company fundamentals per ticker** — trailing and forward P/E, PEG, P/S, P/B
  and EV/EBITDA, gross / operating / net margins, ROE and ROA, year-over-year
  revenue and earnings growth, cash, debt, debt/equity, current ratio, free and
  operating cash flow, beta, the 52-week range, 50- and 200-day averages, short
  interest, institutional ownership, and the last four quarters of actual EPS
  with how far each beat or missed,
- **the Wall Street research** described above,
- recent news headlines per ticker over the last 30 days (Yahoo Finance, falling
  back to Google News RSS — no API key needed for either).

**Why the news matters.** The models are never asked to search; they are handed
headlines. Two reasons. Free-tier Gemini *cannot* use Google Search grounding —
adding the `google_search` tool makes the request fail outright with
`429 RESOURCE_EXHAUSTED` while the same ungrounded request succeeds. And a model
asked about "recent news" with none supplied does not say "I don't know": it
invents specific, confident, wrong headlines. Real headlines are what keep the
suggestions honest.

**What it suggests** — for each holding: the confidence score and the band it
implies (**buy more**, **hold**, **sell part**, **sell all**), with a horizon of
**one to three months**, a concrete price trigger where relevant (e.g. "add
below $280; if it closes under $240 the thesis is broken, sell"), and the
reasoning behind the call.

**Risk toggle** — every refresh generates two sets, one **low-risk** and one
**high-risk**; the toggle switches between them instantly with no new API call.
Low risk prioritises protecting capital; high risk prioritises upside.

**Agreement** — each row shows the three source scores next to the average, so
you can see the spread yourself rather than read a badge about it. The backend
still grades it for the aggregate status line, measured as the **spread**
between scores rather than by comparing labels: within 15 points is `agree`,
more than 35 apart is `split`, anything between is `mixed`. Comparing labels
instead flagged nearly every holding as split, since the street rarely publishes
anything below "buy". The first model in `AI_PROVIDER` leads and supplies the
prose. This is affordable because the job is tiny — two risk profiles times two
models, a few times a day, at roughly 1.5k input tokens a call — and every call
(analyst ratings included) runs concurrently, so a refresh takes about 40s
rather than the sum of its parts. With one model configured, calls are marked
`single` and nothing else changes.

**Two guardrails on the model output**, both earning their keep on real runs:

- *Scale inversion.* Models sometimes read "confidence" as certainty in their
  own recommendation, returning `100` next to `"sell"` — the exact inverse of
  what the number means here. One flipped vote moves a blended score by ~30
  points, so the action enum bounds the score: a score outside its action's
  range is pulled to the nearest edge, and the correction is logged.
- *Invented tickers.* A model answering for `HIMSS` when you hold `HIMS` would
  add a phantom position to the panel, so rows are anchored to your actual
  holdings and unknown symbols are dropped.

**The UI**

- A **summary** at the very top: one bullet per stock — ticker, the blended
  score, the band it falls in, then **the three numbers behind it** in smaller
  type (`AI 30 · AI 20 · WS 88`), and over how many months — plus a meter, a
  one-line rationale, and an overall portfolio note. The three always appear in
  the same order (model A, model B, Wall Street), so the last one is always the
  street; a source with no score for that ticker shows an em dash, and hovering
  any number names its source. The status line reads e.g. "3 sources (2 AI +
  Wall Street) — 9/23 agreed · 8 split — avg score 65".
- A **See details** button switches to a details tab: the ~10-line reasoning for
  each stock, its price trigger, its main risk, and the full per-source
  breakdown including the individual firms. **Back to summary** returns.

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
  "analysts_configured": true, // Wall Street: evidence in both prompts (keyless)
  "analyst_source": "Yahoo Finance analyst ratings",
  "fundamentals_configured": true,  // also prompt-side, not a scoring source
  "fundamentals_source": "Yahoo Finance fundamentals",
  "source_weights": { "model": 1.0 },   // only the AI models carry weights
  "refreshing": false,         // true while a regeneration is in flight
  "market_open": true,
  "refresh_hours": 2,
  "generated_at": "2026-07-30T14:00:00+00:00",
  "model_errors": null,        // non-null if a model failed but others answered
  "risk_profiles": {
    "low": {
      "portfolio_note": "…",
      "models": ["gemini:gemini-3.6-flash", "groq:llama-3.3-70b-versatile"],
      "avg_confidence": 61.4,
      "agreement": { "agreed": 2, "mixed": 1, "split": 1, "total": 4 },
      "suggestions": [
        {
          "ticker": "AAPL",
          "confidence": 71.1,              // the blend — 0-100, 50 = hold
          "action": "buy",                 // the band it falls in
          "confidence_label": "Buy",
          "horizon_months": 2,
          "headline": "…", "reasoning": "…", "price_trigger": "…", "risks": "…",
          "consensus": "agree",            // agree | mixed | split | single
          "sources": [
            { "kind": "model", "name": "gemini:gemini-3.6-flash",
              "confidence": 80.0, "action": "buy", "label": "Strong buy",
              "weight": 1.0, "detail": "…" },
            { "kind": "model", "name": "groq:llama-3.3-70b-versatile",
              "confidence": 60.0, "action": "buy", "label": "Lean buy",
              "weight": 1.0, "detail": "…" },
          ],
          // The research BOTH models read before scoring. Carries no score and
          // takes no part in the average — it is kept so the UI can show the
          // evidence they reasoned from.
          "wall_street": {
            "rating": "Buy", "mean": 2.07, "analyst_count": 41,
            "distribution": { "strongBuy": 6, "buy": 22, "hold": 13,
                              "sell": 2, "strongSell": 3 },
            "bulls": 28, "neutral": 13, "bears": 5,
            "target": { "mean": 324.01, "high": 400.0, "low": 215.0,
                        "upside_pct": 4.18, "spread_pct": 57.1,
                        "low_implies_pct": -30.9, "high_implies_pct": 28.6 },
            "recent_upgrades": 2, "recent_downgrades": 1,
            "firms": [ { "firm": "JP Morgan", "grade": "Overweight",
                         "action": "main", "action_label": "reiterated",
                         "from_grade": "Overweight", "date": "2026-07-31",
                         "price_target": 340.0 } ],
            "disagreement_note": "28 of 46 ratings are buy while 5 say sell …",
            "summary": "41 analysts · Buy (2.07/5) · target $324.01 (+4.18%)"
          }
        }
      ]
    },
    "high": { … }
  },
  "error": null
}
```

Each suggestion is `{ticker, confidence, action, confidence_label,
horizon_months, headline, reasoning, price_trigger, risks, consensus, sources,
wall_street}` where `confidence` is 0–100, `action` ∈ `buy | hold | trim | sell`
(derived from the score), and `horizon_months` is 1–3. `sources` holds only the
AI models; `wall_street` is unscored evidence. Suggestions saved before the switch to
a quarterly view carry `horizon_days` instead; both backend and UI still read
them.

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

The **third** confidence source — the big financial firms' analyst consensus —
needs no key or configuration at all; it comes from the same Yahoo session the
price data already uses. To change how much each source counts toward the
blended score, set `AI_SOURCE_WEIGHTS` (equal by default). Only the AI models
carry weights — Wall Street is evidence inside their prompts, so how much it
counts is now the models' judgement rather than a dial:

```bash
AI_SOURCE_WEIGHTS="gemini:gemini-3.6-flash=2"    # lean on one model
```

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

Holdings, wishlist, sales log, and AI suggestions are stored locally, one set
per portfolio, under `data/portfolios/<id>/` (`portfolio.json`, `wishlist.json`,
`sales.json`, `ai_suggestions.json`) — separate tables. Which portfolios exist
and which one is active live in `data/portfolios/index.json`. Older single-
portfolio installs (with the files directly under `data/`) are migrated into a
first **"My Portfolio"** automatically on first launch, so no history is lost.

### Portfolios

Keep several separate portfolios and switch between them from the dropdown at the
top center of the page. Each is its own workspace — holdings, wishlist, sales,
and AI suggestions never mix — and every action is saved immediately. The app
remembers the portfolio you were last viewing and reopens on it. Each portfolio
also remembers **its own AI-advisor risk toggle** (low / high), so switching
portfolios restores that portfolio's setting.

| Method | Path                      | Purpose                                | Body            |
| ------ | ------------------------- | -------------------------------------- | --------------- |
| GET    | `/api/portfolios`         | List portfolios (+ active, + risk)     | —               |
| POST   | `/api/portfolios`         | Create a new portfolio (and switch)    | `{"name"}`      |
| POST   | `/api/portfolios/switch`  | Switch the active portfolio            | `{"id"}`        |
| POST   | `/api/portfolios/rename`  | Rename a portfolio                     | `{"id","name"}` |
| POST   | `/api/portfolios/risk`    | Remember the AI risk toggle (active)   | `{"risk"}`      |
| DELETE | `/api/portfolios`         | Delete (archive) a portfolio           | `{"id"}`        |

You can't delete your only portfolio. Deleting the active one moves you to
another; the deleted portfolio's data is archived under
`data/portfolios/_archive/`, not erased.
