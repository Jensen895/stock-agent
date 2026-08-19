"""The three competitors: A, B and C.

What is deliberately identical
------------------------------
All three run the *same* five research agents from ``backend/ai_agents.py``,
over the *same* evidence, on the *same* candidate universe, at the *same* time
of day, with the *same* $5,000. Nothing about the data differs. This is the
whole design: if the three books end the month apart, the difference cannot be
attributed to one of them having seen a headline the others didn't.

What differs, and why only this
-------------------------------
Two things, both of which the app already models and neither of which is a
second scoring path:

  the weighting   Each competitor blends the five agents' 0-100 scores under
                  its own weights (``ai_weights.json``, the same file the UI's
                  sliders write). A stock the statistics agent hates and the
                  company agent loves is a different number to A than to B —
                  from identical inputs.

  the policy      What that number is allowed to do. The risk profile it acts
                  on, how high a score has to be before it buys at all, how
                  hard a qualifying score is backed, how many names it will
                  hold, how much of the book one name may become, and how much
                  cash it refuses to deploy whatever it is shown.

Read together those are a strategy, and at the end of thirty sessions the
question "which agent won" has an answer you can act on, because it decomposes
into "which weighting" and "which discipline".

Nothing here calls a model. A persona is data — five weights and eight numbers
— which is what makes the comparison honest and the harness cheap to re-run
with a fourth competitor.

Why these three
---------------
They are chosen to disagree, not to be plausible. A quant that reads only
multiples, a narrative trader that reads only the story, and a committee that
only moves when the desks and the numbers point the same way will take
different sides of the same tape, and a month in which all three do the same
thing is a month that tells you nothing.
"""

from backend.ai_agents import AGENT_KEYS

# Every competitor starts here. Small enough that position sizing is a real
# constraint — a single share of an expensive name is a meaningful slice of the
# book, which is a pressure the agents are told about explicitly.
STARTING_CASH = 5000.0

# The contest length, in trading sessions rather than calendar days: thirty
# decisions and thirty reports, with no dead weekend rows in the ledger.
SESSIONS = 30

# The shared candidate universe. Same twenty names in every competitor's
# wishlist on day one, so no agent gets a head start from a better list. Chosen
# for liquidity and for sector spread — mega-cap tech alone would make the
# macro agent's dimension nearly constant across the board and collapse three
# strategies into one bet on the Nasdaq.
#
# It is a starting point, not a fence: Discover adds whatever the market is
# actually talking about each day, and those names are scored by the same five
# agents and are as buyable as anything here.
UNIVERSE = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",   # tech
    "JPM", "GS", "V",                                          # financials
    "LLY", "UNH", "JNJ",                                       # health care
    "XOM", "CVX",                                              # energy
    "WMT", "KO", "PG",                                         # staples
    "CAT", "BA",                                               # industrials
)

# What the book is measured against. Buy-and-hold from day one, marked at the
# same closes. Three agents beating each other while all three trail an index
# fund is the most likely outcome of any exercise like this, and a leaderboard
# that can't show it isn't worth reading.
BENCHMARK = "SPY"


class Competitor:
    """One agent: a weighting, a policy, and the mandate it is told about.

    Immutable by intent — a competitor that re-tunes itself mid-contest is
    measuring something other than the strategy it started with, and the point
    of the exercise is to find out what thirty days of one discipline does.
    """

    def __init__(self, key, name, thesis, weights, risk, buy_floor,
                 full_conviction_at, max_buys, max_sells, max_position_pct,
                 cash_floor_pct, min_trade, consensus_required, style):
        self.key = key                      # "A" / "B" / "C"
        self.name = name                    # what the report calls it
        self.thesis = thesis                # one line, for the report header
        self.weights = dict(weights)        # the five agents, unnormalised
        self.risk = risk                    # which risk profile it acts on
        self.buy_floor = buy_floor          # score needed before it buys at all
        self.full_conviction_at = full_conviction_at  # score that earns a full slice
        self.max_buys = max_buys            # longest buy list it will act on
        self.max_sells = max_sells          # longest sell list it will act on
        self.max_position_pct = max_position_pct   # cap on one name, % of equity
        self.cash_floor_pct = cash_floor_pct       # % of equity never deployed
        self.min_trade = min_trade          # smallest order worth a ticket, $
        # Which ``consensus`` labels a BUY may carry. The blend already reports
        # whether the five agents agreed, were mixed, or split; a competitor
        # that only acts on agreement is a real and testable discipline, and
        # None means "act on any of them".
        self.consensus_required = (
            tuple(consensus_required) if consensus_required else None
        )
        self.style = style                  # the remit, in the agents' words

    # --- what the report shows -------------------------------------------

    def policy_summary(self) -> str:
        """The policy as one readable line, so a reader of day 17's report
        doesn't have to go and find the source to know why A bought and C
        didn't."""
        parts = [
            f"{self.risk}-risk profile",
            f"buys at {self.buy_floor:g}+",
            f"full size at {self.full_conviction_at:g}",
            f"max {self.max_buys} buys / {self.max_sells} sells a day",
            f"one name capped at {self.max_position_pct:g}% of the book",
            f"keeps {self.cash_floor_pct:g}% in cash",
        ]
        if self.consensus_required:
            parts.append("only buys when " + " or ".join(self.consensus_required))
        return ", ".join(parts)

    def weights_summary(self) -> str:
        """The weighting, heaviest first."""
        ordered = sorted(self.weights.items(), key=lambda kv: -kv[1])
        return ", ".join(f"{k.replace('_', ' ')} x{v:g}" for k, v in ordered)

    # --- what the agents are told ----------------------------------------

    def mandate(self, day: int, sessions_left: int, equity: float,
                cash: float) -> str:
        """The standing instruction for one day, handed to all five agents.

        Four facts and a remit. The facts are what make a 0-100 score mean
        something here rather than in the app it was designed for: a call that
        needs a quarter to play out is not worth the same to a book that is
        marked to market in ``sessions_left`` sessions, and an agent that
        doesn't know the deadline will keep pricing quarters.

        The remit is this competitor's own, and it is the only line of the five
        prompts that differs between A, B and C. It is not evidence and it does
        not narrow anyone's dimension — ``ai_agents.mandate_note`` wraps it in
        the rules that say so.
        """
        weeks = max(1, round(sessions_left / 5))
        return (
            f"You are advising portfolio {self.key} ({self.name}) in a "
            f"three-way, thirty-session contest between three agents running "
            f"the same research under different convictions.\n"
            f"  - Day {day} of {SESSIONS}. {sessions_left} trading sessions "
            f"remain — roughly {weeks} week{'s' if weeks != 1 else ''}.\n"
            f"  - The book started at ${STARTING_CASH:,.0f} and is worth "
            f"${equity:,.2f} today, of which ${cash:,.2f} is cash.\n"
            f"  - It is marked to market at the final close. There is no "
            f"credit for a thesis that is still right and not yet paid; a "
            f"position that needs two quarters to work is, for this book, a "
            f"position that does not work.\n"
            f"  - Nothing is forced to be invested. Finishing in cash and "
            f"ahead beats finishing invested and behind.\n"
            f"This portfolio's remit: {self.style}"
        )

    def describe(self) -> dict:
        """The persona as plain data, for the ledger and the report."""
        return {
            "key": self.key,
            "name": self.name,
            "thesis": self.thesis,
            "weights": dict(self.weights),
            "risk": self.risk,
            "buy_floor": self.buy_floor,
            "full_conviction_at": self.full_conviction_at,
            "max_buys": self.max_buys,
            "max_sells": self.max_sells,
            "max_position_pct": self.max_position_pct,
            "cash_floor_pct": self.cash_floor_pct,
            "min_trade": self.min_trade,
            "consensus_required": list(self.consensus_required or ()),
            "style": self.style,
        }


COMPETITORS = (
    Competitor(
        key="A",
        name="The Quant",
        thesis="Trusts the multiples and the balance sheet; distrusts the story.",
        # Statistics carries the blend, with the desks as a sanity check. The
        # personal agent is nearly silenced on purpose: with a book that is one
        # day old there is no trading history in the name for it to read, so a
        # full weight on it would mostly weight the price chart.
        weights={
            "statistics": 2.5,
            "expert": 1.5,
            "company_perspective": 1.0,
            "macro": 0.5,
            "personal": 0.5,
        },
        risk="low",
        buy_floor=58.0,
        full_conviction_at=82.0,
        max_buys=4,
        max_sells=3,
        max_position_pct=30.0,
        cash_floor_pct=10.0,
        min_trade=50.0,
        consensus_required=None,
        style=(
            "act on valuation and balance-sheet evidence. A cheap, solvent, "
            "profitable business is worth owning through noise; an expensive "
            "one is not rescued by a good headline. Prefer being early and "
            "right about a number to being on time and right about a story."
        ),
    ),
    Competitor(
        key="B",
        name="The Narrative Trader",
        thesis="Buys the story and the tape; will pay up for momentum.",
        # The mirror image of A: the company's own news and the macro backdrop
        # carry the blend, and the multiples are almost silenced. This is the
        # competitor most likely to be badly wrong, which is the point of
        # having it.
        weights={
            "company_perspective": 2.5,
            "macro": 1.5,
            "personal": 1.0,
            "expert": 0.5,
            "statistics": 0.5,
        },
        risk="high",
        buy_floor=55.0,
        full_conviction_at=78.0,
        max_buys=5,
        max_sells=3,
        max_position_pct=40.0,
        cash_floor_pct=0.0,
        min_trade=50.0,
        consensus_required=None,
        style=(
            "act on what is happening to the business and to the market right "
            "now. A catalyst inside the next few weeks is worth more than a "
            "multiple that has been cheap for a year. Volatility is the cost "
            "of being in the right name at the right time, not a reason to "
            "stay out — but a story that has already been told and paid for is "
            "not a story."
        ),
    ),
    Competitor(
        key="C",
        name="The Committee",
        thesis="Moves only when the five agents broadly agree; hoards cash otherwise.",
        # Almost flat weights, tilted to the desks. The discipline is not in
        # the blend but in the policy: the highest bar of the three, the
        # shortest list, the tightest position cap, and a hard consensus
        # requirement, so it declines far more often than it acts.
        weights={
            "expert": 1.5,
            "company_perspective": 1.0,
            "statistics": 1.0,
            "macro": 1.0,
            "personal": 1.0,
        },
        risk="low",
        buy_floor=62.0,
        full_conviction_at=85.0,
        max_buys=3,
        max_sells=2,
        max_position_pct=25.0,
        cash_floor_pct=20.0,
        min_trade=75.0,
        consensus_required=("agree", "mixed"),
        style=(
            "act only on conviction that survives every lens. Where the "
            "evidence is thin or the dimensions point different ways, the "
            "answer is to wait and be paid for waiting. Over thirty sessions "
            "the losses avoided are expected to matter more than the gains "
            "missed, and finishing flat is an acceptable outcome."
        ),
    ),
)

BY_KEY = {c.key: c for c in COMPETITORS}


def validate() -> None:
    """Fail loudly at import time on a persona that can't be run.

    A weight keyed to an agent that doesn't exist is silently dropped by
    ``normalize_weights``, which would leave a competitor quietly running the
    default blend and a month of results that mean nothing. Cheap to check, and
    the only moment it can be caught before the data is worthless.
    """
    for competitor in COMPETITORS:
        unknown = set(competitor.weights) - set(AGENT_KEYS)
        if unknown:
            raise ValueError(
                f"Competitor {competitor.key} weights unknown agents: "
                f"{', '.join(sorted(unknown))}. Valid keys: "
                f"{', '.join(AGENT_KEYS)}."
            )
        missing = set(AGENT_KEYS) - set(competitor.weights)
        if missing:
            raise ValueError(
                f"Competitor {competitor.key} is missing weights for: "
                f"{', '.join(sorted(missing))}."
            )
        if competitor.risk not in ("low", "high"):
            raise ValueError(f"Competitor {competitor.key} has a bad risk profile.")


validate()
