# stock-agent

A personal stock assistant, an agent that updates the positions and news of your
holding stocks and provides financial suggestions daily.

This is the base scaffold: a web UI to **add** and **view** stocks, backed by a
local storage system, with a clean **API layer** in between. The UI and storage
never talk to each other directly — everything goes through the API — so either
side can be swapped or extended independently.

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

| Method | Path          | Purpose                        | Body                              |
| ------ | ------------- | ------------------------------ | --------------------------------- |
| GET    | `/api/stocks` | List all positions (read only) | —                                 |
| POST   | `/api/stocks` | Add / accumulate (write only)  | `{"ticker","shares","avg_price"}` |

When a ticker already exists, POST accumulates the shares and recomputes the
weighted-average price:

```
new_avg = (old_shares * old_avg + added_shares * added_price) / (old_shares + added_shares)
```

## Run it

Requires only Python 3 (no third-party packages):

```bash
python3 run.py
```

Then open http://127.0.0.1:8000 in your browser.

Data is stored locally in `data/portfolio.json`.
