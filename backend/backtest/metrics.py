"""The arithmetic: does an agent's score have anything to do with what happened?

Everything here is deterministic and computed in Python, never by a model. The
model's job (``analyst.py``) is to interpret these numbers and to say what they
mean for what to do next; it is not to produce them, because a number a language
model made up is worse than no number at all.

The measurement, and why it is done this way
--------------------------------------------
The obvious approach — pool every (score, subsequent return) pair and correlate
them — is wrong here in two ways that both push the answer toward "it works".

*The market moves everything at once.* On a day the S&P gains 2%, almost every
holding is up, so any agent that scored anything above 50 looks prescient.
Correlating **within a single day, across tickers**, removes the market entirely:
the question becomes "on this day, did the agent rank the stocks in the order
they subsequently performed", which is the only question a score is capable of
answering. That per-day rank correlation is the **information coefficient**, and
averaging it over days with a t-test on the series is the Fama-MacBeth procedure
that cross-sectional research has used for fifty years.

*Consecutive predictions overlap.* A 21-day return measured on Monday and one
measured on Tuesday share twenty of their twenty-one days. Two hundred such
observations are nowhere near two hundred independent facts, and treating them
as if they were is how back-tests produce p-values of 0.001 for signals that are
worth nothing. So every significance figure here is computed against an
**effective sample size** — roughly the number of non-overlapping windows the
data actually spans — and the naive figure is reported alongside it so the size
of the haircut is visible rather than hidden.

What gets measured
------------------
For each agent, and for the blended score, at each horizon:

    mean IC            average per-day rank correlation with the forward return
    t / p              significance of that average, on the effective sample
    hit rate           of the names it called (>55 or <45), how many moved the
                       way it said, measured against SPY rather than against zero
    long-short         return of its top third minus its bottom third, per day
    dispersion         how far apart it spreads its scores at all — an agent
                       that answers 50 to everything cannot predict anything,
                       and no weight can fix that
    bias               its average score, which says whether it is capable of
                       being bearish

Two comparisons decide whether any of it matters:

    momentum           the same measurement applied to "what did this stock do
                       last month", a signal that costs nothing and calls no
                       model. An agent that cannot beat it is not earning its
                       twenty API calls a day.
    agent correlation  how much the five agents' scores actually resemble each
                       other. The app's central claim is that they see disjoint
                       evidence and therefore make uncorrelated errors. If two
                       of them correlate at 0.8, that claim is false for those
                       two and the average is not doing what it is supposed to.

And the weights: a coordinate search for the weighting that maximises mean IC,
run twice on interleaved halves of the days so the answer can be scored **out of
sample**. In-sample weights always look good — there are five free parameters
and rarely more than a few dozen days — so the in-sample number is reported only
as the thing the out-of-sample number should be compared against. When the two
disagree, the honest reading is that the weights are fitted noise, and the
verdict says so.
"""

import math
from datetime import date as _date

from backend.ai_agents import AGENT_KEYS

from .outcomes import HORIZONS, MOMENTUM_LOOKBACK

# A cross-section needs enough names for a rank correlation to mean anything.
# Below this a single ticker moves the whole coefficient, and averaging such
# days in adds noise with the same weight as a real reading.
MIN_CROSS_SECTION = 5

# Fewer days than this and there is no time series to test — the command still
# reports the descriptive numbers, but every significance claim is suppressed
# rather than computed on three points and quietly believed.
MIN_DAYS_FOR_INFERENCE = 8

# Weights the optimiser is allowed to consider, matching the range the UI
# sliders offer. Zero is included deliberately: "switch this agent off" has to
# be an available answer, because sometimes it is the right one.
WEIGHT_GRID = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0)

# Roughly how many calendar days a trading day spans. Used only to convert an
# overlap window into a span, for the effective-sample haircut.
_CALENDAR_PER_TRADING_DAY = 7.0 / 5.0

# Score thresholds the app itself uses to turn a number into an action. Hit
# rates are measured against these so the statistic describes the calls the
# investor was actually shown, not an arbitrary cut of the distribution.
_BUY_ABOVE = 55.0
_SELL_BELOW = 45.0

MOMENTUM_KEY = f"momentum_{MOMENTUM_LOOKBACK}d"


# --- small statistics ---------------------------------------------------


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _std(values, sample: bool = True):
    values = [v for v in values if v is not None]
    n = len(values)
    if n < (2 if sample else 1):
        return None
    mu = sum(values) / n
    total = sum((v - mu) ** 2 for v in values)
    return math.sqrt(total / (n - 1 if sample else n))


def _ranks(values):
    """Average ranks, so ties don't invent an ordering that isn't there."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sxx = syy = 0.0
    for x, y in zip(xs, ys):
        dx, dy = x - mx, y - my
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0 or syy <= 0:
        return None  # one side is constant — no correlation is defined
    return sxy / math.sqrt(sxx * syy)


def _spearman(xs, ys):
    if len(xs) < 3:
        return None
    return _pearson(_ranks(xs), _ranks(ys))


def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def _betai(a, b, x):
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_p_value(t, df):
    """Two-sided p-value for a t statistic. Exact, not a normal approximation —
    these samples are small enough that the difference changes conclusions."""
    if t is None or df is None or df < 1:
        return None
    t = abs(float(t))
    return round(_betai(df / 2.0, 0.5, df / (df + t * t)), 5)


def _days_between(first: str, last: str) -> int:
    try:
        a = _date.fromisoformat(first)
        b = _date.fromisoformat(last)
    except (TypeError, ValueError):
        return 0
    return max(0, (b - a).days)


# --- observations -------------------------------------------------------


def observations(doc: dict, horizon: int, risk=None, kinds=None,
                 scopes=None) -> list:
    """Flatten the ledger into scored (prediction, outcome) pairs.

    ``risk``, ``kinds`` and ``scopes`` narrow the cohort. Narrowing is not free
    — every filter costs statistical power the ledger may not have to spare —
    so the default is everything, and the report only splits a cohort out when
    there are enough days in it to say something.
    """
    key = str(horizon)
    out = []
    for snapshot in doc.get("snapshots") or []:
        if scopes and snapshot.get("scope") not in scopes:
            continue
        for row in snapshot.get("rows") or []:
            if risk and row.get("risk") != risk:
                continue
            if kinds and row.get("kind") not in kinds:
                continue
            outcome = (row.get("outcomes") or {}).get(key)
            if not outcome:
                continue
            ret = outcome.get("ret_pct")
            if ret is None:
                continue
            excess = outcome.get("excess_pct")
            scores = {k: v for k, v in (row.get("scores") or {}).items()
                      if isinstance(v, (int, float))}
            if not scores:
                continue
            out.append({
                # Grouped on the anchor, not the generation date: two snapshots
                # that were both priced off Monday's close are one cross-section,
                # and counting them as two would double-weight that Monday.
                "date": row.get("anchor") or snapshot.get("date"),
                "scope": snapshot.get("scope"),
                "ticker": row.get("ticker"),
                "kind": row.get("kind"),
                "risk": row.get("risk"),
                "scores": scores,
                "blended": row.get("blended"),
                "equal": row.get("equal"),
                "momentum": row.get("momentum_pct"),
                "ret": ret,
                # Excess over SPY where we have it, the raw return where we
                # don't. Which one was used is reported, because "beat the
                # market" and "went up" are different claims.
                "target": excess if excess is not None else ret,
                "excess_available": excess is not None,
            })
    return out


def cross_sections(obs: list, min_names: int = MIN_CROSS_SECTION) -> list:
    """The observations grouped into one list per day, days too thin dropped."""
    by_date = {}
    for o in obs:
        if not o["date"]:
            continue
        by_date.setdefault(o["date"], []).append(o)
    return [
        {"date": d, "rows": rows}
        for d, rows in sorted(by_date.items())
        if len(rows) >= min_names
    ]


# --- Fama-MacBeth ------------------------------------------------------


def _ic_series(sections: list, value_of, min_names: int = MIN_CROSS_SECTION):
    """Per-day rank correlation between a signal and the forward return.

    ``value_of(row)`` returns the signal for one observation, or None when that
    signal has nothing to say about that ticker on that day (an agent with no
    evidence, a stock too new to have a momentum reading). Days where the signal
    is constant are dropped — an agent that scored every name 50 that day has
    produced no ordering, and calling that a correlation of zero would be a
    reading it never made.
    """
    series = []
    for section in sections:
        xs, ys = [], []
        for row in section["rows"]:
            value = value_of(row)
            if value is None:
                continue
            xs.append(float(value))
            ys.append(float(row["target"]))
        if len(xs) < min_names:
            continue
        ic = _spearman(xs, ys)
        if ic is None:
            continue
        series.append({"date": section["date"], "ic": round(ic, 4), "n": len(xs)})
    return series


def _effective_n(series: list, horizon: int):
    """How many genuinely independent readings the IC series is worth.

    Consecutive days share almost all of a multi-day return window, so the
    series is far less informative than its length suggests. This counts the
    non-overlapping windows the observed span contains — the span in calendar
    days divided by the horizon's calendar length — and never returns more than
    the number of days actually observed.

    It is an approximation, and a deliberately harsh one. Being wrong in this
    direction costs a true signal some significance; being wrong in the other
    direction manufactures signals that are not there.
    """
    n = len(series)
    if n == 0:
        return 0
    span = _days_between(series[0]["date"], series[-1]["date"])
    windows = int(span / (horizon * _CALENDAR_PER_TRADING_DAY)) + 1
    return max(1, min(n, windows))


def _summarise_ic(series: list, horizon: int) -> dict:
    """Mean IC with both the naive and the overlap-adjusted significance."""
    ics = [s["ic"] for s in series]
    mean = _mean(ics)
    std = _std(ics)
    n = len(ics)
    n_eff = _effective_n(series, horizon)
    out = {
        "days": n,
        "effective_days": n_eff,
        "mean_ic": round(mean, 4) if mean is not None else None,
        "ic_std": round(std, 4) if std is not None else None,
        "positive_days": sum(1 for i in ics if i > 0),
        "mean_names_per_day": round(_mean([s["n"] for s in series]) or 0, 1),
        "t_stat": None,
        "p_value": None,
        "t_stat_naive": None,
        "significant": False,
        "series": series,
    }
    if mean is None or std in (None, 0) or n < 2:
        return out
    out["t_stat_naive"] = round(mean / (std / math.sqrt(n)), 3)
    if n_eff >= 2:
        t = mean / (std / math.sqrt(n_eff))
        out["t_stat"] = round(t, 3)
        out["p_value"] = _t_p_value(t, n_eff - 1)
        out["significant"] = bool(
            out["p_value"] is not None
            and out["p_value"] < 0.05
            and n >= MIN_DAYS_FOR_INFERENCE
        )
    return out


# --- per-signal statistics ---------------------------------------------


def _hit_rates(obs: list, value_of, thresholds=None) -> dict:
    """How often a signal's actual calls went the way it said.

    Split by the app's own action bands, so this measures the advice as it was
    displayed. Measured against the benchmark where possible: "the stock went
    up" is not a win when everything went up, and a portfolio of large-cap tech
    in 2026 would score a spectacular hit rate on a signal that is pure noise.

    ``thresholds`` overrides the bands for signals that do not live on the
    0-100 scale — the momentum baseline is a percentage return, where the
    bullish/bearish cut is zero, not 55.
    """
    buy_above, sell_below = thresholds or (_BUY_ABOVE, _SELL_BELOW)
    buys = [o for o in obs
            if value_of(o) is not None and value_of(o) > buy_above]
    sells = [o for o in obs
             if value_of(o) is not None and value_of(o) < sell_below]
    out = {
        "buy_calls": len(buys),
        "sell_calls": len(sells),
        "buy_hit_pct": None,
        "sell_hit_pct": None,
        "buy_mean_excess_pct": None,
        "sell_mean_excess_pct": None,
    }
    if buys:
        out["buy_hit_pct"] = round(
            100.0 * sum(1 for o in buys if o["target"] > 0) / len(buys), 1
        )
        out["buy_mean_excess_pct"] = round(_mean([o["target"] for o in buys]), 3)
    if sells:
        out["sell_hit_pct"] = round(
            100.0 * sum(1 for o in sells if o["target"] < 0) / len(sells), 1
        )
        out["sell_mean_excess_pct"] = round(_mean([o["target"] for o in sells]), 3)
    return out


def _long_short(sections: list, value_of, horizon: int) -> dict:
    """Top third minus bottom third, per day, then averaged.

    The tradeable version of the IC: a rank correlation is a statistic, this is
    a number of percentage points. Thirds rather than deciles because these
    cross-sections are twenty to sixty names, and a decile of forty names is
    four stocks.
    """
    spreads = []
    for section in sections:
        scored = [
            (value_of(r), r["target"]) for r in section["rows"] if value_of(r) is not None
        ]
        if len(scored) < 6:
            continue
        scored.sort(key=lambda p: p[0])
        cut = max(1, len(scored) // 3)
        bottom = _mean([t for _, t in scored[:cut]])
        top = _mean([t for _, t in scored[-cut:]])
        if bottom is None or top is None:
            continue
        spreads.append({"date": section["date"], "ic": top - bottom, "n": len(scored)})
    if not spreads:
        return {"days": 0, "mean_pct": None, "t_stat": None, "p_value": None}
    values = [s["ic"] for s in spreads]
    mean, std = _mean(values), _std(values)
    n_eff = _effective_n(spreads, horizon)
    out = {
        "days": len(values),
        "effective_days": n_eff,
        "mean_pct": round(mean, 3),
        "positive_days": sum(1 for v in values if v > 0),
        "t_stat": None,
        "p_value": None,
    }
    if std not in (None, 0) and n_eff >= 2:
        t = mean / (std / math.sqrt(n_eff))
        out["t_stat"] = round(t, 3)
        out["p_value"] = _t_p_value(t, n_eff - 1)
    return out


def _dispersion(sections: list, value_of) -> dict:
    """How widely a signal spreads its scores, and where it centres them.

    An agent whose cross-sectional standard deviation is near zero has told you
    nothing regardless of what its correlation happens to come out at, and no
    weight the optimiser can find will change that — scaling a flat line leaves
    a flat line. Bias is the other half: an agent averaging 68 has effectively
    no sell setting, so half the scale it was given is unused.
    """
    stds, means, values = [], [], []
    for section in sections:
        xs = [value_of(r) for r in section["rows"]]
        xs = [float(x) for x in xs if x is not None]
        if len(xs) < 3:
            continue
        stds.append(_std(xs))
        means.append(_mean(xs))
        values.extend(xs)
    return {
        "cross_sectional_std": round(_mean(stds), 2) if stds else None,
        "mean_score": round(_mean(means), 2) if means else None,
        "min_score": round(min(values), 1) if values else None,
        "max_score": round(max(values), 1) if values else None,
    }


def _coverage(obs: list, key: str) -> dict:
    scored = sum(1 for o in obs if key in o["scores"])
    return {
        "scored_rows": scored,
        "total_rows": len(obs),
        "coverage_pct": round(100.0 * scored / len(obs), 1) if obs else None,
    }


def signal_stats(obs: list, sections: list, value_of, horizon: int,
                 scale: str = "score") -> dict:
    """Every statistic, for one signal, at one horizon.

    ``scale`` says what the signal's units are. Everything the agents produce is
    on the shared 0-100 confidence scale; the momentum baseline is a percentage
    return, and reading a 55 threshold against that would define a "buy call"
    as a stock that had already risen 55% in a month.
    """
    series = _ic_series(sections, value_of)
    thresholds = (0.0, 0.0) if scale == "return" else None
    return {
        "scale": scale,
        "ic": _summarise_ic(series, horizon),
        "hits": _hit_rates(obs, value_of, thresholds),
        "long_short": _long_short(sections, value_of, horizon),
        "dispersion": _dispersion(sections, value_of),
    }


# --- how alike are the five? -------------------------------------------


def agent_correlations(sections: list) -> dict:
    """Per-day rank correlation between each pair of agents, averaged.

    This tests the premise the whole app rests on. ``ai_agents.py`` argues that
    averaging five opinions only buys anything when their errors are
    uncorrelated, and enforces that by handing each agent a disjoint slice of
    the evidence. Whether the *scores* then come out uncorrelated is an
    empirical question nobody has checked, and if two agents agree at 0.8 the
    average is closer to a three-agent average than a five-agent one.
    """
    pairs = {}
    for a_index, a in enumerate(AGENT_KEYS):
        for b in AGENT_KEYS[a_index + 1:]:
            values = []
            for section in sections:
                xs, ys = [], []
                for row in section["rows"]:
                    if a in row["scores"] and b in row["scores"]:
                        xs.append(row["scores"][a])
                        ys.append(row["scores"][b])
                if len(xs) < MIN_CROSS_SECTION:
                    continue
                rho = _spearman(xs, ys)
                if rho is not None:
                    values.append(rho)
            if values:
                pairs[f"{a}|{b}"] = {
                    "mean_rho": round(_mean(values), 3),
                    "days": len(values),
                }
    return pairs


def momentum_alignment(sections: list) -> dict:
    """How much each agent's score is just last month's price move restated.

    An agent that correlates 0.7 with trailing momentum is, whatever its stated
    dimension, mostly re-deriving a number the ledger already has for free. That
    is worth knowing even when the agent's IC is good — especially then, because
    the IC is then momentum's, not the agent's.
    """
    out = {}
    for key in AGENT_KEYS:
        values = []
        for section in sections:
            xs, ys = [], []
            for row in section["rows"]:
                if key in row["scores"] and row.get("momentum") is not None:
                    xs.append(row["scores"][key])
                    ys.append(row["momentum"])
            if len(xs) < MIN_CROSS_SECTION:
                continue
            rho = _spearman(xs, ys)
            if rho is not None:
                values.append(rho)
        if values:
            out[key] = {"mean_rho": round(_mean(values), 3), "days": len(values)}
    return out


# --- the weight search --------------------------------------------------


def blend(scores: dict, weights: dict):
    """The app's own weighted average, reproduced exactly.

    Renormalises over the agents that actually answered, so a missing agent
    dilutes nobody — the same rule ``ai_advisor.blend_confidence`` follows. Any
    weighting the optimiser proposes therefore means the same thing here as it
    would mean in the sliders.
    """
    total = weight_sum = 0.0
    for key, score in scores.items():
        w = weights.get(key, 0.0)
        if w <= 0:
            continue
        total += score * w
        weight_sum += w
    if weight_sum <= 0:
        return None
    return total / weight_sum


def _prepared(sections: list):
    """Pre-rank each day's returns once, since only the scores change."""
    prepared = []
    for section in sections:
        rows = [r for r in section["rows"] if r["scores"]]
        if len(rows) < MIN_CROSS_SECTION:
            continue
        prepared.append({
            "date": section["date"],
            "scores": [r["scores"] for r in rows],
            "target_ranks": _ranks([r["target"] for r in rows]),
        })
    return prepared


def _mean_ic_for(prepared: list, weights: dict):
    """Mean per-day IC of the blended score under one weighting."""
    ics = []
    for day in prepared:
        blended, ranks = [], []
        for scores, rank in zip(day["scores"], day["target_ranks"]):
            value = blend(scores, weights)
            if value is None:
                continue
            blended.append(value)
            ranks.append(rank)
        if len(blended) < MIN_CROSS_SECTION:
            continue
        ic = _pearson(_ranks(blended), ranks)
        if ic is not None:
            ics.append(ic)
    if not ics:
        return None, 0
    return sum(ics) / len(ics), len(ics)


def _coordinate_search(prepared: list, starts: list):
    """Hill-climb the weight grid one agent at a time, from several starts.

    Deterministic on purpose — no random restarts — so re-running the command
    on unchanged data produces the identical recommendation. A weighting that
    moves because the optimiser rolled differently is indistinguishable from one
    that moves because the market did, and only one of those is worth reading.
    """
    best_weights, best_score = None, None
    for start in starts:
        weights = dict(start)
        score, _ = _mean_ic_for(prepared, weights)
        if score is None:
            continue
        for _ in range(12):
            improved = False
            for key in AGENT_KEYS:
                current = weights[key]
                for candidate in WEIGHT_GRID:
                    if candidate == current:
                        continue
                    trial = dict(weights, **{key: candidate})
                    if not any(trial.values()):
                        continue
                    value, _ = _mean_ic_for(prepared, trial)
                    if value is not None and value > score + 1e-9:
                        score, weights, current = value, trial, candidate
                        improved = True
            if not improved:
                break
        if best_score is None or score > best_score:
            best_weights, best_score = weights, score
    return best_weights, best_score


def _starts(current: dict) -> list:
    """Where the hill-climb begins: equal, the live weights, and each agent alone.

    The solo starts matter — the surface has local maxima, and starting from
    equal weights alone tends to find a mild reweighting of all five when the
    truth is sometimes "one of them is the whole signal".
    """
    equal = {k: 1.0 for k in AGENT_KEYS}
    starts = [equal, {k: float(current.get(k, 1.0)) for k in AGENT_KEYS}]
    for key in AGENT_KEYS:
        starts.append({k: (1.0 if k == key else 0.0) for k in AGENT_KEYS})
    return starts


def optimise_weights(sections: list, current: dict, horizon: int) -> dict:
    """Search for the best weighting, then check whether it survives a split.

    The split is interleaved (odd days against even days) rather than
    chronological. A chronological split fits the weights on one market regime
    and tests them on another, and then reports the regime change as
    over-fitting; interleaving holds the regime roughly constant so what is left
    is the thing being tested — whether the weighting describes a relationship
    or just the particular days it was fitted on.

    ``verdict`` is deliberately blunt. Five free parameters over a few dozen
    days will always produce a flattering in-sample number, and the only useful
    question is whether the out-of-sample one is still there.
    """
    prepared = _prepared(sections)
    equal = {k: 1.0 for k in AGENT_KEYS}
    current = {k: float(current.get(k, 1.0)) for k in AGENT_KEYS}

    equal_ic, _ = _mean_ic_for(prepared, equal)
    current_ic, _ = _mean_ic_for(prepared, current)
    out = {
        "days": len(prepared),
        "equal_weight_ic": round(equal_ic, 4) if equal_ic is not None else None,
        "current_weight_ic": round(current_ic, 4) if current_ic is not None else None,
        "current_weights": current,
        "best_weights": None,
        "in_sample_ic": None,
        "out_of_sample_ic": None,
        "folds": [],
        "verdict": "insufficient-data",
    }
    if len(prepared) < 4:
        return out

    best, score = _coordinate_search(prepared, _starts(current))
    if best is None:
        return out
    out["best_weights"] = {k: round(v, 2) for k, v in best.items()}
    out["in_sample_ic"] = round(score, 4)

    if len(prepared) < MIN_DAYS_FOR_INFERENCE:
        out["verdict"] = "too-few-days-to-validate"
        return out

    folds = []
    even = [d for i, d in enumerate(prepared) if i % 2 == 0]
    odd = [d for i, d in enumerate(prepared) if i % 2 == 1]
    for name, train, test in (("even->odd", even, odd), ("odd->even", odd, even)):
        weights, fitted = _coordinate_search(train, _starts(current))
        if weights is None:
            continue
        held_out, days = _mean_ic_for(test, weights)
        folds.append({
            "fold": name,
            "weights": {k: round(v, 2) for k, v in weights.items()},
            "train_ic": round(fitted, 4),
            "test_ic": round(held_out, 4) if held_out is not None else None,
            "test_days": days,
        })
    out["folds"] = folds

    tested = [f["test_ic"] for f in folds if f["test_ic"] is not None]
    if not tested:
        return out
    oos = sum(tested) / len(tested)
    out["out_of_sample_ic"] = round(oos, 4)

    baseline = equal_ic if equal_ic is not None else 0.0
    if oos <= 0.0:
        out["verdict"] = "weights-are-noise"
    elif oos <= baseline + 0.01:
        out["verdict"] = "no-better-than-equal-weights"
    elif oos < out["in_sample_ic"] / 2.0:
        out["verdict"] = "largely-overfit"
    else:
        out["verdict"] = "holds-up-out-of-sample"
    return out


# --- assembling one horizon --------------------------------------------


def _cohorts(doc: dict, horizon: int) -> dict:
    """Per-risk and per-kind breakdowns, but only where there is data to spare.

    Every split multiplies the number of numbers in the report and divides the
    evidence behind each one. These are reported as descriptive context, not as
    findings, and the report says so.
    """
    out = {}
    for label, kwargs in (
        ("risk:low", {"risk": "low"}),
        ("risk:high", {"risk": "high"}),
        ("kind:holdings", {"kinds": ("holdings",)}),
        ("kind:wishlist", {"kinds": ("wishlist",)}),
        ("kind:discover", {"kinds": ("discover",)}),
    ):
        obs = observations(doc, horizon, **kwargs)
        sections = cross_sections(obs)
        if len(sections) < 3:
            continue
        blended = signal_stats(obs, sections, lambda r: r.get("equal"), horizon)
        out[label] = {
            "days": len(sections),
            "rows": len(obs),
            "equal_blend_ic": blended["ic"]["mean_ic"],
            "equal_blend_p": blended["ic"]["p_value"],
        }
    return out


def compute_horizon(doc: dict, horizon: int, current_weights: dict) -> dict:
    """Everything measurable at one horizon, over the whole ledger."""
    obs = observations(doc, horizon)
    sections = cross_sections(obs)
    result = {
        "horizon_days": horizon,
        "rows": len(obs),
        "days": len(sections),
        "dates": [s["date"] for s in sections],
        "tickers": len({o["ticker"] for o in obs}),
        "benchmark_relative": bool(obs) and all(o["excess_available"] for o in obs),
        "agents": {},
        "blended": {},
        "baselines": {},
        "agent_correlations": {},
        "momentum_alignment": {},
        "weights": {},
        "cohorts": {},
    }
    if not sections:
        return result

    for key in AGENT_KEYS:
        result["agents"][key] = {
            **signal_stats(obs, sections, lambda r, k=key: r["scores"].get(k), horizon),
            "coverage": _coverage(obs, key),
        }

    # Two blends: what the app displayed under the day's weights, and the plain
    # average. They differ, and which one is better is itself informative — if
    # equal weighting wins, the sliders have been actively harmful.
    result["blended"]["as_shown"] = signal_stats(
        obs, sections, lambda r: r.get("blended"), horizon
    )
    result["blended"]["equal_weight"] = signal_stats(
        obs, sections, lambda r: r.get("equal"), horizon
    )

    # The bar. A signal that costs nothing, calls no model, and is already
    # sitting in the ledger.
    result["baselines"][MOMENTUM_KEY] = signal_stats(
        obs, sections, lambda r: r.get("momentum"), horizon, scale="return"
    )

    result["agent_correlations"] = agent_correlations(sections)
    result["momentum_alignment"] = momentum_alignment(sections)
    result["weights"] = optimise_weights(sections, current_weights, horizon)
    result["cohorts"] = _cohorts(doc, horizon)
    return result


# --- the headline judgement --------------------------------------------


def _rank_agents(horizon_result: dict) -> list:
    ranked = []
    for key, stats in (horizon_result.get("agents") or {}).items():
        ic = stats["ic"]
        ranked.append({
            "agent": key,
            "mean_ic": ic["mean_ic"],
            "t_stat": ic["t_stat"],
            "p_value": ic["p_value"],
            "significant": ic["significant"],
            "dispersion": stats["dispersion"]["cross_sectional_std"],
            "mean_score": stats["dispersion"]["mean_score"],
            "long_short_pct": stats["long_short"]["mean_pct"],
        })
    ranked.sort(key=lambda r: (r["mean_ic"] is None, -(r["mean_ic"] or 0)))
    return ranked


def verdict(horizon_result: dict) -> dict:
    """The unhedged summary the report has to lead with.

    Three of the four outcomes are negative, which is the correct prior. A
    handful of weeks of daily scores on twenty-odd correlated large-caps is not
    enough to establish that a signal works, and the most likely honest answer
    for a long time will be "not enough data yet" followed by "no evidence".
    """
    days = horizon_result.get("days") or 0
    ranked = _rank_agents(horizon_result)
    blend_ic = ((horizon_result.get("blended") or {}).get("equal_weight") or {}).get("ic") or {}
    weights = horizon_result.get("weights") or {}
    flags = []

    if days < MIN_DAYS_FOR_INFERENCE:
        state = "insufficient-data"
        headline = (
            f"{days} scored day(s) at this horizon — too few to test anything. "
            f"Descriptive numbers only until there are {MIN_DAYS_FOR_INFERENCE}."
        )
    else:
        winners = [r for r in ranked if r["significant"] and (r["mean_ic"] or 0) > 0]
        losers = [r for r in ranked if r["significant"] and (r["mean_ic"] or 0) < 0]
        if winners:
            state = "signal-found"
            headline = (
                "Significant positive rank correlation from "
                + ", ".join(w["agent"] for w in winners)
                + "."
            )
        elif losers:
            state = "inverted-signal"
            headline = (
                "Significant *negative* correlation from "
                + ", ".join(l["agent"] for l in losers)
                + " — that agent's ranking has been backwards so far."
            )
        elif blend_ic.get("significant"):
            state = "blend-only"
            headline = (
                "No single agent clears significance, but the equal-weighted "
                "blend does."
            )
        else:
            state = "no-evidence"
            headline = (
                "No agent, and no blend of them, shows a correlation with the "
                "subsequent trend that survives the overlap adjustment."
            )

    # Structural findings, true regardless of how much data there is.
    for row in ranked:
        if row["dispersion"] is not None and row["dispersion"] < 5.0:
            flags.append(
                f"{row['agent']} spreads its scores by only "
                f"{row['dispersion']:.1f} points across a typical day — it is "
                "close to a constant, so no weight can make it predictive."
            )
        if row["mean_score"] is not None and row["mean_score"] > 62:
            flags.append(
                f"{row['agent']} averages {row['mean_score']:.0f}/100 — it "
                "almost never says sell, so its useful range is half the scale."
            )
        if row["mean_score"] is not None and row["mean_score"] < 38:
            flags.append(
                f"{row['agent']} averages {row['mean_score']:.0f}/100 — it is "
                "persistently bearish, which the blend reads as a standing "
                "discount rather than a signal."
            )

    for pair, stats in (horizon_result.get("agent_correlations") or {}).items():
        if abs(stats["mean_rho"]) >= 0.6:
            a, b = pair.split("|")
            flags.append(
                f"{a} and {b} rank stocks {stats['mean_rho']:+.2f} alike — the "
                "disjoint-evidence design is not producing independent views "
                "for this pair, so the average counts that view roughly twice."
            )

    for agent, stats in (horizon_result.get("momentum_alignment") or {}).items():
        if abs(stats["mean_rho"]) >= 0.5:
            flags.append(
                f"{agent} tracks trailing {MOMENTUM_LOOKBACK}-day momentum at "
                f"{stats['mean_rho']:+.2f} — much of what it contributes is a "
                "number the price series already contains."
            )

    momentum = ((horizon_result.get("baselines") or {}).get(MOMENTUM_KEY) or {}).get("ic") or {}
    if momentum.get("mean_ic") is not None and blend_ic.get("mean_ic") is not None:
        if momentum["mean_ic"] > blend_ic["mean_ic"]:
            flags.append(
                f"Plain {MOMENTUM_LOOKBACK}-day momentum (IC "
                f"{momentum['mean_ic']:+.3f}) is ranking better than the "
                f"five-agent blend (IC {blend_ic['mean_ic']:+.3f}) — twenty "
                "model calls a day are currently losing to one subtraction."
            )

    if weights.get("verdict") in ("weights-are-noise", "no-better-than-equal-weights"):
        flags.append(
            "The optimised weights do not beat equal weighting out of sample; "
            "recommending them would be fitting the days already observed."
        )

    return {
        "state": state,
        "headline": headline,
        "ranked_agents": ranked,
        "flags": flags,
    }


def compute(doc: dict, current_weights: dict, horizons=HORIZONS) -> dict:
    """The whole analysis: every horizon, plus the ledger's own vital signs."""
    snapshots = doc.get("snapshots") or []
    dates = sorted({s.get("date") for s in snapshots if s.get("date")})
    horizons_out = {}
    for horizon in horizons:
        horizons_out[str(horizon)] = compute_horizon(doc, horizon, current_weights)

    # The horizon the agents are actually asked about is one to three months, so
    # 21 days is the shortest one whose result is a fair test of the claim. It
    # leads the report; the shorter ones are there to show how much of a young
    # ledger's apparent signal is noise.
    primary = "21" if horizons_out.get("21", {}).get("days") else None
    if primary is None:
        scored = [h for h in horizons_out.values() if h.get("days")]
        primary = str(max(scored, key=lambda h: h["days"])["horizon_days"]) if scored else None

    return {
        "portfolio_id": doc.get("portfolio_id"),
        "portfolio_name": doc.get("portfolio_name"),
        "ledger": {
            "snapshots": len(snapshots),
            "distinct_days": len(dates),
            "first_day": dates[0] if dates else None,
            "last_day": dates[-1] if dates else None,
            "rows": sum(len(s.get("rows") or []) for s in snapshots),
            "scopes": sorted({s.get("scope") for s in snapshots if s.get("scope")}),
        },
        "current_weights": {k: float(current_weights.get(k, 1.0)) for k in AGENT_KEYS},
        "primary_horizon": primary,
        "horizons": horizons_out,
        "verdict": verdict(horizons_out[primary]) if primary else {
            "state": "no-data",
            "headline": "Nothing has been scored yet — record some days first.",
            "ranked_agents": [],
            "flags": [],
        },
    }
