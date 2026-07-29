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
originally cost); selling the full position removes it.

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

Holdings are stored locally in `data/portfolio.json` and the wishlist in
`data/wishlist.json` — two separate tables.
