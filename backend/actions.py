"""AI Actions — what to do right now, in shares, on the money you have.

Every other AI panel in this app answers "how strongly do the agents feel about
this stock?" and stops there. A 71/100 on NVDA is a view, not an instruction:
it doesn't say whether to act today, and it certainly doesn't say *how many
shares*. This module is the last step — it turns the scores already on screen
into a short list of concrete orders sized against a cash balance.

Where the balance comes from
----------------------------
Two sources, and the panel always says which one it used:

  the real figure  What you entered in **Available to trade** on the dashboard
                   (``AvailableCashService``). Then the dollars are your
                   dollars: the share counts below are orders you could place,
                   and a $0 balance correctly produces no buys at all.
  the stand-in     Nothing entered — the section is vacant. The app has no idea
                   what is in your brokerage account and won't invent one, so
                   it falls back to ``_BUDGET``, a flat $10,000 of pretend
                   money, and labels it. Read those dollar figures as "if you
                   had $10,000 to put to work today, this is the split" — the
                   *proportions* are the answer, the absolute numbers are a
                   scale you can multiply.

The balance is read fresh on every request, so entering it, editing it, or
buying something (which draws it down) resizes the whole plan immediately —
without re-running a single agent.

No model is called
------------------
This is arithmetic over the suggestions the advisor and the discover panel have
already generated, exactly like the agent-weight sliders. Consequences worth
knowing:

  - It is instant and free, so the plan can be recomputed on every page load,
    after every weight change, and after every buy without a second thought.
  - It is never fresher than the panels it reads. If the advisor last ran at
    this morning's bell, so did this.
  - Change a weight and the plan changes with it. That is the point: the
    allocation is downstream of how much you trust each agent.

Where the candidates come from
------------------------------
Both risk profiles are computed every time (like everywhere else in the app),
so the shared low/high toggle is a re-render rather than a request.

  buys   advisor holdings scored ``buy`` (add to a position you have)
         + advisor wishlist buys (the names you were already watching)
         + discover picks scored ``buy`` (from the In the News column)
         deduped by ticker — highest score wins — ranked by score, capped at
         ``_MAX_BUYS``.
  sells  holdings scored ``trim`` or ``sell``, worst score first, capped at
         ``_MAX_SELLS``.

Only those two. A name the agents landed on neutral produces no row: "wait" is
not an action, it is the absence of one, and a list of things not to do buries
the two or three that need doing. The neutral names are still on screen in the
AI Advisor column, scored, where reading them is a deliberate act rather than
scrolling past them to reach the orders. The one thing the plan does say about
not acting is the money: cash is a position, and it is priced.

Sizing, and the right to hold cash
----------------------------------
The balance is *available*, not a target, and cash is not the leftover — it is
a position that pays. Uninvested money earns ``CASH_APR_PCT`` (4.25% APR, the
same figure every agent is told about in ``ai_agents.py``), risk-free and
liquid, so money only leaves it when a name has earned it.

Each buy can claim at most an equal slice of the balance, and takes the part of
that slice its conviction justifies::

    slice    = budget / (how many buys the plan actually has)
    claim    = (confidence - 55) / (_FULL_CONVICTION_AT - 55)   capped at 1.0
    deployed = slice x claim

``_MAX_BUYS`` is a ceiling on the *list*, not a fixed divisor. Five buys split
the balance five ways; three buys split it three ways and each gets a third.
The agents finding only three names worth owning is not a reason to leave two
fifths of the balance behind — that would price "we found fewer good ideas" as
though it were "we are less sure about the ideas we found", and those are
different statements. Conviction is what sizes a position; the length of the
list is not.

A maximum-conviction name (85+) takes its whole slice; a 60, barely over the
buy floor, takes a sixth of it and leaves the rest earning 4.25%. What stays in
cash is therefore whatever weak scores don't claim — plus the whole balance
when there are no buys at all — and it is reported with what it earns, rather
than being pushed into the least-bad idea on the list.

A name with no live quote is left out of the divisor as well as unsized: it
cannot be turned into shares, so counting it would shrink everyone else's slice
in favour of an order that never gets placed.

Note what this is not: the score is a conviction reading, not a forecast
return, so nothing here claims to compare an expected gain against 4.25%
arithmetically. The rate does two honest jobs — it sets the bar the agents are
told to clear before calling anything a buy, and it prices what the plan holds
back, so leaving money alone reads as a position with a yield instead of an
allocator that ran out of ideas.

Sells are sized off the position you actually hold, scaled the same way in the
other direction: the further below the hold band a score sits, the more of the
position the plan suggests letting go, from a quarter just under the hold band
to the whole position once the score reaches the low teens.

A name with no live quote can't be turned into a share count, so it is listed
unsized and its dollars stay in cash rather than being guessed at.

Not financial advice. These are model-generated numbers.
"""

from backend.ai_agents import CASH_APR_PCT

# The fallback balance, used only while "Available to trade" is vacant. See the
# module note: with no real figure the proportions are the answer and the
# dollars are a scale, so this wants to be a round, obviously-notional number.
_BUDGET = 10000.0

# How the balance in a payload was arrived at, so the UI can caption it
# honestly instead of showing a pretend figure as though it were yours.
_SOURCE_ENTERED = "available"    # you told us
_SOURCE_PLACEHOLDER = "placeholder"  # the section is vacant; this is the stand-in

# The confidence floor for a buy — the bottom of the "Lean buy" band, the same
# edge ``ai_advisor._WISHLIST_MIN_CONFIDENCE`` uses. Keeping the two the same
# means a wishlist name shown as a buy in the advisor column is a name this
# panel is willing to size.
_BUY_FLOOR = 55.0

# The score at which a buy has earned its whole slice of the balance. Not 100:
# the blend of five independent agents lands in the 60s and 70s on names they
# all like, and a scale that only pays out at a score nobody ever reaches would
# leave the plan permanently in cash for the wrong reason. 85 is "the agents
# broadly agree this is a strong buy".
_FULL_CONVICTION_AT = 85.0

# The bottom of the neutral band, matching ``ai_advisor._CONFIDENCE_BANDS``. A
# score above it is not a sell; it is also where the sell scale starts.
_HOLD_LOW = 45.0

# How many of each kind of action to show. A plan longer than this stops being
# a plan: five buys is already $2,000 apiece of a $10,000 balance, and a sixth
# "sell a little of this too" is noise rather than instruction.
#
# A ceiling, not a quota. Nothing tries to fill it — the buy floor decides how
# many names qualify, and if that is two then two is the plan. It is also not
# the divisor the slices are cut with; see ``_slice``.
_MAX_BUYS = 5
_MAX_SELLS = 3

# The least of a position a "trim" ever suggests letting go. Below this the
# suggestion isn't worth a trade ticket.
_MIN_SELL_FRACTION = 0.25

# The score at or below which a sell means the whole position. Measuring "sell
# it all" from 0 would mean no real score ever produced a full exit — the
# agents bottom out in the teens on names they genuinely want gone — so the
# scale runs out a little above zero.
_FULL_SELL_AT = 10.0

# The two profiles every AI panel generates, so the toggle costs nothing.
_RISKS = ("low", "high")

# Where a candidate came from, and what to call it on screen.
_ORIGINS = {
    "holdings": "You hold this",
    "wishlist": "On your wishlist",
    "news": "In the news",
}


def _claim(confidence, floor: float = _BUY_FLOOR,
           full_at: float = _FULL_CONVICTION_AT) -> float:
    """How much of its slice a score has earned, 0.0 -> 1.0.

    The fraction of the balance a name takes *out of cash*. At the buy floor it
    is 0 — a name that only just qualifies has not made the case for money that
    is already earning 4.25% — and it reaches 1.0 at ``full_at``.

    Both edges are arguments rather than constants because they are the two
    dials that turn one allocator into a fussy one and a bold one: raise the
    floor and fewer names qualify at all, lower ``full_at`` and the ones that do
    are backed harder. The app passes neither and gets the module defaults; the
    competition harness gives each of its three agents its own pair, which is
    most of what makes them different allocators over identical scores.
    """
    if confidence is None:
        return 0.0
    span = max(1e-9, float(full_at) - float(floor))
    return max(0.0, min(1.0, (float(confidence) - float(floor)) / span))


def _sell_fraction(confidence) -> float:
    """How much of a position a score argues for letting go, 0.25 -> 1.0.

    Measured across the band between "just left neutral" and "get out": a 44
    gives up a quarter, anything at or below ``_FULL_SELL_AT`` gives up the
    lot. Continuous rather than one fraction per band, so a 21 and a 19 don't
    produce wildly different orders over a two-point difference.
    """
    if confidence is None:
        return _MIN_SELL_FRACTION
    span = _HOLD_LOW - _FULL_SELL_AT
    fraction = (_HOLD_LOW - float(confidence)) / span
    return max(_MIN_SELL_FRACTION, min(1.0, fraction))


class AiActionsService:
    """Turns cached agent scores into a sized, per-risk action plan.

    Composes the advisor (holdings + wishlist calls, and the agent weights they
    were blended under), the discover panel (stocks you don't own yet), the
    market provider (live prices, to convert dollars into shares), the
    portfolio (what you actually hold, to size a sell) and the available-cash
    service (how much there is to spend, when you've said).

    Holds no scoring logic and writes nothing: every number here is derived
    from suggestions those panels already produced, so a plan is only ever as
    fresh as they are and is regenerated from scratch on each request.
    """

    def __init__(self, advisor, discover, market, portfolio, available=None,
                 budget: float = _BUDGET, max_buys: int = _MAX_BUYS,
                 max_sells: int = _MAX_SELLS, buy_floor: float = _BUY_FLOOR,
                 full_conviction_at: float = _FULL_CONVICTION_AT):
        self.advisor = advisor
        self.discover = discover
        self.market = market
        self.portfolio = portfolio
        # The entered balance, or None/absent. Read per request rather than
        # cached: editing the figure or buying something has to resize the plan
        # on the next page load, and nothing else invalidates it.
        self.available = available
        # The stand-in used only while that section is vacant.
        self.budget = float(budget)
        self.max_buys = max_buys
        self.max_sells = max_sells
        # How selective this allocator is, and how hard it backs what clears
        # the bar — see ``_claim``. The app takes the defaults; a caller running
        # several allocators over the same scores varies them per allocator,
        # which is the whole difference between them.
        self.buy_floor = float(buy_floor)
        self.full_conviction_at = float(full_conviction_at)

    # --- public API -----------------------------------------------------

    def get(self) -> dict:
        """The whole panel: a plan per risk profile, plus status for the UI."""
        advisor = self._safe(lambda: self.advisor.get() if self.advisor else {}, {})
        discover = self._safe(lambda: self.discover.get() if self.discover else {}, {})

        budget, source = self._resolve_budget()

        candidates = {
            risk: self._candidates(advisor, discover, risk) for risk in _RISKS
        }
        prices = self._prices(
            {c["ticker"] for rows in candidates.values() for c in rows}
        )
        owned = self._owned()

        plans = {
            risk: self._plan(risk, candidates[risk], prices, owned, budget)
            for risk in _RISKS
        }
        # "Something has been scored", not "something is worth doing". Now that
        # neutral names produce no rows, a quiet day would otherwise look
        # identical to a panel that has never run — and the two want opposite
        # things said about them.
        scored = any(candidates[risk] for risk in _RISKS)

        return {
            "configured": bool(advisor.get("configured")),
            # Nothing has been generated yet vs. generated and quiet are very
            # different states, and the panel says which.
            "scored": scored,
            "budget": round(budget, 2),
            # "available" = the figure you entered, "placeholder" = the stand-in
            # standing in for one you haven't. The UI captions the number from
            # this rather than presenting pretend money as though it were real.
            "budget_source": source,
            "buy_floor": self.buy_floor,
            "full_conviction_at": self.full_conviction_at,
            "max_buys": self.max_buys,
            # What unspent money earns — the reason the plan is allowed to
            # leave any. Same figure the agents are told about.
            "cash_apr": CASH_APR_PCT,
            "generated_at": self._newest(
                advisor.get("generated_at"), discover.get("generated_at")
            ),
            "advisor_generated_at": advisor.get("generated_at"),
            "discover_generated_at": discover.get("generated_at"),
            # The plan moves when either panel is regenerating, so the UI can
            # keep polling on the same signal it already watches.
            "refreshing": bool(advisor.get("refreshing") or discover.get("refreshing")),
            "plans": plans,
            "error": advisor.get("error"),
        }

    # --- candidates -----------------------------------------------------

    def _candidates(self, advisor: dict, discover: dict, risk: str) -> list:
        """Every scored name available at this risk, tagged with its origin.

        Deduped by ticker keeping the highest score, because the same stock can
        legitimately arrive twice — a wishlist name that also turns up in the
        news — and one stock should produce one order.
        """
        rows = []
        profile = (advisor.get("risk_profiles") or {}).get(risk) or {}
        rows += [
            (s, "holdings") for s in (profile.get("suggestions") or [])
        ]
        # The advisor's wishlist list is pre-filtered to buys; ``candidates``
        # is the unfiltered blend. Reading the unfiltered one costs nothing and
        # keeps this panel's buy floor its own, rather than inheriting whatever
        # the advisor column happened to cut at.
        wishlist = profile.get("wishlist") or {}
        rows += [
            (s, "wishlist")
            for s in (wishlist.get("candidates") or wishlist.get("suggestions") or [])
        ]
        news = (discover.get("risk_profiles") or {}).get(risk) or {}
        rows += [(s, "news") for s in (news.get("suggestions") or [])]

        best = {}
        for suggestion, origin in rows:
            ticker = (suggestion or {}).get("ticker")
            if not ticker:
                continue
            score = suggestion.get("confidence")
            current = best.get(ticker)
            if current is None or (score or 0) > (current["confidence"] or 0):
                best[ticker] = {
                    "ticker": ticker,
                    "origin": origin,
                    "confidence": score,
                    "suggestion": suggestion,
                }
        return list(best.values())

    # --- the balance ----------------------------------------------------

    def _resolve_budget(self):
        """(amount, source) — what this plan is sized against, and whose it is.

        The entered figure wins whenever there is one, *including zero*: "I
        have nothing to invest" is an answer, and quietly substituting $10,000
        of pretend money for it would produce a page of buys against money the
        investor just said they don't have. Only a vacant section falls back.
        """
        amount = self._safe(
            lambda: self.available.get() if self.available else None, None
        )
        if amount is None:
            return self.budget, _SOURCE_PLACEHOLDER
        return max(0.0, float(amount)), _SOURCE_ENTERED

    # --- the plan -------------------------------------------------------

    def _plan(self, risk: str, candidates: list, prices: dict, owned: dict,
              budget: float) -> dict:
        """One risk profile's buys and sells, with the cash arithmetic."""
        # With nothing to spend there is no buy to plan — a row reading
        # "buy 0 shares for $0" is not an instruction, it is the arithmetic
        # leaking. The names are still scored in the AI Advisor column; what
        # this panel has to say about them today is that they aren't affordable.
        buys = sorted(
            (
                c for c in candidates
                if c["suggestion"].get("action") == "buy"
                and (c["confidence"] or 0) >= self.buy_floor
            ),
            key=lambda c: c["confidence"] or 0,
            reverse=True,
        )[: self.max_buys] if budget > 0 else []

        sells = sorted(
            (
                c for c in candidates
                # Only a position you actually hold can be sold. A "trim" on a
                # wishlist name means "don't enter" — which is not an action,
                # so it produces no row at all.
                if c["origin"] == "holdings"
                and c["suggestion"].get("action") in ("trim", "sell")
                and owned.get(c["ticker"])
            ),
            key=lambda c: c["confidence"] if c["confidence"] is not None else 50,
        )[: self.max_sells]

        buy_rows, allocated = self._size_buys(buys, prices, owned, budget)
        sell_rows, proceeds = self._size_sells(sells, prices, owned)

        cash = round(budget - allocated, 2)
        # The equal share one name could have taken, at this plan's length.
        # Reported per profile rather than derived client-side: low and high
        # risk routinely surface a different number of buys, so there is no one
        # divisor for the panel to apply.
        sizeable = sum(1 for r in buy_rows if r.get("price"))
        return {
            "risk": risk,
            "buys": buy_rows,
            "sells": sell_rows,
            "total": len(buy_rows) + len(sell_rows),
            "slice_size": round(self._slice(budget, sizeable), 2),
            "sizeable_buys": sizeable,
            # A balance of exactly nothing. Distinct from "nothing scored well
            # enough today", and the two want different sentences on screen.
            "no_cash": budget <= 0,
            # What the plan actually commits, and what it deliberately doesn't.
            "allocated": round(allocated, 2),
            "cash_remaining": cash,
            "cash_pct": round(cash / budget * 100, 1) if budget else None,
            # Cash isn't idle, so the plan says what it earns. This is the
            # figure a buy has to be worth more than.
            "cash_apr": CASH_APR_PCT,
            "cash_income_year": round(cash * CASH_APR_PCT / 100, 2),
            "cash_income_month": round(cash * CASH_APR_PCT / 100 / 12, 2),
            "cash_income_quarter": round(cash * CASH_APR_PCT / 100 / 4, 2),
            # Selling raises cash the buys above do not spend — the two sides
            # of the plan are independent, and saying so avoids implying the
            # sells are what funds the buys.
            "sell_proceeds": round(proceeds, 2),
        }

    @staticmethod
    def _slice(budget: float, sizeable: int) -> float:
        """The most any single name may take out of cash.

        Cut with the number of buys the plan actually has, not ``_MAX_BUYS``.
        Three names split the balance three ways. The cap limits how long the
        list may get; it does not decide how much of the balance a shorter list
        is allowed to reach — see the sizing note at the top of the module.

        ``sizeable`` counts only the buys that have a live quote, since the
        ones that don't can't be turned into shares whatever slice they're
        given. Zero of them means there is nothing to size and no slice.
        """
        return budget / sizeable if sizeable else 0.0

    def _size_buys(self, buys: list, prices: dict, owned: dict, budget: float):
        """Turn the buy candidates into share counts. Returns (rows, allocated).

        Each name takes the part of an equal slice that its conviction has
        earned, and nothing tops the rest up: what isn't claimed stays in cash
        earning ``CASH_APR_PCT``, which is a better home for it than a name the
        agents were only lukewarm about.
        """
        rows = [self._row(c, prices, owned) for c in buys]
        # Sized off the priced rows only, so an unquoted name doesn't shrink
        # the slice of every name that can actually be bought.
        one_slice = self._slice(budget, sum(1 for r in rows if r["price"]))
        allocated = 0.0
        for row in rows:
            if not row["price"]:
                # No quote, so no share count. Say so on the row rather than
                # inventing one; its dollars stay in cash.
                row.update(shares=None, cost=None, claim_pct=None, unpriced=True)
                continue
            claim = _claim(row["confidence"], self.buy_floor,
                           self.full_conviction_at)
            shares = one_slice * claim / row["price"]
            cost = round(shares * row["price"], 2)
            row.update(
                shares=round(shares, 4),
                cost=cost,
                # How much of its own slice this name earned, which is the
                # number that explains the size — a share of the total would
                # make a lone weak buy look like a full-conviction bet.
                claim_pct=round(claim * 100, 1),
                # The slice it was measured against, so the UI can caption the
                # claim without re-deriving a divisor it can't see.
                slice_size=round(one_slice, 2),
            )
            allocated += cost
        return rows, allocated

    def _size_sells(self, sells: list, prices: dict, owned: dict):
        """Turn the sell candidates into share counts. Returns (rows, proceeds)."""
        rows = []
        proceeds = 0.0
        for candidate in sells:
            row = self._row(candidate, prices, owned)
            held = owned.get(row["ticker"], {}).get("shares") or 0
            fraction = _sell_fraction(row["confidence"])
            shares = round(held * fraction, 4)
            value = round(shares * row["price"], 2) if row["price"] else None
            row.update(
                shares=shares,
                sell_fraction=round(fraction * 100),
                sell_all=fraction >= 1.0,
                proceeds=value,
                unpriced=not row["price"],
            )
            proceeds += value or 0.0
            rows.append(row)
        return rows, proceeds

    def _row(self, candidate: dict, prices: dict, owned: dict) -> dict:
        """The shared shape every action item renders from.

        Carries the whole suggestion alongside the numbers so the detail view
        can show the five agents' arguments — the same card the advisor column
        renders — without a second request or a client-side join.
        """
        ticker = candidate["ticker"]
        suggestion = candidate["suggestion"]
        quote = prices.get(ticker) or {}
        position = owned.get(ticker) or {}
        return {
            "ticker": ticker,
            "name": quote.get("name"),
            "origin": candidate["origin"],
            "origin_label": _ORIGINS.get(candidate["origin"], candidate["origin"]),
            "confidence": candidate["confidence"],
            "confidence_label": suggestion.get("confidence_label"),
            "action": suggestion.get("action"),
            "consensus": suggestion.get("consensus"),
            "horizon_months": suggestion.get("horizon_months"),
            "headline": suggestion.get("headline"),
            "headline_from": suggestion.get("headline_from"),
            "price": quote.get("price"),
            "previous_close": quote.get("previous_close"),
            # What you already own of it — context a share count is meaningless
            # without ("buy 12" reads differently against 0 shares and 400).
            "owned_shares": position.get("shares"),
            "owned_avg_price": position.get("avg_price"),
            "shares": None,
            "cost": None,
            "suggestion": suggestion,
        }

    # --- inputs ---------------------------------------------------------

    def _prices(self, tickers) -> dict:
        """Live quotes for every ticker in either plan, in one call.

        Fetched here rather than taken from the suggestions because a score
        generated at this morning's bell carries this morning's price, and a
        share count computed off a stale price is wrong by however far the
        stock has moved since.
        """
        tickers = sorted(t for t in tickers if t)
        if not tickers:
            return {}
        provider = getattr(self.market, "provider", None)
        if provider is None:
            return {}
        return self._safe(lambda: provider.get_quotes(tickers) or {}, {})

    def _owned(self) -> dict:
        """Current positions by ticker — what a sell can be sized against."""
        positions = self._safe(
            lambda: self.portfolio.list_stocks() if self.portfolio else [], []
        )
        return {p["ticker"]: p for p in positions}

    @staticmethod
    def _newest(*stamps):
        """The most recent of several ISO timestamps, ignoring the missing ones."""
        usable = [s for s in stamps if s]
        return max(usable) if usable else None

    @staticmethod
    def _safe(fetch, fallback):
        """Run a read, falling back rather than failing the whole panel.

        This panel is downstream of everything else on screen, so it is the
        last place that should be able to take the page down: a wedged quote
        provider costs it share counts, not the plan.
        """
        try:
            return fetch()
        except Exception as e:
            print(f"AI actions: {type(e).__name__}: {e}")
            return fallback
