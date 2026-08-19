"""The five independent research agents behind every confidence score.

Each agent is a *separate LLM call with a separate, disjoint slice of the
evidence*. That separation is the whole point of this module, and it is
structural rather than a matter of instructions: an agent cannot be influenced
by another agent's reasoning because that reasoning is never in its context
window, and it cannot be influenced by another agent's evidence because
``payload()`` hands it only its own. The five scores meet for the first time in
``ai_advisor.py``, as numbers, in a weighted average.

    company_perspective  Is the business on the right track? — recent company
                         news, what it sells and where that is going, the
                         earnings record against expectations, revenue and
                         earnings growth, partnerships and product launches.

    personal             Does this look like the setups that have worked in
                         *this* portfolio? — the position (shares, cost basis,
                         unrealized P&L), a year of the stock's own price
                         history with the pattern already measured out of it
                         (returns, drawdown, volatility, trend), and the
                         investor's own realized trades in the name.

    statistics           Are the raw numbers attractive? — P/E, PEG, EPS,
                         market cap, price/sales, price/book, EV/EBITDA,
                         margins, returns on equity and assets, cash, debt,
                         beta, the 52-week range, short interest. No story, no
                         news, no opinions.

    expert               What do the big desks conclude? — the sell-side
                         consensus and its 1-5 mean, the bull/bear head count,
                         the price-target spread, and which firms upgraded,
                         downgraded or initiated lately, at what target.

    macro                Does the wider world favour owning this right now? —
                         market-wide headlines that mention no single company:
                         rates, inflation, tariffs, war, energy, regulation and
                         government policy, read against each ticker's sector
                         and beta.

Why disjoint evidence, and not just disjoint questions
------------------------------------------------------
Averaging five opinions only buys you something when the errors are
uncorrelated. Show all five agents the same analyst research and you have not
built five estimators, you have built one estimator sampled five times: they
agree because they read the same paragraph, and their agreement then reads as
confirmation. So the fundamentals split cleanly — growth and the earnings
record go to ``company_perspective``, the multiples and the balance sheet go to
``statistics``, and neither sees the other's fields. Only the price appears in
more than one payload, because a multiple without a price is not a number.

The cost is real and worth naming: each agent is *less informed* than the
single all-seeing model this replaced. The statistics agent scores a 160x P/E
without knowing a product just shipped; the company agent reads the product
news without knowing what the market already charges for it. Neither is wrong
about its own dimension, and the weighted average — with the weights in the
user's hands — is where the trade-off gets made, visibly, instead of inside one
model's hidden reasoning.

The scale is shared even though nothing else is: every agent answers 0-100,
where 100 is maximum conviction to buy, 50 is neutral, and 0 is maximum
conviction to sell. An agent whose own evidence says nothing about a ticker is
told to score it near 50 rather than borrow conviction it hasn't earned, which
keeps a quiet dimension from dragging the average around.

One other thing is shared, and it is deliberately not evidence: every agent is
told what **cash** pays (``CASH_APR_PCT``). Doing nothing is not a zero — it
earns a risk-free yield — so "beats a savings account over a quarter" is the
bar a buy has to clear, and staying out is an answer rather than a missing one.
Without it the models score as though the investor were obliged to be fully
invested, and a stock expected to go nowhere comes back as a 50 instead of the
loss against cash that it is.

The same goes for *how much* cash there is. When the investor has filled in
**Available to trade**, every agent is told the figure (``available_cash_note``)
— again identically, so nothing about the disjoint-evidence split changes. It
is not evidence about any company; it is the size of the decision, and $500 and
$500,000 make the same 60/100 mean different things. Left vacant, the line is
omitted entirely and the prompts are what they always were.

Adding a sixth agent means adding a class here with a ``role``, a ``payload``
and an entry in ``AGENTS``. Nothing in ``ai_advisor.py`` enumerates the five.
"""

import json

# --- what every agent is told -------------------------------------------

# The instruction that makes the separation legible to the model as well as
# structural. Without it, models notice they are seeing a partial picture and
# hedge toward an imagined consensus — which is exactly the correlated error
# the five-agent split exists to avoid.
_INDEPENDENCE = (
    "YOU ARE ONE OF FIVE INDEPENDENT AGENTS. Four other agents are scoring the "
    "same tickers right now, each on a completely different body of evidence — "
    "one on the company's own story, one on the investor's position and price "
    "history, one on the raw statistics, one on Wall Street research, one on "
    "macro conditions. You cannot see their work and they cannot see yours. "
    "Afterwards a weighting system averages the five scores into the number the "
    "investor acts on.\n"
    "This has three consequences for how you answer:\n"
    "  - Do NOT try to produce a balanced all-things-considered verdict. That "
    "is the average's job, not yours. Yours is to state what YOUR evidence "
    "says, as sharply as it supports.\n"
    "  - Do NOT speculate about what the other agents will say, and do not "
    "reason about evidence you were not given. If you find yourself writing "
    "'valuation looks stretched' without a multiple in front of you, stop — "
    "that is another agent's dimension.\n"
    "  - If your evidence is thin or silent on a ticker, say so plainly and "
    "score it near 50. A confident number invented to seem useful is worse "
    "than an honest neutral, because it is weighted the same."
)

# One scale for all five, so the average means something.
_SCALE = (
    "Express your view of EACH ticker as a CONFIDENCE SCORE — an integer from "
    "0 to 100 measuring how strongly YOUR dimension says to act:\n"
    "  100 = maximum conviction to BUY\n"
    "   75 = a solid buy\n"
    "   50 = neutral — your evidence gives no reason to act either way\n"
    "   25 = you would reduce or avoid\n"
    "    0 = maximum conviction to SELL / avoid entirely\n"
    "Scores from about 45 to 55 all read as neutral; the further from 50, the "
    "stronger the signal. Use the whole range — reserve the extremes for "
    "genuine conviction, but do not park everything near 50 to hedge either. "
    "Give the matching 'action' label too, and make sure the two agree: 'buy' "
    "above 55, 'hold' 45-55, 'trim' 20-45, 'sell' below 25."
)

# What uninvested money earns while it waits — the single source of truth for
# the whole app. It is in this module because it belongs in every agent's
# prompt: it changes what a "buy" has to clear. ``actions.py`` imports it from
# here to price the cash its plan deliberately leaves unspent.
#
# 4.25% is a stand-in for a high-yield savings / money-market rate. Change this
# one number when rates move and both the prompts and the plan follow.
CASH_APR_PCT = 4.25

# The alternative every buy is actually competing with. Without this the models
# use "will it go up" as the bar, which is the wrong bar — doing nothing is not
# a zero, it pays — and they answer as if the investor were obliged to be fully
# invested, which they are not.
_CASH = (
    "CASH IS A REAL ALTERNATIVE, AND IT PAYS. Any money not in a stock sits in "
    f"a liquid account earning about {CASH_APR_PCT:g}% APR — roughly "
    f"{CASH_APR_PCT / 12:.2f}% a month, {CASH_APR_PCT / 4:.2f}% over a quarter "
    "— risk-free, and available the moment something better turns up.\n"
    "Three consequences for your score:\n"
    "  - The bar for a buy is not 'will this rise'. It is 'does MY evidence say "
    "this beats a guaranteed "
    f"{CASH_APR_PCT / 4:.2f}% over one to three months, given the risk it "
    "carries'. A name you expect to drift sideways is WORSE than cash, not "
    "equal to it — score it below 50 rather than at it.\n"
    "  - Nobody has to spend anything. Staying in cash and waiting for a better "
    "entry is a legitimate, profitable outcome, not a failure to have an "
    "opinion. When that is what your evidence supports, say so plainly instead "
    "of manufacturing a buy to look useful.\n"
    "  - Do not ration. You are not allocating a budget, competing with the "
    "other agents for it, or deciding position sizes — score every ticker on "
    "its own merits against cash and let the weighting system downstream decide "
    "what actually gets bought and how much."
)


# The shape of the answer. Identical for every agent so one JSON schema and one
# cleaner in ``ai_advisor.py`` handle all five.
_OUTPUT = (
    "Return one suggestion per ticker you were given, and only those tickers — "
    "never invent a symbol. 'confidence' is your 0-100 score from your own "
    "dimension alone. 'action' is the matching label. 'horizon_months' is 1, 2 "
    "or 3 — the number of months you expect your call to play out over, never "
    "longer than a quarter and never expressed in days. 'headline' is one short "
    "line. 'reasoning' is 3-6 short lines that cite the specific figures or "
    "headlines you actually used — four other agents are writing their own, so "
    "keep yours to your dimension and do not pad. 'price_trigger' is a concrete "
    "price-based action if your evidence supports one, else an empty string. "
    "'risks' names the main thing that would make your call wrong. "
    "'portfolio_note' is one or two lines on what your dimension says about the "
    "whole list. Be direct; skip generic disclaimers."
)

# The risk toggle, expressed so each agent can apply it inside its own lens
# rather than as a portfolio-level instruction it has no way to act on.
_STANCE = {
    "low": (
        "The investor wants a LOW-RISK stance: capital preservation and steady "
        "gains ahead of aggressive upside. Within your own dimension, demand "
        "clearer evidence before scoring far from 50, and treat fragility, "
        "uncertainty and stretched expectations as reasons to score lower."
    ),
    "high": (
        "The investor wants a HIGH-RISK stance: maximising gains, comfortable "
        "with volatility. Within your own dimension, you may score real "
        "conviction on weakness or on an improving-but-unproven picture, and "
        "volatility on its own is not a reason to score low — a deteriorating "
        "picture is."
    ),
}

# Holdings and watchlist are different questions, so the neutral point means
# something different in each. Everything else about an agent is unchanged.
_KIND_FRAME = {
    "holdings": (
        "These are stocks the investor ALREADY OWNS. You are scoring conviction "
        "to BUY MORE (high), HOLD (near 50) or SELL (low) over the coming one "
        "to three months."
    ),
    "wishlist": (
        "These are stocks the investor does NOT own and is watching. You are "
        "scoring conviction to BUY NOW (high), WAIT (near 50) or AVOID (low) "
        "over the coming one to three months."
    ),
}

_KINDS = ("holdings", "wishlist")


# The last of the shared lines, and the only one that is generated rather than
# a constant — because it carries a number the investor typed in.
def available_cash_note(amount) -> str:
    """What the investor actually has to spend, when they have said.

    Returns "" while the **Available to trade** section is vacant, which is the
    default and the pre-existing behaviour: with no figure entered the agents
    score exactly as they always did, against cash-in-the-abstract.

    When there *is* a figure, every agent is told it. Not as evidence — it says
    nothing about any company, and the five slices stay disjoint because all
    five get the same line — but as the size of the decision. The same 60/100
    means something different against $500 than against $500,000: at the small
    end a single share of an expensive name is the entire position and there is
    no room to be wrong, and a marginal buy costs the chance to take the good
    one that turns up next week. Without the number the models reason as though
    capital were unlimited, which is the failure mode this fixes.

    It deliberately does NOT ask for position sizes. The share counts are
    arithmetic in ``actions.py``, sized against this same balance, and an agent
    inventing its own split would fight that — so the note reinforces the
    "do not ration" rule above rather than relaxing it, and asks only that
    scarcity be priced into the score and named in the reasoning.
    """
    if amount is None:
        return ""
    amount = float(amount)
    if amount <= 0:
        return (
            "THE INVESTOR HAS NO CASH AVAILABLE TO TRADE. They have stated a "
            "balance of $0 — nothing can be bought today whatever you score, "
            "and anything they do buy has to be funded by selling something "
            "they already own.\n"
            "Two consequences for your score:\n"
            "  - Keep scoring honestly. A buy-scored name is still worth "
            "flagging: it tells the investor what they would want first when "
            "money arrives, and it is the bar a sell candidate has to be worse "
            "than.\n"
            "  - Be correspondingly firmer about names your evidence has gone "
            "cold on. With no cash, the only way into a better position is out "
            "of a worse one, so an honest low score is more useful than usual."
        )
    return (
        "THE INVESTOR HAS A FINITE AMOUNT TO SPEND: "
        f"${amount:,.2f} of cash available to trade, and that is all. This is "
        "a real balance they entered, not a notional one, and every buy across "
        "their whole list — holdings they might add to, watchlist names, and "
        "stocks in the news — competes for the same pot.\n"
        "Three consequences for your score:\n"
        "  - Scarcity raises the bar. A marginal buy does not just have to beat "
        f"cash at {CASH_APR_PCT:g}% APR, it has to be worth spending money that "
        "then cannot go to the better idea that turns up next month. When your "
        "evidence is merely mildly positive, that is a score in the 50s, not "
        "the 70s.\n"
        "  - Size the balance against the price. If one share costs a large "
        f"fraction of ${amount:,.2f}, entering at all is a concentrated bet "
        "rather than a position — say so plainly in your reasoning, and let it "
        "temper a score you would otherwise give freely. If a share costs more "
        "than the whole balance, say that outright.\n"
        "  - Still do not ration or allocate. Do not divide the balance between "
        "tickers, do not name dollar amounts or share counts, and do not lower "
        "one ticker's score because you already scored another highly. A "
        "separate step sizes the actual orders against this same figure; your "
        "job is to price the scarcity into each score and explain it."
    )


def mandate_note(text) -> str:
    """The standing instruction this portfolio is run under, when it has one.

    Returns "" for None or blank — which is what the app itself always passes,
    so the ordinary assistant's prompts are untouched by this existing.

    The one caller today is the 30-day competition harness
    (``backend/competition``), where three portfolios are run by the same five
    agents under three different weightings and each has a deadline. Two facts
    have to reach the models for their scores to mean anything there, and
    neither is evidence about a company:

      the clock     A one-to-three-month call is the horizon this app is built
                    around, and it is the wrong horizon for a book that is
                    marked to market in eleven trading days. Told nothing, the
                    agents keep answering the question they were designed for
                    and their scores quietly stop matching what they are being
                    used for.
      the remit     "Only act on names the raw numbers like" is a real
                    constraint on what a score is *for*, and an agent that
                    doesn't know it will score a thin, story-driven name a
                    confident 70 that the portfolio it belongs to would never
                    act on.

    Like ``available_cash_note`` this is handed to all five agents *identically*
    and is deliberately not a sixth slice of evidence, so the disjoint-evidence
    guarantee the module rests on is unchanged: every agent still reasons from
    its own data alone, it just knows what the answer is being used for.

    The framing below matters as much as the text. Dropped in bare, models read
    a mandate as a hint about the right answer and drift toward it; named as a
    constraint on the *use* of the score, with an explicit instruction not to
    let it move the number, they price the deadline instead of pleasing it.
    """
    text = (text or "").strip()
    if not text:
        return ""
    return (
        "THE MANDATE THIS PORTFOLIO IS RUN UNDER. This is not evidence and it "
        "is not a hint about what to conclude — all five agents are being told "
        "exactly the same thing. It is what your score will be used for, and "
        "you can only price the horizon correctly if you know it:\n"
        f"{text}\n"
        "Two rules about it:\n"
        "  - Let it set the HORIZON and the BAR, not the answer. If the mandate "
        "gives you weeks and your evidence is about a thesis that needs "
        "quarters, that is a lower score — say so and say why. Do not raise a "
        "score because the mandate wants action, and do not lower one because "
        "it counsels caution.\n"
        "  - It does not narrow your dimension. You still read only your own "
        "evidence and you still answer only from it; the mandate tells you when "
        "the answer is due, not which numbers to look at."
    )


# --- fundamentals, split between the agents that may see them -----------
#
# Disjoint by construction. ``fundamentals_data.py`` returns one flat dict per
# ticker; these tuples decide which agent gets which keys, and no key appears
# in both lists. Adding a field to the provider does not leak it into a prompt
# until it is named here.

# The business itself: is it growing, and does it deliver what was expected?
_COMPANY_FIELDS = (
    "sector",
    "industry",
    "business_summary",
    "total_revenue",
    "revenue_growth_pct",
    "earnings_growth_pct",
    "earnings_quarterly_growth_pct",
    "earnings_surprises",
)

# The numbers a screener would show you, and nothing that needs a story.
_STATISTIC_FIELDS = (
    "market_cap",
    "trailing_pe",
    "forward_pe",
    "peg_ratio",
    "trailing_eps",
    "forward_eps",
    "price_to_sales",
    "price_to_book",
    "ev_to_ebitda",
    "ev_to_revenue",
    "gross_margin_pct",
    "operating_margin_pct",
    "profit_margin_pct",
    "return_on_equity_pct",
    "return_on_assets_pct",
    "ebitda",
    "total_cash",
    "total_debt",
    "debt_to_equity",
    "current_ratio",
    "free_cash_flow",
    "operating_cash_flow",
    "beta",
    "week52_high",
    "week52_low",
    "fifty_day_average",
    "two_hundred_day_average",
    "change_52w_pct",
    "dividend_yield_pct",
    "short_pct_of_float",
    "held_pct_institutions",
)

# What the wider world can act on: which sector the shock lands in, and how
# hard this name usually gets hit when the market moves.
_MACRO_FIELDS = ("sector", "industry", "beta", "market_cap")


def _pick(source, fields):
    """The named keys of ``source`` that actually have a value.

    Omitting empty keys rather than sending nulls keeps a thinly covered symbol
    costing a few tokens instead of a wall of "None", and stops a model reading
    an explicit null as a meaningful zero.
    """
    if not source:
        return {}
    return {f: source[f] for f in fields if source.get(f) is not None}


def _rows(context, kind):
    """The per-ticker rows for one side of the app (holdings or watchlist)."""
    return context.get("holdings" if kind == "holdings" else "wishlist") or []


# --- the agent base -----------------------------------------------------


class Agent:
    """One independent analyst: a role, a slice of the evidence, one score.

    Subclasses supply ``role()`` (who the agent is and what it may reason
    about), ``data_note()`` (how to read the JSON it gets) and ``payload()``
    (the slice itself — the only thing that ever reaches the model).
    """

    key = ""      # stable id; the weights file and the API are keyed by this
    name = ""     # human name, shown in the UI
    short = ""    # two-letter tag for the compact per-agent chips
    focus = ""    # one line explaining the dimension, shown as a tooltip

    # --- prompt assembly (shared) ---------------------------------------

    def system(self, kind: str) -> str:
        return (
            f"{self.role(kind)}\n\n"
            f"{_KIND_FRAME[kind]}\n\n"
            f"{_INDEPENDENCE}\n\n"
            f"{_SCALE}\n\n"
            f"{_CASH}\n\n"
            f"{_OUTPUT}"
        )

    def prompt(self, payload: dict, kind: str, risk: str,
               available_cash=None, mandate=None) -> str:
        """The user turn: the stance, the constraints, then the evidence.

        ``available_cash`` and ``mandate`` are the only things here that are
        shared across all five agents, and like ``_CASH`` in the system prompt
        they are constraints rather than evidence — see ``available_cash_note``
        and ``mandate_note``. Both default to None, which is what the app
        passes for the mandate and what a vacant balance produces, and that
        leaves the prompt exactly as it was before either feature existed.
        """
        budget = available_cash_note(available_cash)
        standing = mandate_note(mandate)
        return (
            f"{_STANCE[risk]}\n\n"
            + (f"{budget}\n\n" if budget else "")
            + (f"{standing}\n\n" if standing else "")
            + f"{self.data_note(kind)}\n\n"
            f"{json.dumps(payload, indent=2, default=str)}"
        )

    # --- to implement ---------------------------------------------------

    def role(self, kind: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def data_note(self, kind: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def payload(self, context: dict, kind: str):  # pragma: no cover - abstract
        """This agent's private slice of the context, or None when it has
        nothing to work with — in which case the agent is skipped for that
        (kind, risk) and the remaining weights are renormalised."""
        raise NotImplementedError

    # --- helpers --------------------------------------------------------

    def describe(self) -> dict:
        """The metadata the UI needs to render this agent generically."""
        return {
            "key": self.key,
            "name": self.name,
            "short": self.short,
            "focus": self.focus,
        }


# --- 1. company perspective ---------------------------------------------


class CompanyPerspectiveAgent(Agent):
    key = "company_perspective"
    name = "Company perspective"
    short = "CO"
    focus = (
        "The business itself: news, what it sells and where that's going, the "
        "earnings record against expectations, growth, and who it works with."
    )

    def role(self, kind):
        return (
            "You are a company research analyst. Your only question is whether "
            "the BUSINESS is on the right track and whether its prospects look "
            "good from here. You are given, per ticker: what the company does, "
            "its sector and industry, recent headlines about it, when it next "
            "reports, its last four quarters of actual EPS with how far each "
            "beat or missed expectations, and its trailing revenue and earnings "
            "growth.\n"
            "Read the headlines for what they say about the FRANCHISE, not the "
            "share price: product launches and product lines going stale, "
            "customer wins and losses, partnerships and joint ventures, "
            "capacity and supply deals, management changes, litigation and "
            "regulatory action against this specific company. A headline about "
            "the stock rising is not company news; a headline about a new "
            "design win is.\n"
            "Weigh the earnings record as a track record: a company that beats "
            "expectations four quarters running is being systematically "
            "under-modelled, and one that misses repeatedly has a management or "
            "a demand problem. Read growth alongside it — accelerating revenue "
            "with widening beats is a very different business from decelerating "
            "revenue with in-line prints.\n"
            "You have NOT been given valuation, price history, analyst ratings "
            "or macro news. Do not reason about any of them. A great business "
            "scores high here even if it is expensive — pricing it is another "
            "agent's job."
        )

    def data_note(self, kind):
        return (
            "Here is the company evidence as JSON, one entry per ticker. "
            "'business' carries what the company does plus its growth and its "
            "earnings-surprise record (fields ending in '_pct' are already "
            "percentages; 'surprise_pct' is how far actual EPS beat (+) or "
            "missed (-) expectations that quarter). 'recent_news' is real "
            "headlines from the last month, newest first — never invent others. "
            "'earnings_date' is the next scheduled report. A missing field "
            "simply wasn't reported; don't guess at it."
        )

    def payload(self, context, kind):
        rows = []
        for row in _rows(context, kind):
            business = _pick(row.get("fundamentals"), _COMPANY_FIELDS)
            news = row.get("recent_news") or []
            if not business and not news:
                continue  # nothing to research — leave the ticker unscored
            entry = {"ticker": row["ticker"], "price": row.get("price")}
            if row.get("earnings_date"):
                entry["earnings_date"] = row["earnings_date"]
            if business:
                entry["business"] = business
            if news:
                entry["recent_news"] = news
            rows.append(entry)
        if not rows:
            return None
        return {"as_of": context.get("as_of"), "companies": rows}


# --- 2. personal / historical pattern -----------------------------------


class PersonalAgent(Agent):
    key = "personal"
    name = "My position & history"
    short = "ME"
    focus = (
        "Your own position and the stock's price history: does the current "
        "pattern match the ones that have worked before?"
    )

    def role(self, kind):
        owned = kind == "holdings"
        return (
            "You are the investor's own position analyst. Your only question is "
            "what the PRICE HISTORY and the investor's own record in this name "
            "say — pattern recognition, not fundamentals.\n"
            + (
                "You are given, per holding: how many shares are held at what "
                "average cost, the unrealized profit or loss on the position, a "
                "year of the stock's own price path (weekly closes), the "
                "measured shape of that path — returns over 1, 3, 6 and 12 "
                "months, distance from the 52-week high and low, trend versus "
                "the 50- and 200-day averages, and annualised volatility — and "
                "any earlier sales the investor has made in this ticker with "
                "the gain or loss realised.\n"
                if owned
                else
                "The investor does NOT own these. You are given a year of each "
                "stock's own price path (weekly closes) and the measured shape "
                "of that path — returns over 1, 3, 6 and 12 months, distance "
                "from the 52-week high and low, trend versus the 50- and "
                "200-day averages, and annualised volatility — plus the "
                "portfolio-level context of how the investor's existing "
                "positions have behaved. Judge the entry the same way you would "
                "judge adding to a holding.\n"
            )
            + "Score on whether the current setup resembles the patterns that "
            "have historically been worth buying: a durable uptrend still "
            "intact, a pullback within one, a base after a long decline, versus "
            "a broken trend, a parabolic run far above its averages, or a "
            "position that has been dead money for a year.\n"
            + (
                "The cost basis matters for context, not as a rule. A large "
                "unrealized loss is not itself a reason to sell (the market "
                "does not know your entry) nor to average down; what matters is "
                "whether the pattern that justified owning it is still there. "
                "Say plainly when the honest answer is that the position has "
                "gone nowhere.\n"
                if owned
                else ""
            )
            + "You have NOT been given news, fundamentals, valuation or analyst "
            "ratings. Do not reason about any of them, and do not explain a "
            "price move by guessing at its cause."
        )

    def data_note(self, kind):
        return (
            "Here is the position and price-history evidence as JSON. "
            "'history' carries the measured pattern: 'return_*_pct' are total "
            "price returns over those windows, 'vs_52w_high_pct' and "
            "'vs_52w_low_pct' are where the price sits in its range, "
            "'vs_50d_pct' / 'vs_200d_pct' are the distance from the moving "
            "averages (positive = above), 'volatility_pct' is annualised. "
            "'path' is weekly closes over the past year, oldest first. "
            + (
                "'position' is what is actually owned and 'past_sales' are the "
                "investor's own earlier exits in this ticker. "
                if kind == "holdings"
                else ""
            )
            + "'portfolio' is the investor's overall unrealized and realized "
            "gains across time windows, for context on how the whole book has "
            "been behaving. A missing field simply wasn't available."
        )

    def payload(self, context, kind):
        rows = []
        for row in _rows(context, kind):
            history = row.get("history")
            if not history:
                continue  # no price history — nothing for this agent to read
            entry = {
                "ticker": row["ticker"],
                "price": row.get("price"),
                "history": history,
            }
            if kind == "holdings":
                entry["position"] = {
                    "shares": row.get("shares"),
                    "avg_price": row.get("avg_price"),
                    "cost_basis": row.get("cost_basis"),
                    "unrealized": row.get("total_unrealized"),
                }
                if row.get("past_sales"):
                    entry["past_sales"] = row["past_sales"]
            rows.append(entry)
        if not rows:
            return None
        return {
            "as_of": context.get("as_of"),
            "portfolio": {
                "unrealized_gains_history": context.get("unrealized_gains_history"),
                "realized_gains": context.get("realized_gains"),
            },
            "stocks": rows,
        }


# --- 3. raw statistics --------------------------------------------------


class StatisticsAgent(Agent):
    key = "statistics"
    name = "Raw statistics"
    short = "ST"
    focus = (
        "The numbers alone: P/E, PEG, EPS, market cap, margins, returns, debt, "
        "beta, the 52-week range — no story, no news, no opinions."
    )

    def role(self, kind):
        return (
            "You are a quantitative screener. Your only question is whether the "
            "NUMBERS make this stock worth buying at today's price. You are "
            "given, per ticker: the current price and market cap, trailing and "
            "forward P/E, PEG, trailing and forward EPS, price/sales, "
            "price/book, EV/EBITDA and EV/revenue, gross, operating and profit "
            "margins, return on equity and assets, EBITDA, cash, debt, "
            "debt/equity, current ratio, free and operating cash flow, beta, "
            "the 52-week high and low, the 50- and 200-day averages, the "
            "one-year price change, dividend yield, short interest as a share "
            "of float, and institutional ownership.\n"
            "Read them TOGETHER, not as a checklist. A rich multiple is only a "
            "problem when margins and returns don't support it; a cheap one is "
            "only an opportunity when the balance sheet isn't the reason it's "
            "cheap. Leverage matters far more when free cash flow is thin. High "
            "short interest with weak returns on capital says something "
            "different from high short interest with 40% margins. A negative "
            "P/E means losses, not a bargain.\n"
            "Say which specific figures drove your score, with their values.\n"
            "You have NOT been given news, the company's story, analyst ratings "
            "or macro conditions. Do not reason about any of them, and do not "
            "invent a narrative to explain a number."
        )

    def data_note(self, kind):
        return (
            "Here are the statistics as JSON, one entry per ticker. Every field "
            "ending in '_pct' is already a percentage — do not multiply by 100. "
            "Cash, debt, revenue and cash-flow figures are absolute currency "
            "amounts. Multiples are trailing unless the name says forward. A "
            "missing field simply wasn't reported; don't guess at it, and don't "
            "treat its absence as a zero."
        )

    def payload(self, context, kind):
        rows = []
        for row in _rows(context, kind):
            stats = _pick(row.get("fundamentals"), _STATISTIC_FIELDS)
            if not stats:
                continue  # no figures — this agent has nothing to say
            rows.append({"ticker": row["ticker"], "price": row.get("price"), **stats})
        if not rows:
            return None
        return {"as_of": context.get("as_of"), "stocks": rows}


# --- 4. the street ------------------------------------------------------


class ExpertAgent(Agent):
    key = "expert"
    name = "Wall Street experts"
    short = "WS"
    focus = (
        "What the big firms conclude: consensus ratings, the bull/bear split, "
        "price targets, and who upgraded or downgraded lately."
    )

    def role(self, kind):
        return (
            "You are a sell-side research aggregator. Your only question is "
            "what the big financial firms — Goldman Sachs, JP Morgan, Morgan "
            "Stanley, Wells Fargo, Bank of America and the rest — currently "
            "conclude about each ticker, and how much that conclusion is worth. "
            "You are given, per ticker: the consensus rating and its 1-5 mean "
            "(1 = Strong Buy, 5 = Strong Sell), how many desks cover it, the "
            "full strong-buy-to-strong-sell head count, the mean, high and low "
            "price targets and what each implies from today's price, how many "
            "firms upgraded or downgraded recently, the individual firms' most "
            "recent calls with their targets, and a note summarising how far "
            "apart the desks are.\n"
            "Hold the street to these standards:\n"
            "  - Sell-side ratings are structurally bullish. Desks rarely "
            "publish sells, so 'Buy' sits closer to their neutral than to real "
            "enthusiasm. A consensus mean near 2.0 is ordinary; 2.5 or worse is "
            "unusually cool and should pull your score below 50.\n"
            "  - Disagreement matters more than the average. Forty desks quietly "
            "agreeing is a real signal; a wide bull/bear split, or targets "
            "spanning 100%+ of the mean, means the street has no idea either — "
            "score that closer to neutral and say why.\n"
            "  - Recent upgrades and downgrades carry more information than "
            "standing ratings, which go stale. A fresh downgrade against a "
            "Strong Buy consensus is worth more than the consensus.\n"
            "  - A mean target far above the price is a claim, not a fact. Ask "
            "what would have to go right, and discount targets the desks "
            "themselves clearly disagree on.\n"
            "You have NOT been given the fundamentals, the news, the price "
            "history or the investor's position. You cannot check the street "
            "against them, so don't pretend to — score what the desks say and "
            "how credible their agreement is."
        )

    def data_note(self, kind):
        return (
            "Here is the sell-side research as JSON, one entry per ticker. "
            "'mean' is the 1-5 consensus (lower = more bullish). 'bulls' / "
            "'neutral' / 'bears' are head counts. 'target.spread_pct' is how "
            "wide the target range is as a share of the mean target — the "
            "dispersion is the disagreement. 'firms' lists each desk's most "
            "recent call, newest first, with 'action_label' spelling out what "
            "it did. Tickers nobody covers are simply absent from this list."
        )

    def payload(self, context, kind):
        rows = []
        for row in _rows(context, kind):
            street = row.get("wall_street")
            if not street:
                continue  # uncovered by the street — no view to report
            rows.append(
                {"ticker": row["ticker"], "price": row.get("price"), "wall_street": street}
            )
        if not rows:
            return None
        return {"as_of": context.get("as_of"), "stocks": rows}


# --- 5. macro / everything else -----------------------------------------


class MacroAgent(Agent):
    key = "macro"
    name = "Macro & policy"
    short = "MA"
    focus = (
        "Everything that isn't about the company: rates, inflation, tariffs, "
        "war, energy, regulation and government policy."
    )

    def role(self, kind):
        return (
            "You are a macro strategist. Your only question is whether the "
            "WIDER ENVIRONMENT favours owning each ticker over the coming one "
            "to three months. You are given market-wide headlines that are "
            "deliberately NOT about any single company — interest rates and "
            "central-bank decisions, inflation and employment data, tariffs and "
            "trade policy, wars and geopolitical conflict, energy prices, "
            "regulation, taxes and government policy, and the general market "
            "outlook — plus, per ticker, only its sector, industry, beta and "
            "market cap.\n"
            "Do the transmission explicitly: name the macro development, name "
            "the channel, then name the sector it lands on. Tariffs on imported "
            "components hit hardware and autos, not domestic software. Falling "
            "rates help long-duration growth, leveraged balance sheets and "
            "housing, and compress bank net interest margins. A weaker dollar "
            "flatters overseas revenue. An oil spike helps energy and hurts "
            "airlines and shipping. War premia move defence, energy and "
            "insurance. Beta tells you how hard this name usually moves when "
            "the market as a whole does — a high-beta name in a favourable "
            "regime deserves a higher score than a low-beta one, and a worse "
            "score in a hostile one.\n"
            "Be honest about reach: if nothing in the headlines plausibly "
            "touches a sector, score that ticker near 50 and say the macro tape "
            "is silent on it. Most names, most months, genuinely are — a whole "
            "list scored 70 because the mood is good is not analysis.\n"
            "You have NOT been given any company-specific news, fundamentals, "
            "valuation, price history or analyst ratings. Do not reason about "
            "them, and do not guess at a company's business beyond its stated "
            "sector and industry."
        )

    def data_note(self, kind):
        return (
            "Here is the macro evidence as JSON. 'macro_news' is real "
            "market-wide headlines from the last two weeks, newest first, none "
            "of them tied to a specific holding — never invent others. 'stocks' "
            "gives only each ticker's sector, industry, beta (market "
            "sensitivity; 1.0 = moves with the market) and market cap. That is "
            "deliberately all you get about the companies."
        )

    def payload(self, context, kind):
        news = context.get("macro_news") or []
        if not news:
            return None  # no macro tape — nothing to reason from
        rows = []
        for row in _rows(context, kind):
            entry = _pick(row.get("fundamentals"), _MACRO_FIELDS)
            rows.append({"ticker": row["ticker"], **entry})
        if not rows:
            return None
        return {"as_of": context.get("as_of"), "macro_news": news, "stocks": rows}


# --- the roster ---------------------------------------------------------
#
# Order matters in two places, both cosmetic: it is the order the UI shows the
# agents in, and the order models are handed out in when several are
# configured. Nothing depends on there being exactly five.

AGENTS = (
    CompanyPerspectiveAgent(),
    PersonalAgent(),
    StatisticsAgent(),
    ExpertAgent(),
    MacroAgent(),
)

AGENT_KEYS = tuple(a.key for a in AGENTS)
AGENTS_BY_KEY = {a.key: a for a in AGENTS}


def describe_agents() -> list:
    """Agent metadata for the UI, in display order."""
    return [a.describe() for a in AGENTS]
