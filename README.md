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
  and computes real unrealized gains, and `AvailableCashService`, the one figure
  the app can't derive: how much cash you have to buy with, entered by hand and
  **vacant** until you do. Independent of UI and storage.
- **Storage layer** (`backend/storage/`) — an abstract `StorageBackend`
  interface plus a local `JSONStorage` implementation.
- **Portfolios layer** (`backend/workspace.py`) — lets you keep several separate
  portfolios (holdings + wishlist + sales + AI suggestions + agent weights +
  available-to-trade balance each),
  switch between them, and name/create/delete them. `WorkspaceManager` tracks which portfolio is
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
  reuses the Yahoo session the market layer already holds. This is the sole
  evidence base of the **Wall Street agent**, and no other agent sees it.
- **Fundamentals layer** (`backend/fundamentals_data.py`) —
  `YahooFundamentalsProvider`, an I/O boundary onto the figures: trailing
  valuation multiples, margins and returns, growth, cash and debt, beta, the
  52-week range, short interest, the actual EPS printed over the last four
  quarters, and what the company actually does. An **allowlist** of named
  fields decides what crosses over, and `ai_agents.py` then **splits that
  allowlist in two** — growth and the earnings record go to the company agent,
  the multiples and the balance sheet go to the statistics agent, with no
  overlap.
- **News layer** (`backend/news_data.py`) — two kinds, deliberately kept apart.
  `CompositeNewsProvider` (Yahoo Finance → Google News RSS → optional Finnhub)
  answers *per ticker* and feeds the company agent; `MacroNewsProvider` asks
  Google News a standing set of topic queries — rates, inflation, tariffs, war,
  energy, policy — and feeds the macro agent. **Neither needs an API key.**
  Separating them is what guarantees the macro agent can't quietly re-derive
  the company agent's answer from the same headlines.
- **Agents layer** (`backend/ai_agents.py`) — the five analysts, each with a
  role, a system prompt, and (crucially) a **disjoint slice** of the evidence:
  company perspective, your own position and price history, raw statistics,
  Wall Street, and macro/policy. This module decides who may see what; adding a
  sixth agent is adding a class here. It also holds the two things every agent
  is told regardless of dimension — both constraints rather than evidence, and
  both given identically to all five, so the disjoint-evidence split is
  untouched:
  - **Cash pays 4.25% APR** (`CASH_APR_PCT`). Doing nothing is not a zero, so
    the bar for a buy is "beats a risk-free ~1.06% over a quarter", staying out
    is a real answer, and a stock expected to go nowhere scores *below* 50
    instead of at it.
  - **How much cash there is** (`available_cash_note`), when
    [Available to trade](#available-to-trade) has been filled in. Not evidence
    about any company — the *size of the decision*. The same 60/100 means
    something different against $500 than against $500,000: at the small end a
    single share of an expensive name is the whole position, and a marginal buy
    costs the chance to take the good one that turns up next week. The note
    reinforces "do not ration" rather than relaxing it — the agents still score
    each ticker on its own merits and never name dollar amounts, because
    `actions.py` does the sizing against that same figure. Vacant omits the
    line entirely, leaving prompts exactly as they were.
- **AI advisor layer** (`backend/ai_advisor.py`) — the orchestration. Gathers
  the evidence once, runs the five agents concurrently over their own slices,
  and averages their scores with the per-portfolio weights into **one 0-100
  confidence score per stock**. The LLM is pluggable via `AI_PROVIDER`:
  `GeminiClient` (Google's free API, the default), `GroqClient` (Groq's free
  API), `LlamaClient` (Meta's internal Llama API), `OllamaClient` (a local
  model — no key, nothing leaves the machine), or `ClaudeClient` (the public
  Claude API). `AI_PROVIDER` takes a *list*, and agents are handed out
  round-robin across it — **models are capacity, not opinion**. Every client
  and provider is an I/O boundary (stdlib `urllib` only, like the market
  layer); `AIAdvisorService` holds the logic and the daily opening-bell
  refresh. Served over `/api/ai`; degrades gracefully when a model, key, or
  source isn't available.
- **Trending layer** (`backend/trending_data.py`) — the *inverse* of the news
  layer. There you name a ticker and ask what was written about it; here nobody
  has named a ticker yet and the tickers are the answer, mined out of three
  rooms: retail chatter (Reddit's public JSON, falling back to StockTwits when
  Reddit blocks the network), the financial press (Google News RSS), and the
  **Wall Street Journal** (via Google News restricted to `wsj.com` — Dow Jones'
  own RSS feeds still return 200 OK but have been frozen over a year in the
  past). Candidates are extracted from headlines and then **resolved against
  Yahoo**, which is what keeps CNBC, FOREX and "Weak Jobs Report" off a board
  they would otherwise top. **No API key.**
- **Discover layer** (`backend/discover.py`) — takes the trending board, drops
  everything already held or watched, and sends the top three through
  `AIAdvisorService.score_context` — *the same five agents, the same weights,
  the same 0-100 scale* as everything else on screen. That's the whole design
  decision: a stock found on Reddit and a stock you've held for a year get
  comparable numbers, instead of the discovery panel having its own private
  notion of a good idea. Served over `/api/discover`.

- **Actions layer** (`backend/actions.py`) — the last step, and the only one
  that produces an *instruction*. Every other AI layer stops at a score;
  `AiActionsService` turns those same cached scores into a short list of orders
  sized in shares against a **dummy $10,000** cash balance — what to buy, how
  much of it, what to sell, and what to leave alone. Spending it is never the
  goal: the unspent part earns 4.25% and is reported as a position, so a name
  only takes money out of cash when its conviction has earned it. It calls
  **no model** and
  stores nothing: like the agent-weight sliders, it is arithmetic over
  suggestions the advisor and discover panels already made, so it is instant,
  free, and re-derived on every request. Served over `/api/actions`.

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

| Method | Path             | Purpose                                        | Body                  |
| ------ | ---------------- | ---------------------------------------------- | --------------------- |
| GET    | `/api/summary`   | Total worth + available + realized/unrealized  | —                     |
| POST   | `/api/available` | Set the cash available to trade                | `{"amount": 2500}`    |
| DELETE | `/api/available` | Remove it — the section goes vacant            | —                     |

`/api/summary` returns:

```jsonc
{
  "total_worth": 2856.45,             // sum of shares * avg_price across holdings
  "available": { "amount": 2500.0, "vacant": false },  // or {"amount": null, "vacant": true}
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

<a id="available-to-trade"></a>

- **Available to trade** — sits immediately to the right of total worth, and is
  the only writable thing on the dashboard. The two belong side by side: one is
  money you've committed, the other is money you haven't, and a buy moves both
  at once in opposite directions.

  This is the one figure the app **cannot derive**. Holdings, gains and realized
  returns all fall out of what's been recorded; the cash sitting in a brokerage
  account waiting to be spent is known only because you said so. So it has
  three states, not two, and the difference between the last two is
  load-bearing:

  | State | Means | What the app does |
  |---|---|---|
  | **vacant** | Never entered, or removed. **The default.** | Nothing is assumed. AI Actions falls back to its stand-in $10,000 and labels it; the agents are told nothing about a budget and score exactly as they did before this existed. |
  | **$0** | "I have nothing to invest right now." | A real instruction. No buys are sized at all, and the agents are told the balance is zero — which makes them firmer about names they've gone cold on, since selling is the only way into a better position. |
  | **$n** | Real money. | AI Actions is sized against it, and every agent is told what there is to spend when it scores. |

  Clearing the input and saving is the same as pressing **Remove** — both land
  on *vacant*, so "delete the number" never quietly means zero. On disk the key
  is deleted rather than set to `0`, so the two can't be confused on the next
  read.

  **Buying subtracts automatically.** Every `POST /api/stocks` draws
  `shares × price` out of the balance, floored at zero — running it out means
  "nothing left to deploy", and a negative number would assert a debt the app
  has no way to know about. Selling does **not** credit it back: those proceeds
  land in an account the app can't see, and crediting them would turn a
  note-to-self into a ledger claiming to track the real thing.

  Per-portfolio, like everything else (`available.json`), because the money is:
  a speculative account and a retirement account have different amounts free.
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
earnings date. Kept apart from holdings because it asks a different question —
"get in?" rather than "stay in?" — which is exactly how the AI advisor scores it
(see [Wishlist buys](#wishlist-buys)).

**Earnings dates** — every stock in both the holdings and wishlist tables shows
its next upcoming earnings report date, fetched from the market data provider.

### AI Advisor

Five independent AI agents turn your portfolio into **one confidence score per
stock** for the next one to three months. It sits in its own column on the right
of everything else.

| Method | Path              | Purpose                                          | Body |
| ------ | ----------------- | ------------------------------------------------ | ---- |
| GET    | `/api/ai`         | Latest suggestions (cached) + status             | —    |
| POST   | `/api/ai/refresh` | Trigger a background regeneration                 | —    |
| POST   | `/api/ai/weights` | Set how much each agent counts, and re-blend      | `{"weights": {"statistics": 2, "expert": 0, …}}` |

#### The confidence score

Every stock gets **one number from 0 to 100**, and the number *is* the call:

```
  0 ──────────── 25 ──────────── 50 ──────────── 75 ──────────── 100
 sell it all    trim it        HOLD            buy more      buy hard
```

A score in the **mid-40s to mid-50s means hold**; the further it sits from 50,
the stronger the buy (above) or sell (below) signal. The panel shows the number,
the band it falls in ("Strong buy", "Lean trim", …), and a meter marking how far
from neutral it is.

That score is the **weighted average of the five agents**, and nothing else.

#### The five agents

Each agent is a separate LLM call with its own system prompt and its own slice
of the evidence. They run concurrently, they never see each other's answers, and
the slices **do not overlap**.

| Tag | Agent | Its question | What it sees — and *only* this |
| --- | ----- | ------------ | ------------------------------ |
| `CO` | Company perspective | Is the business on the right track? | What the company does, sector/industry, recent company headlines, next earnings date, four quarters of actual EPS vs. expectations, revenue & earnings growth |
| `ME` | My position & history | Does this match the setups that have worked? | Shares, average cost, unrealized P&L, a year of weekly closes, measured returns over 1/3/6/12m, distance from the 52-week range and the 50/200-day averages, annualised volatility, your own past sales of the name |
| `ST` | Raw statistics | Do the numbers justify the price? | P/E (trailing & forward), PEG, EPS, market cap, P/S, P/B, EV/EBITDA, margins, ROE/ROA, cash, debt, debt/equity, current ratio, free cash flow, beta, 52-week range, short interest, institutional ownership |
| `WS` | Wall Street experts | What do the big desks conclude? | Consensus rating and 1-5 mean, bull/hold/bear head count, mean/high/low price targets and what each implies, recent upgrades & downgrades by firm, the disagreement note |
| `MA` | Macro & policy | Does the wider world favour owning this? | Market-wide headlines with **no company in them** — rates, inflation, tariffs, war, energy, regulation, policy — plus, per ticker, only its sector, industry, beta and market cap |

All five answer on the same 0-100 scale, so the average means something. An
agent whose own evidence is silent on a ticker is told to score it near 50 and
say so, rather than borrow conviction it hasn't earned.

**Why disjoint evidence, and not just disjoint questions.** Averaging opinions
only buys you something when the errors are uncorrelated. The design this
replaced showed *two models the same evidence* — including the same analyst
research — and averaged them, which measured how often two models read one
paragraph the same way and reported it as confirmation. So the fundamentals are
split down the middle (growth and the earnings record to `CO`, the multiples and
the balance sheet to `ST`), the two news feeds are separate providers, and only
the price appears in more than one payload, because a multiple without a price
isn't a number.

**The cost, stated plainly.** Each agent is *less informed* than the single
all-seeing model it replaced. `ST` scores a 160x P/E without knowing a product
just shipped; `CO` reads the product news without knowing what the market
already charges for it. Neither is wrong about its own dimension — but no single
agent's reasoning is a complete argument any more, so the detail card shows all
five instead of one tidy verdict. Reconciling them is the average's job, and the
weights are yours.

**Independence is structural, not advisory.** The prompts also say it (models
that sense they're seeing a partial picture start hedging toward an imagined
consensus, which is exactly the correlated error the split exists to avoid), but
the guarantee is that the evidence physically isn't in the context window.

#### The weighting system

Weights are **equal by default** — 20% each. Nothing about the five dimensions
says one deserves more say a priori, so any imbalance should be a choice you
made, not one the app made quietly.

Open **⚖️ Agent weights** in the panel and drag. The summary line shows each
agent's actual share of the score (`CO 33% · ME 17% · ST 33% · WS 17% · 1 off`),
which is the number that matters — a weight of 2 means nothing until you know
what the others are. Setting one to **0 silences that agent entirely**, which is
a legitimate thing to want: *"I don't care what Wall Street thinks."*

**Applying re-blends instantly and calls no model.** Every suggestion carries
the raw per-agent scores, so a new average is arithmetic over data already on
disk. You can dial `ST` to 2× and `MA` to 0 and watch the whole panel resettle —
scores, bands, meters, the wishlist filter — without spending a token or waiting
on a refresh. Weights are stored **per portfolio** (`ai_weights.json`), for the
same reason the risk toggle is: a speculative watchlist and a retirement account
deserve different priors.

To set a house default for new portfolios, use `AI_AGENT_WEIGHTS` (a saved
per-portfolio weight always wins over it):

```bash
AI_AGENT_WEIGHTS="statistics=2,macro=0.5"
AI_AGENT_WEIGHTS="expert=0"        # ignore Wall Street entirely
```

An agent that doesn't answer for a ticker — its call failed, or it had no
evidence — drops out and the remaining weights are **renormalised**, so a lone
survivor's score passes through unchanged rather than being dragged toward
neutral by the gap. The status line says when that happens, naming the missing
dimension, because a score blended from three agents is not the one from five.

#### Disagreement is the product

A consensus label hides the interesting part, and with five deliberately narrow
views the disagreement *is* the finding. Each detail card states it outright —
*"the agents are split — read all five"* — and each summary row shows the five
raw numbers next to the average, in fixed positions so the same slot always
means the same agent:

```
NVDA   54   Hold      CO 88 · ME 34 · ST 22 · WS 74 · MA 51     over 2 months
```

That row is a genuinely useful thing to see: the business is executing, the
street likes it, the chart broke, and the multiple is rich. A single model would
have written you one paragraph that landed on "hold" and buried which of those
four facts did the work.

Grading is by **spread** (highest minus lowest) rather than by comparing action
labels — on a continuous scale 50 vs. 55 is agreement that happens to straddle a
band edge, while 50 vs. 92 is a real split even though both are nominally "buy".
The thresholds widen with the number of agents (22/46 points at five, 15/35 at
two), because the spread of five draws is naturally wider than the spread of two
even when they describe the same thing; held at the old numbers, five narrow
views would have read as "split" on essentially every ticker — true in a useless
way.

#### What it reads

Everything the app already computes, plus fundamentals, analyst research and two
kinds of news — then split five ways as in the table above:

- all your holdings, share counts, average cost, and cost basis,
- a year of daily closes per ticker, **measured** into returns, drawdown, trend
  and volatility before it reaches a prompt (a model handed 250 closes and asked
  for a six-month return usually gets it roughly right, occasionally gets it
  badly wrong, and always spends tokens on it),
- realized gains from the sales log, including your own past exits per ticker,
- **company fundamentals per ticker** — trailing and forward P/E, PEG, P/S, P/B
  and EV/EBITDA, gross / operating / net margins, ROE and ROA, year-over-year
  revenue and earnings growth, cash, debt, debt/equity, current ratio, free and
  operating cash flow, beta, the 52-week range, 50- and 200-day averages, short
  interest, institutional ownership, the last four quarters of actual EPS with
  how far each beat or missed, and what the business actually sells,
- **the Wall Street research** — consensus, head count, target spread, and which
  firms moved lately,
- **company news** over the last 30 days (Yahoo Finance, falling back to Google
  News RSS),
- **macro news** over the last 14 days across six standing topics (rates,
  inflation/jobs, tariffs, market outlook, geopolitics/energy, policy), merged
  by interleaving so one busy topic — there is always something about the Fed —
  can't crowd the others out.

**Why the news matters.** The agents are never asked to search; they are handed
headlines. Two reasons. Free-tier Gemini *cannot* use Google Search grounding —
adding the `google_search` tool makes the request fail outright with
`429 RESOURCE_EXHAUSTED` while the same ungrounded request succeeds. And a model
asked about "recent news" with none supplied does not say "I don't know": it
invents specific, confident, wrong headlines. Real headlines are what keep the
suggestions honest.

#### What it suggests

For each stock: the confidence score and the band it implies (**buy more**,
**hold**, **sell part**, **sell all**), with a horizon of **one to three
months**, and — per agent — that agent's own headline, its 3-6 line argument
citing the figures it actually used, a concrete price trigger where its evidence
supports one, and the main risk to *its* call.

There is deliberately **no blended paragraph**. Five agents wrote five arguments
from five separate bodies of evidence; flattening them into one would invent a
synthesis nobody performed. The summary line is the headline of whichever agent
moved the score most (weight × distance from neutral), tagged with that agent so
it doesn't read as a consensus view.

**Risk toggle** — every refresh generates two sets, one **low-risk** and one
**high-risk**; the toggle switches between them instantly with no new API call.
Each agent applies the stance inside its own lens rather than as a
portfolio-level instruction it has no way to act on.

**Two guardrails on the model output**, both earning their keep on real runs:

- *Scale inversion.* Models sometimes read "confidence" as certainty in their
  own recommendation, returning `100` next to `"sell"` — the exact inverse of
  what the number means here. One flipped vote moves a blended score, so the
  action enum bounds the score: a score outside its action's range is pulled to
  the nearest edge, and the correction is logged.
- *Invented tickers.* An agent answering for `HIMSS` when you hold `HIMS` would
  add a phantom position to the panel, so rows are anchored to your actual
  holdings and unknown symbols are dropped.

#### The UI

- A **summary** at the very top: one bullet per stock — ticker, the blended
  score, the band it falls in, then **the five numbers behind it** in smaller
  type (`CO 88 · ME 34 · ST 22 · WS 74 · MA 51`), and over how many months —
  plus a meter and the leading agent's one-line rationale. The five always
  appear in roster order, so a reader learns where to look; an agent with no
  score for that ticker shows an em dash, a zero-weighted one is struck through
  rather than hidden (it still has a view, it just isn't counted), and hovering
  any number names the agent, its band and its weight.
- Below the list, **one portfolio note per agent**, each tagged — kept separate
  rather than merged, because they were written from different evidence.
- Every row carries its own **Details →** button, which switches to a details
  tab showing *that stock only*: the consensus badge, then **all five agents
  side by side** — score, meter, weight, headline, reasoning, trigger and risk —
  followed by the raw Wall Street research the `WS` agent read. With two dozen
  holdings a single button at the bottom of the list meant scrolling past
  everything to open anything, so the affordance lives on each row instead.
  **Back to summary** returns and clears the filter.
- The status line reads e.g. *"5 independent agents — 9/23 agreed · 8 split —
  avg score 65"*, and turns amber with the missing dimension named when an agent
  drops out.

<a id="wishlist-buys"></a>
**Wishlist buys** — under the holdings calls sits a second, deliberately
minimal section: the agents' read on whether now is a good time to *enter* the
stocks on your wishlist.

- Every wishlist ticker is scored by all five agents on each refresh, from the
  same slices but a separate framing — the question is entry timing, not whether
  to keep holding. `confidence` reads as 100 = buy now, 50 = wait, 0 = avoid.
  The `ME` agent has no position to look at, so it judges the price-history
  pattern alone.
- **Only genuine buys are shown.** A name survives to the UI only if the blended
  call is `buy` *and* scores at least `_WISHLIST_MIN_CONFIDENCE` (55). Everything
  in the wait band is dropped, and when nothing clears the bar the section is
  hidden outright — it doesn't render as an empty box or a wall of "not yet".
  Survivors are sorted by conviction, highest first.
- The full blended list is still kept server-side as `candidates`, because the
  weights are adjustable: raise `ST` and a name that was just under the line has
  to be able to come back, which it can't if the losers were discarded at
  generation time.
- The panel's wishlist counters (`avg_confidence`, `agreement`) describe the
  kept buys, not the whole watchlist — an average over names nobody suggested
  buying would be noise.

#### Cost — watch your daily quota

A refresh fans out `5 agents × 2 risk profiles × {holdings, wishlist}` = up to
**20 calls**, against 8 before. Each prompt is much smaller (one slice, not the
whole picture), but the *call count* is what free tiers meter. Against a tier
that grants a daily allowance per model (`gemini-3.6-flash` gets 20
requests/day), one refresh can exhaust it.

The panel keeps working — four agents still produce a score — but it stops being
the five-way average it's built around, which is easy to miss. So:

- The status line says it outright, in amber: *"only 4 of 5 agents scored — no
  MA view in these numbers"*.
- **Agents fall back across models.** With more than one provider configured, an
  agent whose assigned model is rate-limited retries on the next one before
  giving up. Losing an agent costs a whole dimension of the score, which is far
  worse than one extra call against a second provider's quota.
- A daily-quota 429 is **not** retried. The provider still suggests a retry
  delay for one (Gemini says 9s), but a daily allowance doesn't return before it
  resets, so retrying only holds a worker open and stalls the refresh. The quota
  id from the error's `details` is appended to the message so
  `_is_transient()` can tell a daily cap from a per-minute burst.
- A per-minute 429 *is* retried, and waits the delay the provider actually
  returned rather than a blind 2s — doubling from 2s gave up after ~6s while the
  window still had ~37s to run, which cost a model for the whole refresh.
- The fan-out is capped at `models × 2` concurrent calls, with holdings queued
  first, so the burst can't knock out the calls the panel is primarily built
  around.

**Configure more than one provider.** Unlike before, a second and third model
buy you nothing in *opinion* — the consensus is between agents now — but they
buy parallelism and quota headroom, which is what actually keeps all five agents
alive. Set `GROQ_API_KEY` alongside your Gemini key (see `.env`); agents are
spread round-robin, so three providers means roughly seven calls each.

**Refresh** — regenerates automatically **once per trading day, at the opening
bell** (9:30 AM ET), plus once on startup if nothing is cached. You can force one
any time with the button; changing weights does *not* trigger one. The latest
result is cached (and persisted per portfolio) so it shows instantly and survives
restarts, and the panel shows when the next refresh is due next to the last one.

The trigger is **the bell, not an elapsed timer**, which matters in two ways. It
**catches up**: a laptop asleep at 9:30 refreshes as soon as it wakes rather than
skipping the day, so you never open the app to yesterday's numbers with no way to
know they're stale. And it **can't double-fire**: restarting the app ten times
after the open regenerates nothing, because the cached result is already newer
than today's bell. Catch-up means an occasional refresh outside market hours — at
most one, and only when a day was genuinely missed.

`GET /api/ai` returns:

```jsonc
{
  "configured": true,          // whether a model is configured (Ollama: always true)
  "models": ["gemini:gemini-3.6-flash", "groq:llama-3.3-70b-versatile"],
  // The roster, so the UI renders the weight controls and the per-agent
  // breakdown generically rather than hardcoding five.
  "agents": [
    { "key": "company_perspective", "name": "Company perspective",
      "short": "CO", "focus": "The business itself: news, what it sells …" },
    { "key": "personal", "name": "My position & history", "short": "ME", … },
    { "key": "statistics", "name": "Raw statistics", "short": "ST", … },
    { "key": "expert", "name": "Wall Street experts", "short": "WS", … },
    { "key": "macro", "name": "Macro & policy", "short": "MA", … }
  ],
  "agent_weights": { "company_perspective": 1.0, "personal": 1.0,
                     "statistics": 2.0, "expert": 0.0, "macro": 1.0 },
  "default_agent_weights": { … },   // what Reset restores
  "news_configured": true,     // the keyless sources make this true by default
  "news_sources": "Yahoo → GoogleNewsRSS",
  "analysts_configured": true,      // the WS agent has something to read
  "analyst_source": "Yahoo Finance analyst ratings",
  "fundamentals_configured": true,  // the CO and ST agents have figures
  "fundamentals_source": "Yahoo Finance fundamentals",
  "macro_configured": true,         // the MA agent has a tape
  "macro_source": "Google News RSS · 6 macro topics",
  "refreshing": false,         // true while a regeneration is in flight
  "market_open": true,
  "refresh_schedule": "daily at market open",
  "next_refresh": "2026-08-10T13:30:00+00:00",   // the next opening bell, UTC
  "generated_at": "2026-08-07T14:00:00+00:00",
  "model_errors": null,        // non-null if an agent failed but others answered
  "risk_profiles": {
    "low": {
      // One note per agent on the whole list, not a merged summary.
      "portfolio_notes": [
        { "key": "macro", "name": "Macro & policy", "short": "MA", "note": "…" }
      ],
      // Which agents answered, and which model ran each.
      "agents": [ { "key": "statistics", "name": "Raw statistics",
                    "short": "ST", "model": "groq:llama-3.3-70b-versatile" } ],
      "models": ["gemini:gemini-3.6-flash", "groq:llama-3.3-70b-versatile"],
      "avg_confidence": 61.4,
      "agreement": { "agreed": 2, "mixed": 1, "split": 1, "total": 4 },
      "suggestions": [
        {
          "ticker": "AAPL",
          "confidence": 71.1,              // the weighted average — 50 = hold
          "action": "buy",                 // the band it falls in
          "confidence_label": "Buy",
          "horizon_months": 2,
          "consensus": "agree",            // agree | mixed | split | single
          "headline": "…",                 // from the agent that moved it most
          "headline_from": "company_perspective",
          // The whole argument: one entry per agent, each with its own case.
          // This is what makes reweighting free — the raw scores are right here.
          "sources": [
            { "kind": "agent", "key": "company_perspective",
              "name": "Company perspective", "short": "CO", "focus": "…",
              "model": "gemini:gemini-3.6-flash",
              "confidence": 88.0, "action": "buy", "label": "Strong buy",
              "weight": 1.0, "horizon_months": 2,
              "detail": "…",          // this agent's headline
              "reasoning": "…", "price_trigger": "…", "risks": "…" },
            { "kind": "agent", "key": "statistics", "short": "ST",
              "confidence": 22.0, "action": "trim", "label": "Trim",
              "weight": 2.0, … }
          ],
          // The research the WS agent read, kept so the card can show it. It
          // carries no score of its own and no other agent saw it.
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
      ],
      // Wishlist buys for this risk profile. Same shape, but `suggestions` is
      // pre-filtered to genuine buys — an empty list is the normal case and
      // tells the UI to hide the section. `candidates` keeps the unfiltered
      // blend so a weight change can bring a name back.
      "wishlist": {
        "portfolio_notes": [ … ],
        "agents": [ … ],
        "avg_confidence": 72.5,
        "agreement": { "agreed": 1, "mixed": 0, "split": 0, "total": 1 },
        "candidates": [ { "ticker": "DELL", "confidence": 72.5, … },
                        { "ticker": "AMD",  "confidence": 48.0, … } ],
        "suggestions": [ { "ticker": "DELL", "confidence": 72.5,
                           "action": "buy", … } ]
      }
    },
    "high": { … }
  },
  "error": null
}
```

`POST /api/ai/weights` takes `{"weights": {...}}`, clamps each to 0-5, fills in
any it wasn't given, persists them to the active portfolio, re-blends the cached
scores, and returns **the same payload as `GET /api/ai`** — so the UI applies the
result directly instead of polling. Weights that are all zero fall back to equal,
since silencing every agent is never what someone means.

**Backwards compatibility.** Suggestions saved before the five-agent split carry
`kind: "model"` sources and a single top-level `reasoning`. There is nothing to
reweight in those, so they are returned untouched rather than silently rescored
against weights that never applied to them, and the UI still renders them the
old way. Suggestions older still carry `horizon_days` instead of
`horizon_months`; both backend and UI read them.

> **Not financial advice.** Suggestions are generated by a model and can be
> wrong — verify before you trade.

### In the News

The left-hand column, and the only panel that starts from **outside your list**.
Everything else answers a question about stocks you already named; this one
answers "what is everyone talking about that I haven't looked at?"

```
GET  /api/discover           -> the three picks, both risk profiles
POST /api/discover/refresh   -> regenerate in the background ({"started": bool})
```

**How a pick is found.** Three lanes are read concurrently — retail chatter, the
general financial press, and the WSJ. Candidate tickers are pulled out of the
headlines (`$NVDA` cashtags, "(NASDAQ: AMD)", "MU stock", and capitalised
company names) and each is then **resolved against Yahoo**: is this a real
US-listed equity? That second step is the one that matters. Headlines are full
of things shaped exactly like tickers that aren't — CNBC, FOREX, GDP, the WSJ
itself — and an extractor without it happily ranks the news network above the
news.

**How the lanes are combined.** A ticker's score in a lane is its mentions as a
share of that lane's busiest name, so "loudest on Reddit" and "loudest in the
WSJ" are worth the same — otherwise whichever feed happened to return the most
headlines would decide the board on volume alone. Appearing in more than one
lane then earns a bonus. A lane also has to be *carrying* a conversation before
its winner counts for full marks, or a single WSJ headline on a slow day
outranks a name a hundred people are posting about.

**Then it is scored like everything else.** The picks go through
`AIAdvisorService.score_context` — the same five agents, the same per-portfolio
weights, the same 0-100 scale, at both risk settings. The risk toggle in the AI
Advisor column drives this panel too. Each pick shows three things, in this
order:

1. **Why it's being talked about** — which lanes, how loudly, and the real
   headlines with links.
2. **About the company** — sector, industry, market cap, growth, margin, P/E,
   beta, the 52-week range, and what the business actually does. A confidence
   score on a ticker you've never heard of means nothing until you know this,
   which is why it comes before the number.
3. **What the agents make of it** — the blended score and band, the five
   per-agent numbers, and a **Why? →** button onto the identical five-argument
   breakdown the AI Advisor shows for a holding.

Anything already in your holdings or wishlist is excluded, so **＋ Wishlist** on
a pick both saves it and takes it out of tomorrow's board.

**Cost.** A refresh is five agents × two risk profiles = up to ten model calls,
on top of the advisor's twenty. It shares the advisor's once-a-day opening-bell
schedule but waits for the advisor to finish first, to keep the burst off a free
tier's per-minute quota.

**When a lane can't answer.** Reddit returns 403 to some networks whatever
User-Agent you send; the retail lane then falls through to StockTwits, and the
header reports which source actually spoke along with how many headlines each
lane returned. A blocked feed shows as a `0` you can see rather than a lane that
silently contributed nothing.

### AI Actions

Sits directly under the dashboard, above the Buy / Add form it prefills, so the
page reads: **what am I worth → what should I do → do it.**

```
GET  /api/actions            -> the sized plan, both risk profiles
```

Every other AI panel answers *how strongly do the agents feel about this
stock?* and stops there. A 78/100 is a view, not an instruction: it doesn't say
whether to act today, and it certainly doesn't say **how many shares**. This
panel is the last step — it turns the scores already on screen into orders.

**The money is yours if you say so.** The plan is sized against
[**Available to trade**](#available-to-trade) — the balance you enter on the
dashboard. Then the share counts are orders you could actually place, and
buying one draws the balance down so the next plan is sized against what's
left.

Leave that section empty and the app makes no assumption about your brokerage
balance: it falls back to a flat **$10,000**, labels the chip
*"(stand-in)"*, and you read the *proportions* as the answer and the dollars as
a scale you multiply. Change the fallback by passing `budget=` to
`AiActionsService` in `run.py`. The payload says which was used:

```jsonc
"budget": 2500.0,
"budget_source": "available"     // or "placeholder" while the section is vacant
```

A balance of exactly **$0** is neither of those — it is a real instruction. The
plan sizes no buys at all (`"no_cash": true` on each plan), says so in place of
the cash bar, and keeps showing sells, because selling is how you get cash when
you have none.

**No model is called.** The plan is re-derived per request from the cached
suggestions, exactly like the weight sliders — instant, free, and never fresher
than the columns it reads from. Move a weight and the allocation moves with it;
that is most of the reason the sliders are interesting.

**Where the candidates come from** (per risk profile — it follows the AI
Advisor's toggle):

| | |
|---|---|
| **Buy** | advisor holdings scored `buy`, advisor **wishlist** buys, and **In the News** picks scored `buy`. Deduped by ticker (highest score wins), ranked by score, capped at 5. |
| **Sell** | positions you actually hold that scored `trim` or `sell`, worst first, capped at 3. A `trim` on a stock you don't own is "don't enter" — not an order, so it produces no row. |

Those two, and nothing else. A name the agents landed on neutral doesn't appear:
"wait" is the absence of an action, and a list of things *not* to do buries the
two or three that need doing. Those names are still on screen — scored, in the
AI Advisor column — where reading them is a deliberate act rather than
something you scroll past to reach the orders.

**Cash pays, so nothing has to be spent.** Uninvested money sits at
**4.25% APR** (`CASH_APR_PCT`, defined once in `ai_agents.py`), risk-free and
liquid. That single number does two jobs: it is written into **every agent's
prompt**, so a buy has to clear "beats a guaranteed ~1.06% over a quarter"
rather than merely "goes up", and it prices what the plan holds back, so
leftover money is reported as a position with a yield rather than an allocator
that ran out of ideas. The line above the list says it every time — *Deploying
$3,753 (38%) · Keeping $6,247 in cash at 4.25% APR — earning ~$265/yr* — and on
a day nothing clears the bar, that line is the whole plan.

**Sizing.** Each buy may claim at most an equal slice of the balance, and takes
only the part of it its conviction has earned:

```
slice    = balance / 5                  ($2,000 of a $10,000 balance)
claim    = (confidence − 55) / (85 − 55)       capped at 1.0
deployed = slice × claim
```

An 85+ takes its whole slice; a 60, barely over the buy floor, takes a sixth of
it and leaves the rest earning 4.25%. Everything unclaimed — by weak scores, by
there being two buys instead of five, or by there being none — stays in cash. In
practice a low-risk day on a 23-stock portfolio deploys around a third of the
balance and a high-risk one about half, which is the point: the plan is allowed
to disagree with the idea that money must be working.

Sells are sized off the position you hold, on the mirror scale — a quarter of it
just under the hold band, all of it once the score reaches the low teens. A name
with no live quote is listed **unpriced** and its dollars stay in cash rather
than being guessed at.

The score is a conviction reading, not a forecast return, so none of this claims
to compare an expected gain against 4.25% arithmetically. The rate sets the bar
and prices the wait; the ramp is the honest way to spend against a number that
isn't a return.

**Tap a row** for the same five-agent breakdown the AI Advisor shows, plus the
price the order was sized at, what you already hold, and a button that prefills
the Buy or Sell form with exactly those numbers. Nothing is ever submitted for
you — a model's suggestion should still take a deliberate click to become a
trade.

## Run it

Requires only Python 3 (no third-party packages):

```bash
python3 run.py
```

Then open http://127.0.0.1:8000 in your browser.

**AI advisor (optional).** `AI_PROVIDER` is a comma-separated list of **models**,
not of opinions — the five agents are the opinions, and they are handed out
round-robin across whatever has a key. Anything without a key is skipped, so one
key is enough to start.

```bash
AI_PROVIDER=gemini,groq,gemini-b     # the default
```

With only a Gemini key that resolves to `gemini` + `gemini-b`, two *different*
Gemini models sharing the load; add `GROQ_API_KEY` and Groq slots in as a third,
on a separate vendor's quota. More models buy **parallelism and quota headroom**
rather than a second vote — and since an agent that runs dry costs you a whole
dimension of the score, headroom is worth having. Up to five are used (one per
agent); beyond that a model would sit idle.

Neither of the keyless evidence sources — the analyst consensus and the macro
news tape — needs any configuration; they come from the same Yahoo session the
price data uses and from Google News RSS.

Agent weights are edited live in the UI and stored per portfolio. To set the
default a *new* portfolio starts from, use `AI_AGENT_WEIGHTS`:

```bash
AI_AGENT_WEIGHTS="statistics=2,macro=0.5"   # keys: company_perspective,
AI_AGENT_WEIGHTS="expert=0"                 # personal, statistics, expert, macro
```

Sizing note: a refresh is up to **20 calls** (5 agents × 2 risk profiles ×
holdings/wishlist), and it runs **once a trading day**, so that is roughly
**20 calls and ~30k tokens a day** — arriving in one burst at the open. Each
prompt carries one slice rather than the whole picture, so they are individually
small, but the *call count* is what free tiers meter and a per-model daily cap of
20 is exactly one refresh. **Configure two or three providers** so the
round-robin has somewhere to go; one key alone cannot carry all five agents.

*Provider 1 — Google Gemini API (default, free).* No credit card:

```bash
# 1. Get a key: https://aistudio.google.com/app/apikey
export GEMINI_API_KEY="AIza..."
# Optional: GEMINI_MODEL   (default gemini-3.6-flash)
#           GEMINI_MODEL_B (default gemini-3.5-flash-lite — a second model,
#                           so the agents have two lanes on one key)
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

*Provider 2 — Groq API (free, recommended second lane).* Free key, no credit
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

Everything degrades gracefully, one layer at a time: an agent whose model fails
retries on another provider, then drops out with the remaining weights
renormalised and the missing dimension named in the status line; every agent
failing leaves the last good suggestions on screen; and losing an evidence
source costs exactly the agents that read it — a ratings outage silences `WS`,
a macro-feed outage silences `MA`, and the rest score on as normal. Every client
talks to its API over the standard library only — no SDK, matching the rest of
the app.

Live prices, history, and earnings dates are fetched from Yahoo Finance, so an
internet connection is needed for those. If the network (or Yahoo) is
unavailable, the app still runs — live fields just show "—" until it recovers.

Holdings, wishlist, sales log, AI suggestions, the agent weights, and the
available-to-trade balance are stored locally, one set per portfolio, under
`data/portfolios/<id>/` (`portfolio.json`, `wishlist.json`, `sales.json`,
`ai_suggestions.json`, `ai_weights.json`, `available.json`) — separate tables.
`available.json` is simply absent until you enter a balance, which is how the
app tells *vacant* from *$0*. Which portfolios exist
and which one is active live in `data/portfolios/index.json`. Older single-
portfolio installs (with the files directly under `data/`) are migrated into a
first **"My Portfolio"** automatically on first launch, so no history is lost.

### Portfolios

Keep several separate portfolios and switch between them from the dropdown at the
top center of the page. Each is its own workspace — holdings, wishlist, sales,
and AI suggestions never mix — and every action is saved immediately. The app
remembers the portfolio you were last viewing and reopens on it. Each portfolio
also remembers **its own AI-advisor risk toggle** (low / high), **its own agent
weights**, and **its own available-to-trade balance**, so switching portfolios
restores all three.

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
