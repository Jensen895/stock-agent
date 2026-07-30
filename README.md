# stock-agent

A personal stock assistant, an agent that updates the positions and news of your
holding stocks and provides financial suggestions daily.

This is the base scaffold: a web UI to **buy**, **sell**, **delete**, and
**view** stocks — plus a separate **wishlist** of tickers you plan to buy —
backed by a local storage system, with a clean **API layer** in between. The UI
and storage never talk to each other directly — everything goes through the API
— so either side can be swapped or extended independently.

## Architecture

```
   UI (frontend/)              API layer (backend/server.py)         Storage (backend/storage/)
  ┌──────────────┐   HTTP     ┌───────────────────────────┐        ┌────────────────────────┐
  │ index.html   │ ───────►   │ GET  /api/stocks  (read)  │  ────► │ StorageBackend         │
  │ app.js       │            │ POST /api/stocks  (write) │        │   └─ JSONStorage       │
  └──────────────┘   ◄─────── │  ▼ PortfolioService       │  ◄──── │      (data/*.json)     │
                     JSON     │    (business logic)       │        └────────────────────────┘
                              └───────────────────────────┘
```

- **UI layer** (`frontend/`) — a plain HTML/CSS/JS page. Speaks only HTTP.
- **API layer** (`backend/server.py`) — the only bridge. Exposes a REST API.
- **Business logic** (`backend/service.py`) — accumulation + weighted-average
  price. Independent of UI and storage.
- **Storage layer** (`backend/storage/`) — an abstract `StorageBackend`
  interface plus a local `JSONStorage` implementation.

### Why it's flexible / reusable

- **New UI?** Build anything that calls `GET/POST /api/stocks` (CLI, mobile,
  another web app). No backend changes.
- **New storage?** Implement `StorageBackend` (e.g. SQLite, Postgres, cloud)
  and change one line in `run.py`. Nothing else changes.

## API

### Holdings

| Method | Path               | Purpose                          | Body                              |
| ------ | ------------------ | -------------------------------- | --------------------------------- |
| GET    | `/api/stocks`      | List all positions (read only)   | —                                 |
| POST   | `/api/stocks`      | Buy / accumulate a position      | `{"ticker","shares","avg_price"}` |
| POST   | `/api/stocks/sell` | Sell part/all of a position      | `{"ticker","shares","price"}`     |
| DELETE | `/api/stocks`      | Delete an entire position        | `{"ticker"}`                      |

### Dashboard

| Method | Path           | Purpose                                  | Body |
| ------ | -------------- | ---------------------------------------- | ---- |
| GET    | `/api/summary` | Total worth + realized/unrealized gains  | —    |

`/api/summary` returns:

```jsonc
{
  "total_worth": 897.0,               // sum of shares * avg_price across holdings
  "realized":   { "1d": 0, "1w": 0, "1m": 0, "ytd": 0, "1y": 0 },
  "unrealized": {                     // PLACEHOLDER data (no live prices yet)
    "1d": { "value": 82.67, "series": [ { "t": "…", "v": 12.3 }, … ] },
    …
  },
  "intervals":  [ { "key": "1d", "label": "1D" }, … ]
}
```

- **Total stock worth** — the sum of every holding's cost basis
  (`shares * avg_price`). Shown as the largest text in the UI, in green.
- **Realized gains** — summed from the sales log. Each sell records
  `(sale_price - avg_price) * shares` with a UTC timestamp; the summary buckets
  those into rolling windows (1D / 1W / 1M / YTD / 1Y). Green when positive, red
  when negative. The UI remembers the selected window across restarts.
- **Unrealized gains** — **placeholder** for now: the live-price logic isn't
  implemented, so the value and its line graph (time × USD, per window) are
  deterministic dummy data. Same green/red coloring and remembered window.

### Wishlist

| Method | Path            | Purpose                            | Body         |
| ------ | --------------- | ---------------------------------- | ------------ |
| GET    | `/api/wishlist` | List wishlist tickers (read only)  | —            |
| POST   | `/api/wishlist` | Add a ticker to the wishlist       | `{"ticker"}` |
| DELETE | `/api/wishlist` | Remove a ticker from the wishlist  | `{"ticker"}` |

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
(ticker only, no shares or price). Kept apart from holdings so it can later feed
the AI's buy suggestions.

## Run it

Requires only Python 3 (no third-party packages):

```bash
python3 run.py
```

Then open http://127.0.0.1:8000 in your browser.

Holdings are stored locally in `data/portfolio.json`, the wishlist in
`data/wishlist.json`, and the sales log in `data/sales.json` — separate tables.
