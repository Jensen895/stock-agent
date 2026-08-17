"""Rendering: the metrics and the review as one markdown report.

Two layers, in this order on the page and for a reason:

The **deterministic layer** comes first — the tables, the significance figures,
the weight search. Every number in it was computed by ``metrics.py`` and is
reproducible from the ledger with no model involved. If the reviewing agent is
unavailable, or says something that does not match the tables, this is the part
to believe.

The **review** follows, clearly marked as a model's interpretation. It is the
part that can be wrong in interesting ways, so it is never allowed to be the
only thing on the page and it is never allowed to restate a number the tables
already carry without the tables being right there to check it against.

Without the CLI the command still produces a full report, minus the prose. That
is a deliberate ordering of what matters: the measurement is the product, the
commentary is a convenience.
"""

import os
from datetime import datetime, timezone

from backend.ai_agents import AGENTS_BY_KEY, AGENT_KEYS

from .metrics import MIN_DAYS_FOR_INFERENCE, MOMENTUM_KEY
from .outcomes import HORIZONS, MOMENTUM_LOOKBACK


def _num(value, digits=3, plus=False):
    if value is None:
        return "—"
    fmt = f"{{:{'+' if plus else ''}.{digits}f}}"
    return fmt.format(value)


def _pct(value, digits=1):
    return "—" if value is None else f"{value:.{digits}f}%"


def _p(value):
    if value is None:
        return "—"
    if value < 0.001:
        return "<0.001"
    return f"{value:.3f}"


def _name(key: str) -> str:
    agent = AGENTS_BY_KEY.get(key)
    return agent.name if agent else key


def _table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _signal_row(label: str, stats: dict, extra: str = "") -> list:
    ic = stats["ic"]
    hits = stats["hits"]
    disp = stats["dispersion"]
    # The momentum baseline is a percentage return, not a 0-100 confidence, so
    # its centre is reported as a return and its "buy calls" are the names that
    # were simply up. Sharing the table is worth this small annotation — the
    # whole point is to read the agents against it on one line each.
    centre = disp["mean_score"]
    if stats.get("scale") == "return" and centre is not None:
        centre = f"{centre:+.1f}%"
    else:
        centre = _num(centre, 1)
    return [
        label + extra,
        _num(ic["mean_ic"], 3, plus=True),
        _num(ic["t_stat"], 2, plus=True),
        _p(ic["p_value"]),
        f"{ic['positive_days']}/{ic['days']}",
        _num(disp["cross_sectional_std"], 1),
        centre,
        f"{_pct(hits['buy_hit_pct'], 0)} ({hits['buy_calls']})",
        _num(stats["long_short"]["mean_pct"], 2, plus=True),
    ]


_SIGNAL_HEADERS = (
    "Signal", "Mean IC", "t", "p", "IC>0", "Spread", "Avg score",
    "Buy hit-rate (n)", "Top−bot %",
)


def _ledger_section(metrics: dict) -> str:
    led = metrics["ledger"]
    horizon = metrics.get("primary_horizon")
    h = (metrics.get("horizons") or {}).get(horizon) or {}
    lines = [
        "## Ledger",
        "",
        _table(
            ["", ""],
            [
                ["Portfolio", f"{metrics.get('portfolio_name') or '?'} "
                             f"(`{metrics.get('portfolio_id') or '?'}`)"],
                ["Days recorded", led["distinct_days"]],
                ["Date range", f"{led['first_day'] or '—'} → {led['last_day'] or '—'}"],
                ["Predictions stored", led["rows"]],
                ["Sources", ", ".join(led["scopes"]) or "—"],
                ["Primary horizon", f"{horizon} trading days" if horizon else "—"],
                ["Scored days at that horizon", h.get("days", 0)],
                ["Independent windows", _independent(h)],
                ["Tickers", h.get("tickers", 0)],
                ["Return basis", _basis(h)],
            ],
        ),
    ]
    return "\n".join(lines)


def _basis(horizon_result: dict) -> str:
    """Whether returns are measured against SPY, said only once there are any."""
    if not horizon_result.get("rows"):
        return "—"
    if horizon_result.get("benchmark_relative"):
        return "excess over SPY"
    return "raw return (SPY unavailable for some rows)"


def _independent(horizon_result: dict) -> str:
    """The sample size that actually governs every p-value on the page."""
    blend = ((horizon_result.get("blended") or {}).get("equal_weight") or {}).get("ic") or {}
    eff = blend.get("effective_days")
    days = blend.get("days")
    if not eff:
        return "—"
    return f"{eff} (from {days} overlapping days)"


def _verdict_section(metrics: dict) -> str:
    v = metrics["verdict"]
    state = v["state"]
    badge = {
        "signal-found": "**SIGNAL**",
        "blend-only": "**BLEND ONLY**",
        "inverted-signal": "**INVERTED**",
        "no-evidence": "**NO EVIDENCE**",
        "insufficient-data": "**NOT ENOUGH DATA**",
        "no-data": "**NO DATA**",
    }.get(state, state)
    lines = [f"## Verdict — {badge}", "", v["headline"]]
    if v.get("flags"):
        lines += ["", "### Structural findings", ""]
        lines += [f"- {flag}" for flag in v["flags"]]
    return "\n".join(lines)


def _agents_section(metrics: dict) -> str:
    horizon = metrics.get("primary_horizon")
    h = (metrics.get("horizons") or {}).get(horizon) or {}
    if not h.get("days"):
        return ""
    rows = []
    for key in AGENT_KEYS:
        stats = h["agents"].get(key)
        if not stats:
            continue
        cov = stats["coverage"]["coverage_pct"]
        extra = f" ({cov:.0f}% cov)" if cov is not None and cov < 95 else ""
        rows.append(_signal_row(_name(key), stats, extra))

    blend_rows = []
    for label, key in (("Blend — as shown", "as_shown"),
                       ("Blend — equal weights", "equal_weight")):
        stats = (h.get("blended") or {}).get(key)
        # A ledger written before the app stored a blended score has nothing
        # here; an empty row would read as a measurement of zero.
        if stats and stats["ic"]["days"]:
            blend_rows.append(_signal_row(label, stats))
    momentum = (h.get("baselines") or {}).get(MOMENTUM_KEY)
    if momentum and momentum["ic"]["days"]:
        blend_rows.append(
            _signal_row(f"Baseline — {MOMENTUM_LOOKBACK}d momentum", momentum)
        )

    return "\n".join([
        f"## The five agents at {horizon} trading days",
        "",
        _table(_SIGNAL_HEADERS, rows + blend_rows),
        "",
        "*Mean IC is the average per-day rank correlation between the score and "
        "the forward return, computed across tickers within each day — the "
        "market's own move is already removed. `t` and `p` are computed on "
        "independent windows, not on overlapping days. `Spread` is how far "
        "apart the agent puts its scores on a typical day; near zero means it "
        "cannot rank anything whatever weight it is given. `Buy hit-rate` is "
        "how often the names it scored above 55 then beat SPY, with the number "
        "of such calls in brackets. `Top−bot` is the return of its "
        "highest-scored third minus its lowest, in points. The momentum "
        "baseline is a return rather than a 0-100 score, so its centre is shown "
        "as a percentage and its 'calls' are simply the names that were up.*",
    ])


def _correlation_section(metrics: dict) -> str:
    horizon = metrics.get("primary_horizon")
    h = (metrics.get("horizons") or {}).get(horizon) or {}
    pairs = h.get("agent_correlations") or {}
    momentum = h.get("momentum_alignment") or {}
    if not pairs and not momentum:
        return ""
    lines = ["## Are the five actually independent?", ""]
    if pairs:
        grid = []
        for a_index, a in enumerate(AGENT_KEYS):
            row = [_name(a)]
            for b in AGENT_KEYS:
                if a == b:
                    row.append("—")
                    continue
                key = f"{a}|{b}" if f"{a}|{b}" in pairs else f"{b}|{a}"
                stats = pairs.get(key)
                row.append(_num(stats["mean_rho"], 2, plus=True) if stats else "·")
            grid.append(row)
        lines += [
            _table([""] + [AGENTS_BY_KEY[k].short for k in AGENT_KEYS], grid),
            "",
            "*Average per-day rank correlation between two agents' scores. The "
            "app's design assumes these stay low — that is the entire argument "
            "for splitting the evidence five ways. A pair above ±0.6 is one "
            "view being counted twice by the average.*",
            "",
        ]
    if momentum:
        lines += [
            "### How much of each agent is just last month's price move",
            "",
            _table(
                ["Agent", "ρ with trailing 21d return"],
                [[_name(k), _num(v["mean_rho"], 2, plus=True)]
                 for k, v in momentum.items()],
            ),
            "",
            "*A high value means the agent is largely restating a number the "
            "price series already contains, whatever dimension it was asked "
            "about.*",
        ]
    return "\n".join(lines)


def _weights_section(metrics: dict) -> str:
    horizon = metrics.get("primary_horizon")
    h = (metrics.get("horizons") or {}).get(horizon) or {}
    w = h.get("weights") or {}
    if not w:
        return ""
    verdict_text = {
        "insufficient-data": "Not enough scored days to search for weights.",
        "too-few-days-to-validate":
            "Weights found, but there are too few days to hold any of them out "
            "— the in-sample figure below is not evidence.",
        "weights-are-noise":
            "The optimised weights score **zero or worse out of sample**. What "
            "the search found was the shape of the days it was fitted on.",
        "no-better-than-equal-weights":
            "The optimised weights do **not** beat a plain equal average out of "
            "sample. Leave the sliders alone.",
        "largely-overfit":
            "The out-of-sample score is less than half the in-sample one — most "
            "of what the search found was noise, though not all of it.",
        "holds-up-out-of-sample":
            "The weighting **survives the held-out half**. This is the one case "
            "where acting on it is defensible.",
    }.get(w.get("verdict"), w.get("verdict") or "—")

    rows = [["Agent", "Live now", "Optimiser's answer"]]
    body = []
    for key in AGENT_KEYS:
        body.append([
            _name(key),
            _num((w.get("current_weights") or {}).get(key), 2),
            _num((w.get("best_weights") or {}).get(key), 2),
        ])

    lines = [
        "## Weights",
        "",
        verdict_text,
        "",
        _table(rows[0], body),
        "",
        _table(
            ["Weighting", "Mean IC"],
            [
                ["Equal (all five count once)", _num(w.get("equal_weight_ic"), 4, True)],
                ["Live weights", _num(w.get("current_weight_ic"), 4, True)],
                ["Optimised, in-sample", _num(w.get("in_sample_ic"), 4, True)],
                ["**Optimised, out-of-sample**",
                 f"**{_num(w.get('out_of_sample_ic'), 4, True)}**"],
            ],
        ),
    ]
    if w.get("folds"):
        lines += [
            "",
            "### The held-out folds",
            "",
            _table(
                ["Fit on", "Weights found", "IC where fitted", "IC on held-out days"],
                [
                    [
                        f["fold"],
                        ", ".join(
                            f"{AGENTS_BY_KEY[k].short} {v:g}"
                            for k, v in f["weights"].items()
                        ),
                        _num(f["train_ic"], 4, True),
                        _num(f["test_ic"], 4, True),
                    ]
                    for f in w["folds"]
                ],
            ),
            "",
            "*Days are split odd/even rather than early/late, so both halves "
            "cover the same market conditions and the gap between the two "
            "columns measures over-fitting rather than a change in regime.*",
        ]
    return "\n".join(lines)


def _horizons_section(metrics: dict) -> str:
    rows = []
    for horizon in HORIZONS:
        h = (metrics.get("horizons") or {}).get(str(horizon)) or {}
        if not h.get("days"):
            continue
        blend = ((h.get("blended") or {}).get("equal_weight") or {}).get("ic") or {}
        best = None
        for key in AGENT_KEYS:
            stats = (h.get("agents") or {}).get(key)
            if not stats:
                continue
            ic = stats["ic"]["mean_ic"]
            if ic is not None and (best is None or ic > best[1]):
                best = (key, ic)
        rows.append([
            f"{horizon}d",
            h["days"],
            blend.get("effective_days") or "—",
            _num(blend.get("mean_ic"), 3, True),
            _p(blend.get("p_value")),
            f"{_name(best[0])} {_num(best[1], 3, True)}" if best else "—",
        ])
    if not rows:
        return ""
    return "\n".join([
        "## Every horizon",
        "",
        _table(
            ["Horizon", "Days", "Independent", "Blend IC", "p", "Best single agent"],
            rows,
        ),
        "",
        "*The agents are asked for a one-to-three-month view, so 21 and 63 days "
        "are the horizons that test what they were actually asked. The short "
        "ones are here to show how much of a young ledger's apparent signal is "
        "day-to-day noise.*",
    ])


def _review_section(review: dict, error: str = "") -> str:
    if error:
        return "\n".join([
            "## Review",
            "",
            f"> The reviewing agent did not run: {error}",
            "",
            "The tables above are unaffected — they are computed from the "
            "ledger with no model involved.",
        ])
    if not review:
        return ""
    meta = review.get("_meta") or {}
    lines = [
        "## Review",
        "",
        f"> {review.get('headline', '')}",
        "",
        _table(
            ["", ""],
            [
                ["Verdict", f"`{review.get('verdict')}`"],
                ["Confidence", f"`{review.get('confidence')}`"],
                ["Change the weights?",
                 "**yes**" if review.get("apply_recommendation") else "no"],
                ["Reviewer", f"`{meta.get('label', '?')}`"],
            ],
        ),
        "",
        "### Did the trend follow the prediction?",
        "",
        review.get("trend_vs_prediction", ""),
        "",
    ]
    per_agent = review.get("per_agent") or []
    if per_agent:
        grades = {"helping": "helping", "neutral": "neutral",
                  "hurting": "HURTING", "unknown": "unknown"}
        lines += [
            "### Agent by agent",
            "",
            _table(
                ["Agent", "Grade", "Assessment"],
                [
                    [_name(a.get("agent")), grades.get(a.get("grade"), a.get("grade")),
                     (a.get("assessment") or "").replace("\n", " ")]
                    for a in per_agent
                ],
            ),
            "",
        ]
    lines += [
        "### Weights",
        "",
        review.get("weights_rationale", ""),
        "",
    ]
    if review.get("recommended_weights"):
        lines += [
            _table(
                ["Agent", "Recommended"],
                [[_name(k), _num(v, 2)]
                 for k, v in review["recommended_weights"].items()],
            ),
            "",
        ]
    for heading, key in (("What these five cannot see", "beyond_these_five"),
                         ("What to watch", "watch_next")):
        items = review.get(key) or []
        if items:
            lines += [f"### {heading}", ""] + [f"- {i}" for i in items] + [""]
    body = (review.get("report_markdown") or "").strip()
    if body:
        lines += ["---", "", body]
    return "\n".join(lines)


def render(metrics: dict, review: dict = None, review_error: str = "") -> str:
    """The whole daily report."""
    now = datetime.now(timezone.utc)
    horizon = metrics.get("primary_horizon")
    h = (metrics.get("horizons") or {}).get(horizon) or {}
    days = h.get("days") or 0

    header = [
        f"# Back-test — {metrics.get('portfolio_name') or 'portfolio'} — "
        f"{now.date().isoformat()}",
        "",
        "*Not a feature of the app. This measures whether the five agents' "
        "scores have predicted anything, and is written for the person who "
        "built them.*",
        "",
    ]
    if days and days < MIN_DAYS_FOR_INFERENCE:
        header += [
            f"> **Read nothing into today's numbers.** {days} scored day(s) at "
            f"the primary horizon; significance testing starts at "
            f"{MIN_DAYS_FOR_INFERENCE} and even then rests on the far smaller "
            "number of independent windows. The tables below are descriptive.",
            "",
        ]

    sections = [
        "\n".join(header),
        _verdict_section(metrics),
        _ledger_section(metrics),
        _agents_section(metrics),
        _weights_section(metrics),
        _correlation_section(metrics),
        _horizons_section(metrics),
        _review_section(review, review_error),
    ]
    return "\n\n".join(s for s in sections if s).rstrip() + "\n"


def write(directory: str, markdown: str, day: str = None) -> str:
    """Save the report and refresh ``latest.md``. Returns the dated path.

    Named for the day the report was *written*, not the day of the last
    prediction. Outcomes mature every session, so a run on a day the app never
    generated is still a new report — a 21-day window that closed overnight can
    move a conclusion without a single new score being recorded.
    """
    day = day or datetime.now(timezone.utc).date().isoformat()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{day}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    with open(os.path.join(directory, "latest.md"), "w", encoding="utf-8") as f:
        f.write(markdown)
    return path
