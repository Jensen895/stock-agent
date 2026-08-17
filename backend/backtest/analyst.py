"""The reviewing agent — a Claude CLI call that reads the metrics and judges them.

This is a sixth agent, but it is not one of the five. The five score stocks;
this one scores *the five*. It never sees a price, a headline or a fundamental —
only the output of ``metrics.py`` — so it cannot form a view on any company, and
nothing it writes can leak back into a prediction.

It runs through the ``claude`` CLI already installed on this machine, the same
way ``ai_local_claude.py`` runs the advisor's agents: no API key, no bill, one
process per call. If the CLI is missing the command still works — ``report.py``
renders the deterministic sections on their own and says the interpretation is
absent.

What it is asked to do, and what it is asked not to do
------------------------------------------------------
The numbers are already computed. Asking a model to compute them would be
strictly worse, so it is told they are given and not to recalculate them.

What it is for is the part arithmetic cannot do: deciding what the numbers
*mean* when several of them disagree, noticing that an agent's good correlation
came entirely from three days in one week, and — the part this whole exercise
exists for — being willing to say that none of it works.

That last point needs saying explicitly in the prompt, because the default
behaviour of a helpful model handed a table of statistics is to find something
encouraging in it. The system prompt therefore makes "these five scores are not
correlated with the trend", "the recommended weights are fitted noise" and "the
missing signal is not one of these five agents" first-class answers, and pushes
against the failure mode where a p-value of 0.31 gets described as "a promising
trend".
"""

import json
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone

from backend.ai_agents import AGENT_KEYS

DEFAULT_MODEL = "claude-opus-5[1m]"
DEFAULT_TIMEOUT = 600


def _weights_schema() -> dict:
    return {
        "type": "object",
        "properties": {k: {"type": "number"} for k in AGENT_KEYS},
        "required": list(AGENT_KEYS),
        "additionalProperties": False,
    }


REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": [
                "insufficient-data",
                "no-evidence",
                "weak-signal",
                "signal-found",
                "inverted-signal",
            ],
        },
        "headline": {
            "type": "string",
            "description": "One sentence. The single most important thing "
                           "today's data says. No hedging.",
        },
        "confidence": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
            "description": "How much the evidence supports the verdict, given "
                           "the number of independent days behind it.",
        },
        "trend_vs_prediction": {
            "type": "string",
            "description": "2-5 sentences: over the days observed, did the "
                           "stocks move the way the scores implied? Name the "
                           "agents that got the direction right and wrong.",
        },
        "per_agent": {
            "type": "array",
            "description": "One entry per agent, all five, worst to best is "
                           "fine — order does not matter.",
            "items": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": list(AGENT_KEYS)},
                    "grade": {
                        "type": "string",
                        "enum": ["helping", "neutral", "hurting", "unknown"],
                    },
                    "assessment": {
                        "type": "string",
                        "description": "2-4 sentences citing this agent's own "
                                       "IC, t, dispersion and hit rate.",
                    },
                },
                "required": ["agent", "grade", "assessment"],
                "additionalProperties": False,
            },
        },
        "recommended_weights": _weights_schema(),
        "apply_recommendation": {
            "type": "boolean",
            "description": "True only if the evidence genuinely supports "
                           "changing the live weights today. False when the "
                           "honest answer is 'not enough data' or 'equal "
                           "weights are fine' — in that case still fill in "
                           "recommended_weights with what you would keep.",
        },
        "weights_rationale": {
            "type": "string",
            "description": "Why those weights, or why you are declining to "
                           "recommend a change.",
        },
        "beyond_these_five": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete things the five agents structurally "
                           "cannot see that the data suggests are driving "
                           "returns. Be specific and be willing to say the "
                           "five are the wrong five.",
        },
        "watch_next": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What would change your mind, and how many more "
                           "days it would take to find out.",
        },
        "report_markdown": {
            "type": "string",
            "description": "The long-form analysis, GitHub-flavoured markdown, "
                           "starting at heading level 2. This is the body of "
                           "the daily report.",
        },
    },
    "required": [
        "verdict",
        "headline",
        "confidence",
        "trend_vs_prediction",
        "per_agent",
        "recommended_weights",
        "apply_recommendation",
        "weights_rationale",
        "beyond_these_five",
        "watch_next",
        "report_markdown",
    ],
    "additionalProperties": False,
}


SYSTEM = """You are a quantitative research reviewer auditing a five-agent stock
scoring system. You are not an investment advisor and you are not being asked
whether to buy anything. You are being asked one question: do these five agents'
scores predict what the stocks subsequently did, and if so, how should they be
weighted?

WHAT THE SYSTEM DOES
Five independent LLM agents each score every ticker 0-100 each trading day
(100 = maximum conviction to buy, 50 = neutral, 0 = maximum conviction to sell).
Each agent sees a deliberately disjoint slice of the evidence and none sees
another's work:
  company_perspective  company news, what it sells, earnings record, growth
  personal             the investor's position and the stock's own price history
  statistics           multiples, margins, balance sheet, beta, 52-week range
  expert               sell-side consensus, price targets, upgrades/downgrades
  macro                market-wide news only: rates, tariffs, war, policy
The five scores are averaged with user-set weights into the number the investor
acts on.

WHAT YOU ARE GIVEN
A JSON metrics document, already computed. Do not recompute anything and do not
invent a number that is not in it. Key fields:
  mean_ic          average per-day cross-sectional rank correlation (Spearman)
                   between a signal's score and the forward return. Computed
                   within each day and across tickers, so the market's own move
                   is already removed. This is the primary measure.
  t_stat, p_value  significance of mean_ic, computed on EFFECTIVE days, not
                   calendar days.
  effective_days   the number of NON-OVERLAPPING return windows the data spans.
                   Consecutive days share almost all of a 21-day return, so this
                   is the real sample size and it is often a small fraction of
                   `days`. Reason about effective_days. Never quote t_stat_naive
                   as if it meant something.
  cross_sectional_std  how far apart an agent spreads its scores on a typical
                   day. Below ~5 the agent is effectively a constant and NO
                   weighting can make it predictive.
  mean_score       the agent's average score. Far above 50 means it has no sell
                   setting; the blend reads that as a standing offset.
  agent_correlations   per-day rank correlation between pairs of agents. The
                   whole design assumes these are low. High values mean the
                   average is double-counting one view.
  momentum_21d     a free baseline signal: the stock's own trailing 21-day
                   return. It calls no model and costs nothing. Any agent that
                   cannot beat it is not paying for itself.
  momentum_alignment   how much each agent's score is just momentum restated.
  weights          an out-of-sample test of the optimised weighting. `folds`
                   fit on half the days and score on the other half.
                   out_of_sample_ic is the only weight number worth believing.

HOW TO JUDGE
Interpret IC on the scale this field actually lives on. |IC| below 0.02 is
nothing. 0.02-0.05 is the level a real, useful institutional signal operates at
IF it is stable. Above 0.10 sustained over many independent periods would be
remarkable and your first assumption should be a data problem, not a discovery.

A result is not a finding unless it survives effective_days. With 3 effective
days nothing is a finding, however large the coefficient. Say that plainly.

THE ANSWERS YOU ARE EXPLICITLY ALLOWED, AND OFTEN REQUIRED, TO GIVE
  - "None of these five scores is correlated with the subsequent trend."
  - "There is not yet enough data to say anything; come back in N days."
  - "The optimised weights are fitted noise and should not be applied."
  - "Equal weights are as good as anything found, so change nothing."
  - "Agent X is actively harmful — its ranking has been inverted."
  - "The five agents are the wrong five; here is what they structurally cannot
     see." Sector and factor exposure, position size and concentration,
     liquidity, earnings-date proximity, crowding and short interest dynamics,
     the investor's own tax and cash constraints, transaction costs, or simply
     that one to three month stock returns are close to unforecastable from
     public information and the honest expected IC is zero.

Do NOT describe a p-value above 0.10 as promising, encouraging, or a trend.
Do NOT recommend weight changes to chase a difference that is inside the noise.
Do NOT pad. If the answer is "nothing yet, for the fourth day running", say so
in two lines and spend the report on what would need to change.

Where the data does support something, be equally direct about it.

REPORT
`report_markdown` is the body of a daily report the investor reads. Start at
heading level 2. Suggested shape, adapt it to what the data actually supports:
  ## What the numbers say
  ## Agent by agent
  ## Weights
  ## What these five agents cannot see
  ## What to watch
Cite specific figures from the metrics document. Prefer a table when comparing
the five. Keep it tight — a page or two, not five."""


class ClaudeAnalyst:
    """One ``claude`` process, one report.

    Mirrors the flags ``ai_local_claude.py`` uses, for the same reasons: --bare
    so this project's PARA-workspace ``CLAUDE.md`` does not leak into the
    prompt, no tools because the evidence is already assembled, a JSON schema so
    the answer arrives structured, and the payload on stdin because a full
    metrics document can exceed the argv limit.
    """

    def __init__(self, binary: str = None, model: str = None, timeout: int = None,
                 bare: bool = None, extra_args=None):
        self.binary = binary or os.environ.get("BACKTEST_CLAUDE_BIN") \
            or os.environ.get("LOCAL_CLAUDE_BIN") or "claude"
        self.model = model or os.environ.get("BACKTEST_CLAUDE_MODEL") or DEFAULT_MODEL
        try:
            self.timeout = int(timeout or os.environ.get("BACKTEST_CLAUDE_TIMEOUT")
                               or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT
        self.bare = True if bare is None else bare
        self.extra_args = list(extra_args) if extra_args is not None else shlex.split(
            os.environ.get("BACKTEST_CLAUDE_EXTRA_ARGS", "")
        )
        self.label = f"claude-cli:{self.model}"

    def available(self) -> bool:
        return bool(shutil.which(self.binary) or os.path.isfile(self.binary))

    def review(self, metrics: dict, previous: dict = None) -> dict:
        """Run the review. Returns the structured answer plus run metadata.

        Raises RuntimeError with a readable message on failure, which the
        command catches and turns into a report with the deterministic sections
        only — a missing interpretation is a degraded report, not a lost day.
        """
        prompt = self._prompt(metrics, previous)
        started = time.time()
        envelope = self._run(prompt)
        answer = self._answer_from(envelope)
        answer["_meta"] = {
            "model": self.model,
            "label": self.label,
            "duration_ms": int((time.time() - started) * 1000),
            "cost_usd": envelope.get("total_cost_usd"),
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        return answer

    # --- prompt ---------------------------------------------------------

    # How many recent per-day IC readings to keep in the prompt, for the
    # horizon the report leads with. Enough to see a signal decaying or a good
    # average resting entirely on one week; not so many that a year-old ledger
    # spends most of the context window on a column of numbers whose summary is
    # already in the same document.
    SERIES_TAIL = 60

    @classmethod
    def _compact(cls, metrics: dict) -> dict:
        """The metrics with the bulky per-day series trimmed.

        Only the primary horizon keeps its IC series, and only its tail. Every
        summary statistic is untouched — this drops raw detail the model would
        have to re-average anyway, never a conclusion.
        """
        primary = metrics.get("primary_horizon")
        out = dict(metrics)
        horizons = {}
        for key, horizon in (metrics.get("horizons") or {}).items():
            trimmed = dict(horizon)
            keep_series = key == primary
            if not keep_series:
                trimmed["dates"] = []
            for group in ("agents", "blended", "baselines"):
                block = trimmed.get(group) or {}
                rebuilt = {}
                for name, stats in block.items():
                    stats = dict(stats)
                    ic = dict(stats.get("ic") or {})
                    series = ic.get("series") or []
                    ic["series"] = series[-cls.SERIES_TAIL:] if keep_series else []
                    ic["series_truncated"] = len(series) > len(ic["series"])
                    stats["ic"] = ic
                    rebuilt[name] = stats
                trimmed[group] = rebuilt
            horizons[key] = trimmed
        out["horizons"] = horizons
        return out

    def _prompt(self, metrics: dict, previous: dict) -> str:
        parts = []
        if previous:
            # Continuity is the point of a *daily* report. Without yesterday's
            # verdict the model re-derives the same conclusion every morning and
            # never notices that a signal has been decaying for a week.
            parts.append(
                "YESTERDAY'S REVIEW, for continuity. Say explicitly whether "
                "today's data changes it, and do not repeat it verbatim if "
                "nothing has moved — note that nothing has moved and why that "
                "is itself informative.\n"
                + json.dumps(
                    {
                        "reviewed_at": (previous.get("_meta") or {}).get("reviewed_at"),
                        "verdict": previous.get("verdict"),
                        "headline": previous.get("headline"),
                        "confidence": previous.get("confidence"),
                        "apply_recommendation": previous.get("apply_recommendation"),
                        "recommended_weights": previous.get("recommended_weights"),
                    },
                    indent=1,
                )
            )
        parts.append(
            "TODAY'S METRICS. Every number below is already computed from the "
            "prediction ledger and the realised closes. The primary horizon is "
            f"{metrics.get('primary_horizon')} trading days. Per-day IC series "
            "are kept only for that horizon and only for the most recent "
            f"{self.SERIES_TAIL} days; `series_truncated` says when there was "
            "more.\n"
            + json.dumps(self._compact(metrics), indent=1, default=str)
        )
        return "\n\n".join(parts)

    # --- process --------------------------------------------------------

    def _argv(self) -> list:
        argv = [self.binary]
        if self.bare:
            argv.append("--bare")
        argv.extend([
            "--print",
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", SYSTEM,
            "--tools", "",
            "--json-schema", json.dumps(REPORT_SCHEMA),
            "--no-session-persistence",
            "--disable-slash-commands",
        ])
        argv.extend(self.extra_args)
        return argv

    def _run(self, prompt: str) -> dict:
        cwd = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            proc = subprocess.run(
                self._argv(), input=prompt, capture_output=True, text=True,
                timeout=self.timeout, cwd=cwd,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Claude CLI '{self.binary}' not found on PATH. Install Claude "
                "Code, set BACKTEST_CLAUDE_BIN to its full path, or pass "
                "--no-ai for the numbers without the interpretation."
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Claude CLI timed out after {self.timeout}s. Raise "
                "BACKTEST_CLAUDE_TIMEOUT or use a smaller model."
            )
        envelope = self._envelope(proc.stdout)
        if envelope is None:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(
                f"Claude CLI exited {proc.returncode} without a result"
                + (f": {tail}" if tail else ".")
            )
        if envelope.get("is_error"):
            detail = envelope.get("result") or envelope.get("api_error_status") \
                or "unknown error"
            raise RuntimeError(f"Claude CLI reported an error: {detail}")
        return envelope

    @staticmethod
    def _envelope(stdout: str):
        found = None
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                found = obj
        return found

    @staticmethod
    def _answer_from(envelope: dict) -> dict:
        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            return structured
        text = (envelope.get("result") or "").strip()
        if not text:
            raise RuntimeError("Claude CLI returned no content.")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise RuntimeError("Claude CLI returned malformed JSON.")
