"""The paper book: turning one agent's plan into positions, and marking it.

``AiActionsService`` already answers "what should this portfolio do today, in
shares, against the cash it has". This module is the part the app never needed,
because the app stops at the suggestion and lets a human place the order:
something has to actually *do* it, and then say what the book is worth.

Three jobs, in the order the day runs them:

    policy      A plan is a proposal. A competitor's discipline — how big one
                name may get, how much cash it refuses to deploy, whether it
                acts on a split vote at all — is applied here, after the sizing
                and before the trade. Every rejection is recorded with its
                reason, because "C did nothing today" and "C wanted to buy NVDA
                and its own position cap stopped it" are different days and the
                report should not read the same on both.

    execution   Sells first, then buys against the proceeds. See ``execute``.

    the mark    Cash plus every position at the last close. This is the number
                the contest is scored on and the only one that matters at the
                end of the thirty sessions.

Sales credit cash here, and only here
-------------------------------------
``PortfolioService.sell_stock`` deliberately does *not* return the proceeds to
"available to trade": in the app that figure is a note about a real brokerage
account the code cannot see, and inventing a credit would turn a note into a
false ledger. In a paper contest the opposite is true — the account *is* the
JSON file, there is nowhere else for the money to go, and an agent that could
sell without getting paid could never rotate out of a loser into a winner. So
the book credits the proceeds itself, immediately after the sale, and that
asymmetry is confined to this module.
"""

from backend.service import ValidationError

# Below this the position is treated as closed. Matches the epsilon the
# portfolio service uses when deciding a sale emptied a holding.
_DUST_SHARES = 1e-6


class TradableCash:
    """The slice of the balance a competitor is willing to deploy today.

    Handed to ``AiActionsService`` in place of the real cash service so the
    plan is *sized* against the deployable figure rather than sized against
    everything and then cut down afterwards. Those two produce different plans,
    not the same plan trimmed: the allocator divides the budget by the number
    of buys, so a competitor that holds back a fifth of the book should be
    taking smaller positions in the names it does buy, not the same positions
    in fewer of them.

    The agents are still told the *whole* balance — that is the real constraint
    on what they are choosing between, and the cash floor is described to them
    in the mandate instead.
    """

    def __init__(self, amount: float):
        self.amount = max(0.0, float(amount))

    def get(self):
        return round(self.amount, 2)


class Book:
    """One competitor's positions, cash and trading, for one session.

    Thin by design: it composes the same ``PortfolioService`` /
    ``AvailableCashService`` / ``MarketService`` the app uses, already pointed
    at this competitor's workspace by the manager. Nothing here caches — the
    runner switches workspaces underneath these services, so a stale read would
    be a read of another agent's book.
    """

    def __init__(self, competitor, portfolio, available, market, wishlist,
                 universe=()):
        self.competitor = competitor
        self.portfolio = portfolio
        self.available = available
        self.market = market
        self.wishlist = wishlist
        # Names that go back on the watchlist when a position is closed, so a
        # competitor that sells out of something today can buy it back next
        # week. Without this an exit is permanent and the universe ratchets
        # down towards nothing over thirty sessions.
        self.universe = set(universe)

    # --- the mark ---------------------------------------------------------

    def mark(self) -> dict:
        """What the book is worth right now: cash + positions at last price.

        ``priced`` is false when a quote was missing for at least one holding.
        The position is then carried at cost, which is the least wrong of the
        available options — dropping it would understate the book and calling
        it zero would be a lie — and the flag says so rather than letting a
        Yahoo hiccup look like a loss.
        """
        cash = self.available.get() or 0.0
        rows, priced = [], True
        positions_value = 0.0
        cost_basis = 0.0
        for row in self.market.holdings_view():
            value = row.get("market_value")
            if value is None:
                priced = False
                value = row.get("cost_basis") or 0.0
            positions_value += value
            cost_basis += row.get("cost_basis") or 0.0
            rows.append({
                "ticker": row["ticker"],
                "name": row.get("name"),
                "shares": row["shares"],
                "avg_price": row["avg_price"],
                "price": row.get("price"),
                "cost_basis": row.get("cost_basis"),
                "market_value": round(value, 2),
                "day_pct": ((row.get("today") or {}).get("pct")),
                "total_value": ((row.get("total") or {}).get("value")),
                "total_pct": ((row.get("total") or {}).get("pct")),
                "quote_ok": bool(row.get("quote_ok")),
            })
        rows.sort(key=lambda r: -(r["market_value"] or 0))
        return {
            "cash": round(cash, 2),
            "positions_value": round(positions_value, 2),
            "cost_basis": round(cost_basis, 2),
            "equity": round(cash + positions_value, 2),
            "positions": rows,
            "priced": priced,
        }

    # --- policy -----------------------------------------------------------

    def tradable_cash(self, equity: float, cash: float) -> float:
        """Cash minus this competitor's floor, which is a share of the *book*.

        A floor set against equity rather than against cash means it grows with
        the book and is not quietly satisfied by having spent everything: a
        competitor that keeps 20% in cash and is fully invested is over its
        limit and should be deploying nothing, which is what this returns.
        """
        floor = equity * self.competitor.cash_floor_pct / 100.0
        return max(0.0, cash - floor)

    def screen_buys(self, rows: list, equity: float, tradable: float,
                    positions: dict):
        """Apply the competitor's discipline to a sized buy list.

        Returns ``(accepted, rejected)``. An accepted row carries the shares it
        will actually be filled for, which may be fewer than the plan asked for
        when the position cap bit; a rejected one carries ``why``, in words a
        report can print.
        """
        accepted, rejected, spent = [], [], 0.0
        cap = equity * self.competitor.max_position_pct / 100.0

        for row in rows:
            ticker = row["ticker"]
            price = row.get("price")
            shares = row.get("shares")

            if not price or not shares:
                rejected.append({**row, "why": "no live quote to size against"})
                continue

            consensus = (row.get("suggestion") or {}).get("consensus")
            required = self.competitor.consensus_required
            if required and consensus not in required:
                rejected.append({
                    **row,
                    "why": f"agents were {consensus}; this book only buys on "
                           f"{' or '.join(required)}",
                })
                continue

            cost = shares * price

            # The position cap counts what is already held, so adding to a
            # winner is limited by how big it has become rather than by how
            # much is being added today.
            held_value = (positions.get(ticker) or {}).get("market_value") or 0.0
            room = cap - held_value
            if room <= 0:
                rejected.append({
                    **row,
                    "why": f"already {held_value / equity * 100:.0f}% of the book, "
                           f"at the {self.competitor.max_position_pct:g}% cap",
                })
                continue
            cost = min(cost, room)

            # And the money has to exist after everything bought before it.
            room_in_cash = tradable - spent
            if room_in_cash <= 0:
                rejected.append({**row, "why": "no deployable cash left today"})
                continue
            cost = min(cost, room_in_cash)

            if cost < self.competitor.min_trade:
                rejected.append({
                    **row,
                    "why": f"${cost:,.2f} is under this book's "
                           f"${self.competitor.min_trade:,.0f} minimum ticket",
                })
                continue

            filled = round(cost / price, 6)
            accepted.append({**row, "shares": filled, "cost": round(cost, 2)})
            spent += cost

        return accepted, rejected

    def screen_sells(self, rows: list, positions: dict):
        """Apply the discipline to a sized sell list. Same shape as buys.

        The minimum ticket applies to sells too, with one exception: an order
        that closes the position entirely always goes through. Leaving four
        dollars of a name behind to respect a minimum would be the rule
        defeating its own purpose.
        """
        accepted, rejected = [], []
        for row in rows:
            ticker = row["ticker"]
            price = row.get("price")
            shares = row.get("shares")
            held = (positions.get(ticker) or {}).get("shares") or 0.0

            if not price or not shares:
                rejected.append({**row, "why": "no live quote to size against"})
                continue
            shares = min(shares, held)
            if shares <= _DUST_SHARES:
                rejected.append({**row, "why": "nothing left to sell"})
                continue

            proceeds = shares * price
            closing = (held - shares) <= _DUST_SHARES
            if proceeds < self.competitor.min_trade and not closing:
                rejected.append({
                    **row,
                    "why": f"${proceeds:,.2f} is under this book's "
                           f"${self.competitor.min_trade:,.0f} minimum ticket",
                })
                continue

            accepted.append({
                **row,
                "shares": round(shares, 6),
                "proceeds": round(proceeds, 2),
                "sell_all": closing,
            })
        return accepted, rejected

    # --- execution --------------------------------------------------------

    def sell(self, row: dict) -> dict:
        """Book one sale and credit the proceeds. Returns the trade record."""
        result = self.portfolio.sell_stock(row["ticker"], row["shares"],
                                           row["price"])
        self._credit(result["proceeds"])
        if result["sold_out"] and row["ticker"] in self.universe:
            # Back on the watchlist so it stays a candidate — see __init__.
            try:
                self.wishlist.add(row["ticker"])
            except ValidationError:
                pass
        return self._trade("sell", row, result)

    def buy(self, row: dict) -> dict:
        """Book one purchase. Cash is drawn down by ``PortfolioService``."""
        self.portfolio.add_stock(row["ticker"], row["shares"], row["price"])
        # It is a holding now, not a name being watched. Removing it keeps the
        # advisor from scoring the same ticker twice each morning, once on each
        # side of the book, for two prompts' worth of tokens and one answer.
        try:
            self.wishlist.remove(row["ticker"])
        except ValidationError:
            pass
        return self._trade("buy", row, None)

    # --- internals --------------------------------------------------------

    def _credit(self, proceeds) -> None:
        """Put the money from a sale back into the balance. See the module note."""
        current = self.available.get()
        self.available.set(round((current or 0.0) + float(proceeds or 0.0), 2))

    def _trade(self, side: str, row: dict, result) -> dict:
        """One executed order, with the argument that produced it.

        The five agents' notes ride along because this is the only moment they
        can be captured: ``ai_suggestions.json`` keeps the latest scores only,
        so by the next close the reasoning behind today's trade has been
        overwritten by tomorrow's.
        """
        suggestion = row.get("suggestion") or {}
        record = {
            "side": side,
            "ticker": row["ticker"],
            "name": row.get("name"),
            "shares": row["shares"],
            "price": row["price"],
            "value": round(row["shares"] * row["price"], 2),
            "origin": row.get("origin"),
            "origin_label": row.get("origin_label"),
            "confidence": row.get("confidence"),
            "confidence_label": row.get("confidence_label"),
            "consensus": suggestion.get("consensus"),
            "horizon_months": suggestion.get("horizon_months"),
            "headline": suggestion.get("headline"),
            "headline_from": suggestion.get("headline_from"),
            "owned_shares_before": row.get("owned_shares"),
            "agents": [
                {
                    "key": s.get("key"),
                    "name": s.get("name"),
                    "short": s.get("short"),
                    "weight": s.get("weight"),
                    "confidence": s.get("confidence"),
                    "action": s.get("action"),
                    "detail": s.get("detail"),
                    "reasoning": s.get("reasoning"),
                    "risks": s.get("risks"),
                }
                for s in (suggestion.get("sources") or [])
            ],
        }
        if side == "buy":
            record["claim_pct"] = row.get("claim_pct")
            record["slice_size"] = row.get("slice_size")
        else:
            record["sell_fraction"] = row.get("sell_fraction")
            record["sold_out"] = bool((result or {}).get("sold_out"))
            record["realized_gain"] = (result or {}).get("realized_gain")
            record["cost_basis"] = (result or {}).get("cost_basis")
        return record
