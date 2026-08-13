"""AI advisor — five independent agents, one weighted score per stock.

This is the brain of the assistant. It gathers everything the rest of the app
knows about a portfolio (holdings and cost basis, live prices, a year of price
history, realized and unrealized gains, company fundamentals, Wall Street's
research, company news and the macro tape), splits it into five disjoint slices,
asks a separate agent to score each slice, and averages the five answers into a
single **confidence score** per stock over the next one to three months — one
set tuned for low risk, one for high risk.

The horizon is deliberately one to three months rather than a week. The
sell-side ratings the expert agent reads are twelve-month views, so asking for a
few days' outlook made the agents disagree with the street about the question
rather than the answer — a 12-month "Strong Buy" and a 3-day "trim" are not
actually contradictory.

The confidence score
--------------------
Every stock gets one number from 0 to 100, and the number *is* the call:

    100 ── maximum conviction to buy more
     50 ── neutral: hold
      0 ── maximum conviction to sell the whole position

so the mid-40s-to-mid-50s band reads as hold, and the further a score sits from
50 the stronger the buy (above) or sell (below) signal. ``_CONFIDENCE_BANDS``
turns a score into the label and colour the UI shows next to it.

The five agents
---------------
The agents themselves — their roles, their prompts, and crucially which fields
each one is allowed to see — live in ``ai_agents.py``. In short:

    company_perspective   the business: news, products, the earnings record,
                          growth, partnerships
    personal              your position and the stock's own price history
    statistics            P/E, market cap, EPS, margins, debt, beta, ranges
    expert                what Goldman, JP Morgan and the rest conclude
    macro                 rates, tariffs, war, policy — nothing company-specific

Independence is structural, not advisory. Each agent is its own LLM call with
its own system prompt and its own payload, and the payloads are disjoint: the
statistics agent never sees a headline, the company agent never sees a multiple,
the macro agent never learns what a company sells beyond its sector. No agent
sees another's output. They meet for the first time here, as five numbers.

That matters because averaging only buys you something when the errors are
uncorrelated. The design this replaced showed two models the *same* evidence,
including the same analyst research, and averaged them — which measured how
often two models read one paragraph the same way, and reported it as
confirmation. Five narrow views that genuinely disagree carry more information
than two broad views that mostly can't.

What it costs: no single agent sees the whole picture, so no single agent's
reasoning is a complete argument, and the detail card shows all five rather than
one tidy verdict. Reconciling them is the average's job — and the weights are in
your hands.

The weighting system
--------------------
The score is the weighted average of whichever agents answered, with the
weights in ``DEFAULT_AGENT_WEIGHTS`` (all 1.0 — equal) overridden by the
per-portfolio ``ai_weights.json``. An agent that failed, or that had no evidence
for a ticker, simply drops out and the rest are renormalised, so the number is
always a true average of who actually spoke rather than a figure dragged toward
neutral by a gap.

Reweighting does **not** re-run anything. Each suggestion carries the raw
per-agent scores in ``sources``, so ``set_weights`` re-blends the cached result
in place and returns instantly — you can dial the statistics agent to 2x and
the macro agent to 0 and watch the whole panel resettle without spending a
token.

Layers, kept separate like the rest of the app:

  - The clients — ``GeminiClient``, ``GroqClient``, ``ClaudeClient``,
    ``LlamaClient``, ``OllamaClient``. Each is an I/O boundary onto one LLM API,
    reached with the Python standard library only (``urllib``), matching
    ``market_data.py`` and ``news_data.py``. They share one method,
    ``complete_json``, so they are interchangeable.
  - ``ai_agents.py`` — who the agents are and what each may see.
  - ``AIAdvisorService`` — the orchestration. Gathers context once, fans the
    agent calls out concurrently, blends, caches, persists so it survives
    restarts, and refreshes once per trading day at the opening bell.

Models are capacity here, not opinion. Configure one and it answers for all five
agents; configure three and they are handed out round-robin so the calls spread
across providers and quotas. Which model an agent used is recorded and shown,
but the consensus on screen is between *agents*, never between models.

Two design notes worth keeping:

  - The agents are given headlines; they never search. Free-tier Gemini cannot
    use Google Search grounding (the request is rejected outright), and a model
    asked about "recent news" with none supplied will invent specific, confident,
    wrong headlines. ``news_data.py`` supplies real ones without an API key.
  - A refresh is five agents x two risk profiles x {holdings, wishlist}, so up
    to 20 calls — more than the eight this replaced, but each prompt carries one
    slice instead of the whole picture, so they are individually much smaller.
    Watch the daily allowance on free tiers, and spread the load by configuring
    more than one provider.

Everything degrades gracefully: no key at all -> the advisor reports it's "not
configured" and the UI shows a hint; one agent failing -> the other four still
score and the error is noted; every agent failing -> the last good suggestions
stay on screen.

Not financial advice — suggestions are generated by a model and can be wrong.
"""

import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from backend.ai_agents import AGENT_KEYS, AGENTS

try:  # stdlib on Python 3.9+, used only for US market-hours math
    from zoneinfo import ZoneInfo

    _EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if tz data is unavailable
    _EASTERN = None


# --- US market hours ----------------------------------------------------
#
# The advisor generates once per trading day, at the opening bell, so what the
# scheduler needs is not "is it trading hours" but "when did the market last
# open" — the bell is the trigger, not an elapsed timer.

_OPEN_HOUR, _OPEN_MINUTE = 9, 30      # 9:30 ET
_CLOSE_MINUTE = 16 * 60               # 16:00 ET

# Without tz data we can't know when 9:30 ET is, so approximate with a fixed
# UTC time. 14:30 UTC is exactly the open in winter (EST) and an hour after it
# in summer (EDT) — late rather than early, which is the safe direction: firing
# before the bell would score the day on the previous close.
_FALLBACK_OPEN_HOUR, _FALLBACK_OPEN_MINUTE = 14, 30

# A weekend is at most two days, so five days back or forward always spans a
# weekday. Holidays are ignored throughout — on a closed Monday the refresh
# simply runs against unchanged prices, which is harmless.
_MARKET_DAY_SEARCH = 5


def is_market_open(now_utc: datetime = None) -> bool:
    """True during regular US market hours (Mon-Fri, 9:30-16:00 ET).

    Ignores holidays. Only used for the status line the UI shows; the refresh
    schedule keys off ``last_market_open`` instead.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    if _EASTERN is None:
        return now_utc.weekday() < 5
    et = now_utc.astimezone(_EASTERN)
    if et.weekday() >= 5:  # Saturday / Sunday
        return False
    minutes = et.hour * 60 + et.minute
    return _OPEN_HOUR * 60 + _OPEN_MINUTE <= minutes <= _CLOSE_MINUTE


def _market_local(now_utc: datetime) -> datetime:
    """``now_utc`` in market-local terms — Eastern when tz data is available,
    UTC as the documented approximation when it isn't."""
    return now_utc.astimezone(_EASTERN) if _EASTERN is not None else now_utc


def _open_on(local_day: datetime) -> datetime:
    """The UTC instant the market opens on ``local_day``'s calendar date."""
    hour, minute = (
        (_OPEN_HOUR, _OPEN_MINUTE)
        if _EASTERN is not None
        else (_FALLBACK_OPEN_HOUR, _FALLBACK_OPEN_MINUTE)
    )
    return local_day.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ).astimezone(timezone.utc)


def last_market_open(now_utc: datetime = None):
    """The most recent opening bell at or before ``now_utc``, in UTC.

    On a Sunday this is Friday's open; at 08:00 on a Monday it is still
    Friday's, because Monday's bell hasn't rung yet.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    local = _market_local(now_utc)
    for back in range(_MARKET_DAY_SEARCH):
        day = local - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        opened = _open_on(day)
        if opened <= now_utc:
            return opened
    return None


def next_market_open(now_utc: datetime = None):
    """The next opening bell strictly after ``now_utc``, in UTC."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local = _market_local(now_utc)
    for ahead in range(_MARKET_DAY_SEARCH):
        day = local + timedelta(days=ahead)
        if day.weekday() >= 5:
            continue
        opens = _open_on(day)
        if opens > now_utc:
            return opens
    return None


# --- Claude client (Messages API over urllib) ---------------------------

# The structured shape we force Claude to return, per risk profile. No numeric
# constraints (the API's structured-output schema doesn't support them) — the
# horizon is capped to three months and the confidence to 0-100 in the prompt,
# and both are clamped in code.
_SUGGESTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "portfolio_note": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ticker": {"type": "string"},
                    # The model's own 0-100 conviction: 100 = buy hard, 50 =
                    # hold, 0 = sell out. This is what gets blended.
                    "confidence": {"type": "integer"},
                    "action": {
                        "type": "string",
                        "enum": ["buy", "hold", "trim", "sell"],
                    },
                    # 1, 2 or 3 — months, not days. An integer this small is
                    # unambiguous; "47 days" would invite spurious precision.
                    "horizon_months": {"type": "integer"},
                    "headline": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "price_trigger": {"type": "string"},
                    "risks": {"type": "string"},
                },
                "required": [
                    "ticker",
                    "confidence",
                    "action",
                    "horizon_months",
                    "headline",
                    "reasoning",
                    "price_trigger",
                    "risks",
                ],
            },
        },
    },
    "required": ["portfolio_note", "suggestions"],
}


class ClaudeClient:
    """Minimal Claude Messages API client over the standard library.

    One method — ``complete_json`` — asks Claude for a response constrained to a
    JSON schema (structured outputs) and returns the parsed object. Uses adaptive
    thinking so the model reasons before committing to an answer.
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, api_key: str = None, model: str = "claude-opus-4-8",
                 timeout: int = 180):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.label = f"claude:{model}"

    def available(self) -> bool:
        """True when an Anthropic API key is configured."""
        return bool(self.api_key)

    def complete_json(self, system: str, prompt: str, schema: dict,
                      max_tokens: int = 16000) -> dict:
        """Send one request and return the parsed JSON object Claude produced.

        Raises RuntimeError with a readable message on any API/parse failure so
        the service can surface it and keep the last good suggestions on screen.
        """
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")

        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": max_tokens,
                # Adaptive thinking: Claude decides how much to reason. Effort
                # medium keeps cost/latency sensible for a periodic job.
                "thinking": {"type": "adaptive"},
                "output_config": {
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": schema},
                },
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL,
            data=body,
            method="POST",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self.ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = self._error_detail(e)
            raise RuntimeError(f"Claude API error ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach the Claude API: {e.reason}")

        # With structured outputs the model returns valid JSON in a text block.
        # Adaptive thinking may prepend thinking blocks, so scan for the text.
        text = next(
            (b.get("text") for b in data.get("content", []) if b.get("type") == "text"),
            None,
        )
        if not text:
            raise RuntimeError("Claude returned no text content.")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError("Claude returned malformed JSON.")

    @staticmethod
    def _error_detail(e: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
            return payload.get("error", {}).get("message", "unknown error")
        except Exception:
            return "unknown error"


class OllamaClient:
    """Local-model client (Ollama) with the same interface as ClaudeClient.

    Runs the model on your own machine — nothing leaves the laptop and no API key
    is needed. Talks to a local Ollama server (``ollama serve``) over the
    standard library, using its structured-output support (``format`` = a JSON
    schema) so the model returns JSON matching our suggestion shape.

    Swap this in for ClaudeClient and ``AIAdvisorService`` works unchanged.
    """

    def __init__(self, host: str = None, model: str = "llama3.1",
                 timeout: int = 300):
        self.host = (host or "http://localhost:11434").rstrip("/")
        self.model = model
        self.timeout = timeout
        self.label = f"ollama:{model}"

    def available(self) -> bool:
        """Local model is always 'configured' — reachability problems surface as
        clear errors at generation time instead."""
        return True

    def complete_json(self, system: str, prompt: str, schema: dict,
                      max_tokens: int = 16000) -> dict:
        """Send one chat request constrained to the JSON schema and return the
        parsed object. Raises RuntimeError with a readable message on failure."""
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                # Ollama structured outputs: pass the JSON schema directly.
                "format": schema,
                "options": {"num_predict": max_tokens, "temperature": 0.3},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            self.host + "/api/chat",
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = self._error_detail(e)
            # A missing model is the most common setup error — make it actionable.
            if "not found" in detail.lower() or e.code == 404:
                raise RuntimeError(
                    f"Ollama model '{self.model}' isn't installed. "
                    f"Run: ollama pull {self.model}"
                )
            raise RuntimeError(f"Ollama error ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not reach Ollama at {self.host} — is `ollama serve` "
                f"running? ({e.reason})"
            )

        content = (data.get("message") or {}).get("content")
        if not content:
            raise RuntimeError("Ollama returned no content.")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise RuntimeError("Ollama returned malformed JSON.")

    @staticmethod
    def _error_detail(e: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
            return payload.get("error", "unknown error")
        except Exception:
            return "unknown error"


# Transient upstream conditions worth a retry: overloaded models (503, which
# free tiers see regularly), rate limits, gateway errors, and dropped
# connections. Anything else — a bad key, an unknown model, malformed JSON — is
# permanent and retrying only delays the report.
_TRANSIENT_MARKERS = (
    "(429)",
    "(500)",
    "(502)",
    "(503)",
    "(504)",
    "high demand",
    "overloaded",
    "timed out",
    "timeout",
    "temporarily",
    "could not reach",
    # Connection-level failures. These matter more than they look: the score is
    # the average of the agents, so one dropped call costs a whole dimension of
    # a risk profile's score. A long prompt held open for tens of seconds gets its
    # connection cut often enough that not retrying loses a model outright —
    # observed as a bare "Remote end closed connection without response", which
    # http.client raises outside the urllib error types the clients catch.
    "remote end closed",
    "connection reset",
    "connection aborted",
    "connection refused",
    "broken pipe",
    "incompleteread",
    "incomplete read",
    "eof occurred",
    "ssl",
)


def _is_transient(error: Exception) -> bool:
    """True when retrying the call has a real chance of working.

    Matches on the message rather than the exception type because the clients
    normalise everything to RuntimeError, and because the same condition
    surfaces with different types across providers.
    """
    message = str(error).lower()
    # A daily allowance doesn't come back before the quota resets, whatever
    # retry delay the provider suggests. Retrying it holds a worker open for
    # minutes, delays the whole refresh, and still fails — so don't.
    if "perday" in message or "per day" in message:
        return False
    if any(marker in message for marker in _TRANSIENT_MARKERS):
        return True
    # Belt and braces: connection-level errors that reached us unwrapped.
    return isinstance(error, (ConnectionError, TimeoutError))


# A rate-limited provider usually says exactly how long its window has left —
# Gemini as "Please retry in 36.995s", others as a "retryDelay": "37s" field.
_RETRY_HINT_RE = re.compile(r"retry(?:delay)?[\"':\s]*(?:in\s+)?([\d.]+)\s*s", re.I)

# Waiting out a quota window is worth it; waiting out an outage is not.
_MAX_RETRY_WAIT = 75.0


def _retry_after(error: Exception) -> float:
    """Seconds the provider asked us to wait, or 0 when it didn't say.

    Blind exponential backoff is useless against a per-minute quota: doubling
    from 2s gives up after ~6s, while the window has 37s left to run. Honouring
    the number the provider actually returned is the difference between losing
    a model for the whole refresh and simply answering late.
    """
    match = _RETRY_HINT_RE.search(str(error))
    if not match:
        return 0.0
    try:
        return min(float(match.group(1)), _MAX_RETRY_WAIT)
    except ValueError:
        return 0.0


def _extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating markdown fences or
    stray prose around it. Raises RuntimeError if nothing parseable is found."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost {...} span (handles ```json fences / preamble).
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise RuntimeError("Model returned malformed JSON.")


class LlamaClient:
    """Meta-internal Llama API client (MetaGen), same interface as the others.

    Uses the company-provided, OpenAI-compatible endpoint — free/self-serve for
    employees, callable from a local laptop on the corp network (VPN) with a
    Bearer key, and keeps inference on Meta-hosted models. Stdlib ``urllib`` only.

      POST {base}/compat/v1/chat/completions   (OpenAI Chat Completions shape)
      Authorization: Bearer LLM|<app-id>|<secret>

    Get a key at https://www.internalfb.com/metagen/tools/llm-api-keys and an
    entitlement at https://www.internalfb.com/metagen/tools/entitlements.
    """

    def __init__(self, api_key: str = None,
                 model: str = "llama4-scout-17b-16e-instruct",
                 base_url: str = "https://api.llama.com", timeout: int = 180):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.label = f"llama:{model}"

    def available(self) -> bool:
        """True when a Llama API key is configured."""
        return bool(self.api_key)

    def complete_json(self, system: str, prompt: str, schema: dict,
                      max_tokens: int = 16000) -> dict:
        """Send one chat request and return the parsed JSON object.

        The compat endpoint may not enforce a JSON schema, so we ask for a JSON
        object, embed the schema in the instructions, and parse defensively.
        """
        if not self.api_key:
            raise RuntimeError("LLAMA_API_KEY is not set.")

        system_with_schema = (
            f"{system}\n\nReturn ONLY a single JSON object — no markdown, no "
            f"prose — that matches this JSON schema:\n{json.dumps(schema)}"
        )
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_with_schema},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "max_completion_tokens": max_tokens,
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            self.base_url + "/compat/v1/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = self._error_detail(e)
            low = detail.lower()
            if e.code == 403 and "corp" in low:
                raise RuntimeError(
                    "Llama API needs the corp network — connect to VPN and retry."
                )
            if (
                "does not have access" in low
                or "invalid model" in low
                or "not available" in low
            ):
                raise RuntimeError(
                    f"Llama model '{self.model}' isn't available to your key. "
                    "Grant model access on your MetaGen entitlement (its "
                    "'Available Models' tab), then set LLAMA_MODEL to a listed "
                    "name. Details: " + detail
                )
            raise RuntimeError(f"Llama API error ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not reach the Llama API at {self.base_url} "
                f"(on VPN?): {e.reason}"
            )

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("Llama API returned an unexpected response shape.")
        if not content:
            raise RuntimeError("Llama API returned no content.")
        return _extract_json(content)

    @staticmethod
    def _error_detail(e: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
            err = payload.get("error")
            if isinstance(err, dict):
                return err.get("message", "unknown error")
            return err or payload.get("message", "unknown error")
        except Exception:
            return "unknown error"


class GroqClient:
    """Groq API client — free tier, no credit card, very fast open-weight models.

    Same interface as the other clients, so it drops straight into the consensus
    pair. Groq speaks the OpenAI Chat Completions shape::

      POST https://api.groq.com/openai/v1/chat/completions
      Authorization: Bearer gsk_...

    Get a free key at https://console.groq.com/keys — no card required. Groq is
    an *inference provider* running open-weight models (Llama, Qwen, Kimi); it
    is unrelated to xAI's "Grok", which has no comparable free tier.

    We ask for ``response_format: json_object`` rather than a strict schema:
    schema support varies by model on Groq, and ``_extract_json`` plus the
    service's ``_clean_profile`` already normalise whatever comes back.

    Env:
      GROQ_API_KEY  — required
      GROQ_MODEL    — optional, default ``llama-3.3-70b-versatile``
      GROQ_API_BASE — optional, default ``https://api.groq.com/openai/v1``
    """

    def __init__(self, api_key: str = None,
                 model: str = "llama-3.3-70b-versatile",
                 base_url: str = "https://api.groq.com/openai/v1",
                 timeout: int = 180):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.label = f"groq:{model}"

    def available(self) -> bool:
        """True when a Groq API key is configured."""
        return bool(self.api_key)

    def complete_json(self, system: str, prompt: str, schema: dict,
                      max_tokens: int = 16000) -> dict:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set.")

        system_with_schema = (
            f"{system}\n\nReturn ONLY a single JSON object — no markdown, no "
            f"prose — that matches this JSON schema:\n{json.dumps(schema)}"
        )
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_with_schema},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "temperature": 0.3,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = self._error_detail(e)
            low = detail.lower()
            if e.code == 429:
                raise RuntimeError(
                    f"Groq rate limited (429): {detail}. Free-tier limits are "
                    "per-model — wait, or set GROQ_MODEL to a smaller model."
                )
            if e.code in (401, 403):
                # Groq answers a bad key with 403 and often an empty body, so
                # say what to check rather than passing "unknown error" along.
                raise RuntimeError(
                    f"Groq rejected the API key ({e.code}) — check GROQ_API_KEY "
                    f"at https://console.groq.com/keys. Details: {detail}"
                )
            if e.code == 404 or "decommissioned" in low or "does not exist" in low:
                raise RuntimeError(
                    f"Groq model '{self.model}' isn't available. Pick a current "
                    "one from https://console.groq.com/docs/models and set "
                    f"GROQ_MODEL. Details: {detail}"
                )
            raise RuntimeError(f"Groq API error ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not reach the Groq API at {self.base_url} ({e.reason})"
            )

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("Groq API returned an unexpected response shape.")
        if not content:
            raise RuntimeError("Groq API returned no content.")
        return _extract_json(content)

    @staticmethod
    def _error_detail(e: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
            err = payload.get("error")
            if isinstance(err, dict):
                return err.get("message", "unknown error")
            return err or payload.get("message", "unknown error")
        except Exception:
            return "unknown error"


class GeminiClient:
    """Google Gemini API client — free tier, no credit card, 1M-token context.

    Uses Google's native ``generateContent`` endpoint over stdlib ``urllib``
    (no SDK, matching the rest of the app), with ``responseMimeType:
    application/json`` plus ``responseSchema`` to force structured JSON output.
    Falls back to an OpenAI-compatible endpoint when the base URL contains
    ``/openai``.

    Get a key at https://aistudio.google.com/app/apikey

    NOTE — Google Search grounding is NOT usable on a free key. Adding
    ``tools: [{"google_search": {}}]`` makes the request fail immediately with
    ``429 RESOURCE_EXHAUSTED`` (a zero-quota entitlement, not burst limiting),
    while the same ungrounded request succeeds. That is why this app feeds the
    model headlines from ``news_data.py`` instead of asking it to search: given
    no news, these models reliably invent confident, wrong headlines.

    Free-tier request limits vary by model and change often — check
    https://ai.google.dev/gemini-api/docs/rate-limits rather than trusting a
    number here.

    Mind the DAILY allowance, not just the per-minute one. A refresh costs
    ``agents x risk profiles x {holdings, wishlist}`` requests — up to 20 —
    spread across whatever models are configured. The auto-refresh fires once
    per trading day at the open, so that is roughly the whole daily budget in a
    single burst. Against a free tier of 20 requests/day (as
    ``gemini-3.6-flash`` was) one model cannot carry all five agents on its
    own: configure a second provider so the round-robin has somewhere to go,
    or move the key to a paid tier. An agent that runs dry drops out and its
    whole dimension goes missing from the score.

    Env:
      GEMINI_API_KEY  — required
      GEMINI_MODEL    — optional, default ``gemini-flash-lite-latest``
      GEMINI_API_BASE — optional, default
                       ``https://generativelanguage.googleapis.com/v1beta``

    For OpenAI-compatible mode, set::
      GEMINI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
    """

    def __init__(self, api_key: str = None,
                 model: str = "gemini-flash-lite-latest",
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta",
                 timeout: int = 180):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.label = f"gemini:{model}"

    def available(self) -> bool:
        return bool(self.api_key)

    def complete_json(self, system: str, prompt: str, schema: dict,
                      max_tokens: int = 16000) -> dict:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")

        system_with_schema = (
            f"{system}\n\nReturn ONLY a single JSON object — no markdown, no "
            f"prose — that matches this JSON schema:\n{json.dumps(schema)}"
        )

        # If user pointed at the OpenAI-compatible base, use that path.
        if "openai" in self.base_url.lower():
            return self._complete_openai_compat(system_with_schema, prompt,
                                                 schema, max_tokens)
        return self._complete_native(system_with_schema, prompt,
                                      schema, max_tokens)

    # --- native generateContent path (default, recommended) -------------

    def _complete_native(self, system_with_schema: str, prompt: str,
                         schema: dict, max_tokens: int) -> dict:
        # Gemini native endpoint uses key as query param (also accepts
        # x-goog-api-key header, but query param is simplest with urllib).
        url = (f"{self.base_url}/models/{self.model}:generateContent"
               f"?key={urllib.parse.quote(self.api_key)}")

        body = json.dumps(
            {
                "systemInstruction": {
                    "parts": [{"text": system_with_schema}]
                },
                "contents": [
                    {"role": "user", "parts": [{"text": prompt}]}
                ],
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": max_tokens,
                    # Force JSON output; schema enforcement is optional but
                    # responseMimeType alone is enough + our defensive parse.
                    "responseMimeType": "application/json",
                    # Including responseSchema helps accuracy; Gemini accepts a
                    # simplified JSON schema. Strip additionalProperties which it
                    # may reject on some models.
                    "responseSchema": self._strip_additional_properties(schema),
                },
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = self._error_detail(e)
            low = detail.lower()
            if e.code == 429:
                raise RuntimeError(
                    f"Gemini API rate limited (429): {detail}. "
                    f"Free tier: 15 RPM / 200-1000 RPD depending on model. "
                    f"Try {self.model} or wait."
                )
            if e.code == 400 and "api key" in low:
                raise RuntimeError(f"Gemini API key invalid: {detail}")
            raise RuntimeError(f"Gemini API error ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not reach Gemini API at {self.base_url} ({e.reason})"
            )
        except Exception as e:
            # http.client can drop a long request with RemoteDisconnected,
            # which is neither an HTTPError nor a URLError and would otherwise
            # escape as a bare, unretryable message.
            raise RuntimeError(
                f"Could not reach Gemini API at {self.base_url} "
                f"({type(e).__name__}: {e})"
            )

        # Expected shape: {candidates: [{content: {parts: [{text: "..."}]}}]}
        try:
            candidates = data.get("candidates") or []
            content = candidates[0].get("content", {}) if candidates else {}
            parts = content.get("parts") or []
            text = parts[0].get("text") if parts else None
        except (KeyError, IndexError, TypeError):
            text = None

        if not text:
            # Include full payload in error for debugging (truncated)
            preview = json.dumps(data)[:500]
            raise RuntimeError(
                f"Gemini API returned no text content: {preview}"
            )
        return _extract_json(text)

    # --- OpenAI-compatible path (optional) --------------------------------

    def _complete_openai_compat(self, system_with_schema: str, prompt: str,
                                schema: dict, max_tokens: int) -> dict:
        url = self.base_url + "/chat/completions"
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_with_schema},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": max_tokens,
                "max_completion_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = self._error_detail(e)
            if e.code == 429:
                raise RuntimeError(f"Gemini API rate limited (429): {detail}")
            raise RuntimeError(f"Gemini API error ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not reach Gemini API at {self.base_url} ({e.reason})"
            )

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("Gemini API returned an unexpected response shape.")
        if not content:
            raise RuntimeError("Gemini API returned no content.")
        return _extract_json(content)

    @staticmethod
    def _strip_additional_properties(schema: dict) -> dict:
        """Remove ``additionalProperties`` keys which some Gemini models reject.
        Deep copies only the levels we care about, to stay minimal."""
        def _strip(obj):
            if isinstance(obj, dict):
                out = {}
                for k, v in obj.items():
                    if k == "additionalProperties":
                        continue
                    out[k] = _strip(v)
                return out
            if isinstance(obj, list):
                return [_strip(x) for x in obj]
            return obj
        try:
            return _strip(schema)
        except Exception:
            # If stripping fails, return original — API may still accept.
            return schema

    @staticmethod
    def _error_detail(e: urllib.error.HTTPError) -> str:
        try:
            raw = e.read().decode("utf-8", "replace")
            payload = json.loads(raw)
            # Gemini error shape: {"error": {"message": "...", "code": ...}}
            err = payload.get("error")
            if isinstance(err, dict):
                message = err.get("message", raw[:500])
                # A 429's message reads the same whether the limit is a burst
                # that clears in seconds or a daily allowance that doesn't —
                # only the quota id in `details` says which, and the retry
                # logic has to know. Carry it through on the message.
                quotas = [
                    violation.get("quotaId")
                    for detail in err.get("details") or []
                    if str(detail.get("@type", "")).endswith("QuotaFailure")
                    for violation in detail.get("violations") or []
                    if violation.get("quotaId")
                ]
                if quotas:
                    message = f"{message} [quota: {', '.join(quotas)}]"
                return message
            if isinstance(err, str):
                return err
            return payload.get("message", raw[:500])
        except Exception:
            try:
                return e.read().decode("utf-8", "replace")[:500]
            except Exception:
                return "unknown error"


# --- Confidence scoring -------------------------------------------------
#
# One number per stock, 0-100, the weighted average of the five agents. See the
# module docstring for the scale; this section is the whole of the arithmetic.

# How much each agent counts toward the blended score. Equal by default —
# nothing about the five dimensions says one deserves more say a priori, and
# starting equal makes any later imbalance a choice the user made rather than
# one the app made quietly. Overridden per portfolio via ``ai_weights.json``
# (edited from the UI) or at boot via AI_AGENT_WEIGHTS.
DEFAULT_AGENT_WEIGHTS = {key: 1.0 for key in AGENT_KEYS}

# A weight is a multiplier on one voice, not a budget. Zero silences an agent
# outright (a legitimate choice — "I don't care what the street thinks"); the
# ceiling only exists so a slip of a decimal point can't make one agent the
# whole score while appearing to leave the others in.
_MIN_AGENT_WEIGHT = 0.0
_MAX_AGENT_WEIGHT = 5.0

# The neutral point of the scale: no opinion either way.
NEUTRAL_CONFIDENCE = 50.0

# Score -> (action, label), highest threshold first. The action keys are the
# ones the UI already colours: buy green, hold grey, trim amber, sell red.
_CONFIDENCE_BANDS = (
    (80, "buy", "Strong buy"),
    (65, "buy", "Buy"),
    (55, "buy", "Lean buy"),
    (45, "hold", "Hold"),
    (35, "trim", "Lean trim"),
    (20, "trim", "Trim"),
    (0, "sell", "Sell"),
)

# Fallback when a model names an action but omits (or mangles) its score: use
# the middle of that action's band so the source still contributes sensibly.
_ACTION_CONFIDENCE = {"buy": 72.0, "hold": 50.0, "trim": 32.0, "sell": 12.0}

# The score a model may report for a given action. These exist because models
# genuinely misread the scale: one returned confidence 100 alongside "sell" and
# the headline "Sell to eliminate a 35% loss" — i.e. "100% sure about selling",
# the exact inverse of what the number means here. Left alone, that one flipped
# vote swings the blended score by ~30 points.
#
# The action is a four-value enum the agents have always got right, so it acts
# as a guardrail: a score outside its action's range is pulled to the nearest
# edge rather than taken at face value. A model that used the scale correctly
# is always inside its range and passes through untouched.
_ACTION_RANGES = {
    "buy": (55.0, 100.0),
    "hold": (40.0, 60.0),
    "trim": (20.0, 45.0),
    "sell": (0.0, 25.0),
}


def clamp_confidence(value):
    """Coerce a model-supplied score into 0-100, or None if it isn't a number.

    Models occasionally answer on a 0-1 or 0-10 scale despite the instructions;
    both are rescaled rather than thrown away, since a 0.8 clamped to 1 would
    read as "sell everything" — the exact opposite of what was meant.
    """
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score:  # NaN
        return None
    if 0.0 < score <= 1.0:
        score *= 100.0
    elif 1.0 < score <= 10.0:
        score *= 10.0
    return max(0.0, min(100.0, score))


def action_for_confidence(score) -> str:
    """The action label ("buy" / "hold" / "trim" / "sell") a score implies."""
    return _band(score)[0]


def label_for_confidence(score) -> str:
    """The human phrase for a score — "Strong buy", "Hold", "Trim", ..."""
    return _band(score)[1]


def _band(score):
    if score is None:
        return ("hold", "Hold")
    for threshold, action, label in _CONFIDENCE_BANDS:
        if score >= threshold:
            return (action, label)
    return ("sell", "Sell")


# How far apart the agents' scores may sit before we call it a disagreement.
# Measured as a spread (highest score minus lowest) rather than by comparing
# action labels: on a continuous scale, 50 vs. 55 is agreement that happens to
# straddle a band edge, while 50 vs. 92 is a real split even though both are
# nominally "buy".
#
# The thresholds widen with the number of agents, because the spread of five
# draws is naturally wider than the spread of two even when they are describing
# the same thing. Held at the old two-source numbers, five deliberately narrow
# views would have been reported as "split" on essentially every ticker, which
# is true in a useless way — the point of asking five different questions is
# that the answers differ.
_SPREADS = {
    2: (15.0, 35.0),   # (agree at or below, split above) — the old pair
    3: (18.0, 40.0),
    4: (20.0, 43.0),
    5: (22.0, 46.0),
}
_WIDEST_SPREAD = (22.0, 46.0)


def consensus_for(scores) -> str:
    """Classify how well the agents agree: single / agree / mixed / split."""
    usable = [s for s in scores if s is not None]
    if len(usable) < 2:
        return "single"
    agree_at, split_above = _SPREADS.get(len(usable), _WIDEST_SPREAD)
    spread = max(usable) - min(usable)
    if spread <= agree_at:
        return "agree"
    if spread <= split_above:
        return "mixed"
    return "split"


def _clean_horizon(suggestion: dict) -> int:
    """Normalise a suggestion's horizon to a whole number of months, 1-3.

    Models occasionally answer in days despite the instructions ("horizon_months:
    45"), so anything implausibly large is read as days and converted rather
    than clamped flat to 3 — 45 means six weeks, not a quarter. Also accepts the
    legacy ``horizon_days`` field so suggestions persisted before the switch to
    a quarterly view still load.
    """
    months = suggestion.get("horizon_months")
    if months is None and suggestion.get("horizon_days") is not None:
        # Pre-existing saved data, written when the view was a week.
        try:
            return max(_MIN_HORIZON_MONTHS, min(
                _MAX_HORIZON_MONTHS, round(float(suggestion["horizon_days"]) / 30.0)
            ))
        except (TypeError, ValueError):
            return _MIN_HORIZON_MONTHS
    try:
        value = float(months)
    except (TypeError, ValueError):
        return _MIN_HORIZON_MONTHS
    if value != value:  # NaN
        return _MIN_HORIZON_MONTHS
    if value > _MAX_HORIZON_MONTHS:
        # Almost certainly days. 30 -> 1 month, 90 -> 3 months.
        value = value / 30.0
    return max(_MIN_HORIZON_MONTHS, min(_MAX_HORIZON_MONTHS, round(value) or 1))


def _reconcile(ticker: str, action: str, confidence: float) -> float:
    """Pull a score back inside the range its action allows.

    Guards against a model reading "confidence" as certainty-in-its-own-call
    rather than buy-signal strength — see ``_ACTION_RANGES``. Returns the score
    unchanged when the two already agree, which is the overwhelming majority.
    """
    low, high = _ACTION_RANGES[action]
    if low <= confidence <= high:
        return confidence
    fixed = max(low, min(high, confidence))
    print(
        f"AI advisor: {ticker or '?'} scored {confidence:g} but called "
        f"'{action}' — reading that as {fixed:g}."
    )
    return fixed


def clamp_weight(value, fallback: float = 1.0) -> float:
    """Coerce a user-supplied agent weight into the allowed range."""
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return fallback
    if weight != weight:  # NaN
        return fallback
    return max(_MIN_AGENT_WEIGHT, min(_MAX_AGENT_WEIGHT, weight))


def normalize_weights(weights) -> dict:
    """A complete, clamped weight for every agent.

    Unknown keys are dropped and missing ones default to 1.0, so a weights file
    written by an older version — or by hand — can't leave an agent unweighted
    or smuggle in a source that doesn't exist. Setting every weight to zero
    would silence the whole panel, which is never what someone means, so that
    one case falls back to equal weights.
    """
    weights = weights if isinstance(weights, dict) else {}
    out = {
        key: clamp_weight(weights.get(key, DEFAULT_AGENT_WEIGHTS[key]))
        for key in AGENT_KEYS
    }
    if not any(out.values()):
        return dict(DEFAULT_AGENT_WEIGHTS)
    return out


def blend_confidence(sources) -> float:
    """Weighted average of the agents that produced a score.

    ``sources`` is the per-suggestion list of ``{confidence, weight, ...}``
    dicts — one per agent. An agent that had no evidence for this ticker, or
    whose call failed, is simply absent, so the weights of those that did answer
    are renormalised against each other and a lone survivor's score passes
    through unchanged rather than being dragged toward neutral by the gap.
    """
    total_weight = 0.0
    total = 0.0
    for source in sources:
        score = source.get("confidence")
        weight = source.get("weight") or 0.0
        if score is None or weight <= 0:
            continue
        total += score * weight
        total_weight += weight
    if not total_weight:
        return None
    return round(total / total_weight, 1)


# --- Advisor service ----------------------------------------------------

# The two risk profiles the advisor always produces, so the UI toggle is instant
# (no new API call on toggle — both are generated each refresh).
_RISK_PROFILES = ("low", "high")

# Suggestions look one to three months ahead. This matches the timescale the
# sell-side ratings the expert agent reads are written on, so the street's view
# and the agents' own are answering the same question.
_MIN_HORIZON_MONTHS = 1
_MAX_HORIZON_MONTHS = 3

# Attempts per (agent, model) call before handing the agent to the next model.
_MAX_ATTEMPTS = 3

# How many agent calls to keep in the air at once, per configured model. Two is
# right for the HTTP clients: the call is a single request that spends almost
# all its time waiting, and a free tier's per-minute quota is the real ceiling
# anyway.
_LANES_PER_CLIENT = 2


def _lanes_for(clients, jobs: int) -> int:
    """Thread-pool width for a fan-out of ``jobs`` calls over ``clients``.

    A client may override the default by exposing ``lanes``, because not every
    client is an HTTP request. The local-CLI client spawns a process per call,
    which is heavier than a socket but also isn't rate-limited by anyone, so it
    asks for more lanes than a hosted model should get. Never fewer than two,
    so a single model still overlaps its calls, and never more than there are
    calls to make.
    """
    total = sum(getattr(c, "lanes", _LANES_PER_CLIENT) for c in clients)
    return max(2, min(jobs, total))

# How often the scheduler wakes to look at the clock. A minute lands the daily
# refresh within a minute of the bell and costs nothing — the check is two
# datetime comparisons.
_TICK_SECONDS = 60

# The two sides of the app an agent can be asked about. Holdings first, always:
# with a per-minute quota in play the fan-out below queues in this order, and
# the panel is built around the holdings answers.
_KINDS = ("holdings", "wishlist")

# How much of a year of daily closes to hand the personal agent. Weekly is the
# right grain for a one-to-three-month pattern read — 250 daily points cost ten
# times the tokens to say the same thing, and invite the model to narrate noise.
_HISTORY_POINTS = 52

# Trading days per window, for the return figures computed off the daily series.
_RETURN_WINDOWS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}

# Filter for wishlist: only show buys where the blended confidence is a
# genuine buy signal (>= Lean buy). Anything in the hold/wait band is
# hidden entirely — the section renders as if it doesn't exist.
_WISHLIST_MIN_CONFIDENCE = 55


# --- Price history, measured ---------------------------------------------
#
# The personal agent is asked whether the current setup matches the patterns
# that have worked before, which means it needs the shape of the past year, not
# a wall of numbers. These helpers do the arithmetic here rather than in the
# prompt: a model handed 250 closes and asked for a six-month return will
# usually get it roughly right, occasionally get it badly wrong, and always
# spend tokens on it. Computed figures are exact and free.


def _pct(value, base):
    """``value`` as a percentage change from ``base``, or None."""
    if value is None or not base:
        return None
    return round((value - base) / base * 100, 2)


def _iso_day(ts_ms) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _volatility_pct(closes):
    """Annualised volatility of daily returns, in percent, or None.

    Log returns, sample standard deviation, sqrt-of-252 scaling — the ordinary
    convention, so the number means what a reader expects it to mean.
    """
    returns = [
        math.log(cur / prev)
        for prev, cur in zip(closes, closes[1:])
        if prev > 0 and cur > 0
    ]
    if len(returns) < 20:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round(math.sqrt(variance) * math.sqrt(252) * 100, 1)


def _price_path(points):
    """Downsample daily closes to roughly weekly, always ending on the latest.

    Plain slicing would drop the most recent close whenever the series length
    isn't a multiple of the step, which is the one point the agent most needs —
    so the last sample is replaced with the true last close.
    """
    step = max(1, len(points) // _HISTORY_POINTS)
    sampled = points[::step][-_HISTORY_POINTS:]
    if sampled and sampled[-1][0] != points[-1][0]:
        sampled = sampled[:-1] + [points[-1]]
    return [{"d": _iso_day(ts), "c": round(close, 2)} for ts, close in sampled]


def _history_stats(series):
    """A year of daily closes -> the measured pattern, or None if too thin.

    ``series`` is ``[(ts_ms, close), ...]`` oldest first, as ``market_data.py``
    returns it. A newly listed stock with a few weeks of history still gets
    whatever windows it can fill; the rest are simply absent.
    """
    points = [(ts, close) for ts, close in (series or []) if close]
    if len(points) < 10:
        return None

    closes = [close for _, close in points]
    last = closes[-1]
    out = {}

    for label, days in _RETURN_WINDOWS.items():
        if len(closes) > days:
            out[f"return_{label}_pct"] = _pct(last, closes[-1 - days])
    if "return_1y_pct" not in out and len(closes) >= 40:
        # Not a full year of history — say so rather than silently omitting the
        # long view, which would read as "flat over a year".
        out["return_since_listing_pct"] = _pct(last, closes[0])
        out["history_days"] = len(closes)

    high, low = max(closes), min(closes)
    out["range_high"] = round(high, 2)
    out["range_low"] = round(low, 2)
    out["vs_52w_high_pct"] = _pct(last, high)
    out["vs_52w_low_pct"] = _pct(last, low)

    for label, days in (("50d", 50), ("200d", 200)):
        if len(closes) >= days:
            out[f"vs_{label}_pct"] = _pct(last, sum(closes[-days:]) / days)

    volatility = _volatility_pct(closes)
    if volatility is not None:
        out["volatility_pct"] = volatility

    out["path"] = _price_path(points)
    return out


class AIAdvisorService:
    """Runs the five agents, blends their scores, caches and refreshes.

    Composes one *or more* LLM clients with the market/portfolio/summary
    services and the news, analyst and fundamentals providers. Produces both a
    low-risk and a high-risk set every refresh and caches them together, so the
    UI's risk toggle never triggers a new API call. Persists the latest result
    so a restart shows the last suggestions.

    Blending: each agent in ``ai_agents.AGENTS`` gets its own call with its own
    disjoint slice of the context (see that module), and the five scores are
    averaged with the per-agent weights. Every suggestion carries the full
    ``sources`` breakdown — which agent scored it what, on which model, at what
    weight, with its own headline, reasoning, trigger and risks — so the UI can
    show the whole argument rather than one voice, plus a ``consensus`` of
    ``agree`` / ``mixed`` / ``split`` / ``single`` measuring how far apart the
    five landed. Five narrow views disagreeing is the interesting case, and the
    panel says so.

    Because the raw per-agent scores are kept on every suggestion, changing the
    weights is pure arithmetic: ``set_weights`` re-blends in place without
    calling a model.

    All calls in a refresh run concurrently, so latency is roughly the slowest
    single call rather than their sum — bounded by the lane cap that keeps a
    free tier's per-minute quota intact.
    """

    def __init__(self, clients, news, market, portfolio, summary,
                 storage=None, analysts=None,
                 agent_weights=None, fundamentals=None, wishlist=None,
                 macro_news=None, weights_storage=None, sales=None):
        # Accept a single client or a list, so existing callers keep working.
        if not isinstance(clients, (list, tuple)):
            clients = [clients]
        self.clients = [c for c in clients if c is not None]
        self.news = news
        self.market = market
        self.portfolio = portfolio
        self.summary = summary
        self.storage = storage
        # Sell-side research — the expert agent's entire evidence base. Without
        # it that agent has nothing to read and drops out of the average.
        self.analysts = analysts
        # Company fundamentals, split between the company and statistics agents
        # by ``ai_agents.py``. Without it both of those agents go quiet.
        self.fundamentals = fundamentals
        # Market-wide headlines — the macro agent's entire evidence base.
        self.macro_news = macro_news
        # Wishlist: stocks you don't own yet but may want to buy. Optional;
        # when wired in the advisor also scores each wishlist name for a buy
        # entry and filters to the ones worth showing.
        self.wishlist = wishlist
        # The sales log, so the personal agent can see the investor's own
        # earlier exits in a name. Optional; falls back to the summary
        # service's log, which is the same object in the default wiring.
        self.sales = sales if sales is not None else getattr(summary, "sales", None)

        # Per-portfolio weights live on disk next to the suggestions; the
        # constructor argument (from AI_AGENT_WEIGHTS) is the default a
        # portfolio starts from before anyone touches the sliders.
        self.weights_storage = weights_storage
        self._default_weights = normalize_weights(
            {**DEFAULT_AGENT_WEIGHTS, **(agent_weights or {})}
        )
        self.weights = self._load_weights()

        self._lock = threading.Lock()
        self._refreshing = False
        self._latest = self._load_persisted()

    # --- agent weighting --------------------------------------------------

    def _agent_weight(self, key: str) -> float:
        """How much this agent counts toward the blended score."""
        return float(self.weights.get(key, DEFAULT_AGENT_WEIGHTS.get(key, 1.0)))

    def _load_weights(self) -> dict:
        """The active portfolio's saved weights, or the boot defaults."""
        if self.weights_storage is None:
            return dict(self._default_weights)
        try:
            saved = (self.weights_storage.load() or {}).get("weights")
        except Exception:
            saved = None
        if not saved:
            return dict(self._default_weights)
        return normalize_weights({**self._default_weights, **saved})

    def set_weights(self, weights) -> dict:
        """Change the agent weights and re-blend the cached scores in place.

        No model is called. Every suggestion already carries each agent's raw
        score, so the new average is arithmetic over data we have — which is
        what makes the sliders usable: drag one, see the whole panel resettle,
        spend nothing. Returns the weights actually stored (clamped and
        completed).
        """
        self.weights = normalize_weights(weights)
        self._persist_weights()
        with self._lock:
            if self._latest and self._latest.get("risk_profiles"):
                self._latest = {
                    **self._latest,
                    "risk_profiles": {
                        risk: self._reblend_profile(profile)
                        for risk, profile in self._latest["risk_profiles"].items()
                    },
                }
                self._persist(self._latest)
        return dict(self.weights)

    # --- public API -----------------------------------------------------

    def available(self) -> bool:
        """True when at least one model is configured and usable."""
        return any(c.available() for c in self.clients)

    def active_clients(self) -> list:
        """The configured models, in the order agents are assigned to them."""
        return [c for c in self.clients if c.available()]

    def analysts_available(self) -> bool:
        """True when the expert agent has sell-side research to read."""
        return self.analysts is not None and self.analysts.available()

    def fundamentals_available(self) -> bool:
        """True when the company and statistics agents have figures to read."""
        return self.fundamentals is not None and self.fundamentals.available()

    def macro_available(self) -> bool:
        """True when the macro agent has a market-wide news tape to read."""
        return self.macro_news is not None and self.macro_news.available()

    def get(self) -> dict:
        """Return the latest cached suggestions plus status for the UI."""
        latest = self._latest
        upcoming = next_market_open()
        return {
            "configured": self.available(),
            "models": [c.label for c in self.active_clients()],
            # Who the agents are, so the UI renders the weight controls and the
            # per-agent breakdown from the roster rather than hardcoding five.
            "agents": [a.describe() for a in AGENTS],
            "agent_weights": dict(self.weights),
            "default_agent_weights": dict(DEFAULT_AGENT_WEIGHTS),
            "news_configured": self.news.available(),
            "news_sources": getattr(self.news, "describe", lambda: "")(),
            # Which agents actually have an evidence source wired in.
            "analysts_configured": self.analysts_available(),
            "analyst_source": (
                self.analysts.describe() if self.analysts_available() else None
            ),
            "fundamentals_configured": self.fundamentals_available(),
            "fundamentals_source": (
                self.fundamentals.describe()
                if self.fundamentals_available()
                else None
            ),
            "macro_configured": self.macro_available(),
            "macro_source": (
                self.macro_news.describe() if self.macro_available() else None
            ),
            "refreshing": self._refreshing,
            "market_open": is_market_open(),
            # Once a trading day, at the bell. `next_refresh` is when that
            # will next happen, so the UI can say so rather than leaving a
            # day-old timestamp looking stale.
            "refresh_schedule": "daily at market open",
            "next_refresh": upcoming.isoformat() if upcoming else None,
            "generated_at": latest.get("generated_at") if latest else None,
            "risk_profiles": latest.get("risk_profiles") if latest else None,
            "model_errors": latest.get("model_errors") if latest else None,
            "error": latest.get("error") if latest else None,
        }

    def reload(self) -> None:
        """Re-read persisted suggestions and weights for the now-active
        portfolio.

        Called after the active portfolio is switched or deleted: the cached
        ``_latest`` and weights belong to the old portfolio, so drop them and
        load whatever the new one has (which may be nothing yet)."""
        with self._lock:
            self.weights = self._load_weights()
            self._latest = self._load_persisted()

    def refreshing(self) -> bool:
        """True while a generation is in flight.

        Public because the discover panel schedules itself around it: both draw
        on the same daily model quota, and firing them at the same bell would
        put twice the burst through a free tier at once.
        """
        return self._refreshing

    def request_refresh(self) -> bool:
        """Kick off a background regeneration. Returns False if one is running
        or the advisor isn't configured (nothing to do)."""
        if not self.available() or self._refreshing:
            return False
        threading.Thread(target=self._safe_generate, daemon=True).start()
        return True

    def start_scheduler(self):
        """Start the background loop: generate once on boot (if nothing is
        cached), then once per trading day at the opening bell."""
        if not self.available():
            print("AI advisor: no model configured — advisor disabled.")
            return
        labels = ", ".join(c.label for c in self.active_clients())
        upcoming = next_market_open()
        print(
            f"AI advisor: {labels} — refreshing daily at the opening bell"
            + (f" (next {upcoming.isoformat()})" if upcoming else "")
        )
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    # --- scheduler ------------------------------------------------------

    def _scheduler_loop(self):
        # Generate immediately on boot if we have nothing to show yet — a blank
        # panel until the next bell would be worse than one extra refresh.
        if self._latest is None:
            self._safe_generate()
        while True:
            time.sleep(_TICK_SECONDS)
            if self._refresh_due():
                self._safe_generate()

    def _refresh_due(self) -> bool:
        """True when the last generation predates the most recent opening bell.

        This is what makes the schedule "once a day at the open" rather than
        "every N hours": the trigger is the bell, not an elapsed timer. Two
        consequences worth naming, both deliberate:

          - It **catches up**. A laptop asleep at 9:30 refreshes as soon as it
            wakes, rather than skipping the day and showing yesterday's numbers
            until tomorrow. That can mean a refresh outside market hours, or on
            a Saturday when Friday's was missed — but only ever one, since the
            next check finds the generation is newer than the bell.
          - It **can't double-fire**. Restarting the app ten times after the
            open regenerates nothing; the cached result is already newer than
            today's bell.
        """
        opened = last_market_open()
        if opened is None:  # no weekday found — only possible without tz data
            return False
        last = self._generated_at_epoch()
        return last is None or last < opened.timestamp()

    def _safe_generate(self):
        """Run one generation, guarded so only one runs at a time. Never raises."""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        try:
            self._generate()
        except Exception as e:  # keep the last good result on screen
            print(f"AI advisor: generation failed: {e}")
            if self._latest is None:
                self._latest = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "risk_profiles": None,
                    "error": str(e),
                }
        finally:
            self._refreshing = False

    # --- generation -----------------------------------------------------

    def _generate(self):
        context = self._gather_context()
        clients = self.active_clients()
        if not clients:
            raise RuntimeError("No AI model is configured.")

        wishlist_tickers = [w["ticker"] for w in context.get("wishlist", [])]
        kinds = ["holdings"] + (["wishlist"] if wishlist_tickers else [])

        # Every (agent x risk profile x kind) triple is one independent call.
        # Fan them all out at once so a refresh costs roughly one round-trip of
        # wall-clock rather than the sum of twenty.
        #
        # Holdings jobs are queued first, and agents in roster order within
        # them: with a per-minute quota in play the lane cap below decides who
        # gets the first wave, and the panel is built around the holdings
        # answers. An empty portfolio still gets its holdings calls — that is
        # what produces the note the panel leads with.
        all_jobs = [
            (agent, risk, kind)
            for kind in kinds
            for risk in _RISK_PROFILES
            for agent in AGENTS
        ]

        # raw[kind][risk][agent_key] = (model_label, cleaned_profile)
        raw = {kind: {risk: {} for risk in _RISK_PROFILES} for kind in _KINDS}
        errors = []

        tickers = [h["ticker"] for h in context["holdings"]]
        lanes = _lanes_for(clients, len(all_jobs))
        with ThreadPoolExecutor(max_workers=lanes) as pool:
            futures = {
                pool.submit(self._run_agent, agent, risk, kind, context, clients,
                            "advisor"):
                    (agent, risk, kind)
                for agent, risk, kind in all_jobs
            }
            for future in as_completed(futures):
                agent, risk, kind = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    errors.append(f"{agent.name} ({risk}/{kind}): {e}")
                    continue
                if result is None:
                    continue  # the agent had no evidence for this side
                label, cleaned = result
                raw[kind][risk][agent.key] = (label, cleaned)

        # The street's research, kept alongside the result so the detail card
        # can show what the expert agent was actually reading.
        street = {
            h["ticker"]: h.get("wall_street")
            for h in context["holdings"]
            if h.get("wall_street")
        }
        w_street = {
            w["ticker"]: w.get("wall_street")
            for w in context.get("wishlist", [])
            if w.get("wall_street")
        }

        profiles = {}
        for risk in _RISK_PROFILES:
            holdings_votes = self._ordered_votes(raw["holdings"][risk])
            wishlist_votes = self._ordered_votes(raw["wishlist"][risk])
            if holdings_votes:
                profile = self._merge_agents(holdings_votes, street, tickers)
            elif wishlist_votes:
                # No agent answered on holdings — or there are none — but the
                # wishlist did. Give those buys somewhere to live rather than
                # dropping the whole risk profile.
                profile = self._empty_profile(wishlist_votes)
            else:
                continue  # nothing came back for this risk at all

            profile["wishlist"] = self._merge_wishlist(
                wishlist_votes, w_street, wishlist_tickers
            )
            profiles[risk] = profile

        if not profiles:
            raise RuntimeError("; ".join(errors) or "Every agent failed.")

        self._latest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "risk_profiles": profiles,
            # Partial failures are not fatal, but the UI should still say so —
            # a score blended from three agents is not the one from five.
            "model_errors": errors or None,
            "error": None,
        }
        self._persist(self._latest)

        note = ", ".join(
            f"{risk}: {len(p['agents'])} agents, "
            f"{p['agreement']['agreed']}/{p['agreement']['total']} agreed"
            for risk, p in profiles.items()
        )
        w_note = "".join(
            f" {risk}/wishlist:{len((p.get('wishlist') or {}).get('suggestions') or [])}"
            for risk, p in profiles.items()
            if (p.get("wishlist") or {}).get("suggestions")
        )
        print(f"AI advisor: refreshed at {self._latest['generated_at']} ({note}{w_note}).")

    def score_context(self, context: dict, kind: str, tickers: list,
                      street: dict = None, scope: str = "scored"):
        """Run every agent over a context someone else prepared, both risks.

        The five agents, the weights and the blending are the most valuable
        thing in this module, and they are not specific to a portfolio — they
        score whatever tickers you put in front of them. This exposes that
        machinery to callers who assemble their own evidence: the discover panel
        scores three stocks nobody owns and which are not on the wishlist, and
        should get exactly the same treatment as everything else on screen
        rather than a second, lesser scoring path.

        It is a separate method rather than a parameter on ``_generate``
        because ``_generate`` fans holdings and wishlist out through *one*
        thread pool on purpose — a refresh then costs roughly one round-trip
        instead of two — and because its cache, its persisted file and its
        wishlist filtering all belong to the portfolio. Nothing here touches
        any of them.

        Returns ``(profiles_by_risk, errors)``. Deliberately unfiltered, unlike
        ``_merge_wishlist``: the caller named these tickers, so "we looked, and
        it's a hold" is an answer they asked for, not a row to hide.

        ``scope`` names the caller for clients that key their storage by call
        (see ``_ask``). Discover scores its picks as a wishlist, so without it
        those ten calls would share slot names with the portfolio's own
        wishlist and overwrite each other's files.
        """
        clients = self.active_clients()
        if not clients:
            raise RuntimeError("No AI model is configured.")

        jobs = [(agent, risk) for risk in _RISK_PROFILES for agent in AGENTS]
        raw = {risk: {} for risk in _RISK_PROFILES}
        errors = []

        lanes = _lanes_for(clients, len(jobs))
        with ThreadPoolExecutor(max_workers=lanes) as pool:
            futures = {
                pool.submit(self._run_agent, agent, risk, kind, context, clients,
                            scope):
                    (agent, risk)
                for agent, risk in jobs
            }
            for future in as_completed(futures):
                agent, risk = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    errors.append(f"{agent.name} ({risk}): {e}")
                    continue
                if result is None:
                    continue  # this agent had no evidence for these tickers
                label, cleaned = result
                raw[risk][agent.key] = (label, cleaned)

        profiles = {}
        for risk in _RISK_PROFILES:
            votes = self._ordered_votes(raw[risk])
            if votes:
                profiles[risk] = self._merge_agents(votes, street or {}, tickers)
        return profiles, errors

    def _run_agent(self, agent, risk: str, kind: str, context: dict, clients: list,
                   scope: str = "advisor"):
        """Ask one agent one question. Returns (model_label, cleaned) or None.

        Model choice is capacity management, not opinion: agents are handed out
        round-robin so the calls spread across whatever providers are
        configured, and an agent whose assigned model is rate-limited or down
        falls through to the next one rather than dropping out of the average
        entirely. Losing an agent costs a whole dimension of the score, which is
        a far worse outcome than one extra call against a second provider.
        """
        payload = agent.payload(context, kind)
        if not payload:
            return None  # this agent has no evidence on this side — stay quiet

        system = agent.system(kind)
        prompt = agent.prompt(payload, kind, risk)

        start = AGENT_KEYS.index(agent.key) % len(clients)
        rotation = [clients[(start + i) % len(clients)] for i in range(len(clients))]
        last_error = None
        for client in rotation[:2]:  # assigned model, then one fallback
            try:
                return client.label, self._clean_profile(
                    self._ask(client, system, prompt, agent, risk, kind, scope)
                )
            except Exception as e:
                last_error = e
                if len(rotation) > 1:
                    print(
                        f"AI advisor: {agent.name} ({risk}/{kind}) failed on "
                        f"{client.label} — {e}"
                    )
        raise last_error

    @staticmethod
    def _ask(client, system: str, prompt: str, agent, risk: str, kind: str,
             scope: str = "advisor") -> dict:
        """One call to one model, retrying transient upstream errors.

        A client that sets ``accepts_slot`` is additionally told *which* call
        this is — scope, side, risk and agent — so it can key a file or a cache
        by it. The name has to be assembled here because this is the only place
        that knows all four, and it is passed by opt-in rather than added to the
        interface so the HTTP clients stay a three-argument ``complete_json``.
        """
        extra = {}
        if getattr(client, "accepts_slot", False):
            extra["slot"] = f"{scope}_{kind}_{risk}_{agent.key}"

        backoff = 2.0
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return client.complete_json(
                    system, prompt, _SUGGESTION_SCHEMA, **extra
                )
            except Exception as e:
                if attempt == _MAX_ATTEMPTS - 1 or not _is_transient(e):
                    raise
                # Take the provider at its word when it names a delay — a rate
                # limit clears on its own schedule, not ours.
                wait = max(backoff, _retry_after(e))
                print(
                    f"AI advisor: {client.label} / {agent.name} ({risk}/{kind}) "
                    f"transient error, retrying in {wait:.0f}s — {e}"
                )
                time.sleep(wait)
                backoff *= 2

    @staticmethod
    def _ordered_votes(by_key: dict) -> list:
        """The agents that answered, in roster order: [(agent, label, profile)].

        Roster order rather than completion order, so the per-agent chips in the
        UI always appear in the same places and a reader learns where to look.
        """
        return [
            (agent, by_key[agent.key][0], by_key[agent.key][1])
            for agent in AGENTS
            if agent.key in by_key
        ]

    def _gather_analysts(self, tickers) -> dict:
        """Wall Street's view per ticker — the expert agent's evidence.

        Returns {ticker: view-or-None}, and {} if the source isn't wired in or
        falls over: a ratings outage must only cost one agent, not the whole
        refresh.
        """
        if not self.analysts_available() or not tickers:
            return {}
        try:
            return self.analysts.get_ratings_many(tickers) or {}
        except Exception as e:
            print(f"AI advisor: analyst ratings unavailable: {e}")
            return {}

    def _gather_macro_news(self) -> list:
        """Market-wide headlines — the macro agent's evidence.

        Returns [] if the source isn't wired in or falls over, in which case the
        macro agent has nothing to read and drops out of the average.
        """
        if not self.macro_available():
            return []
        try:
            return self.macro_news.get_macro_news() or []
        except Exception as e:
            print(f"AI advisor: macro news unavailable: {e}")
            return []

    # --- blending -------------------------------------------------------

    def _source_from(self, agent, label: str, suggestion: dict) -> dict:
        """One agent's vote on one ticker, as the UI will read it.

        Carries the agent's whole answer, not just its number: the detail card
        shows five short arguments side by side instead of one long one, and the
        blended score is only honest if the reader can see what went into it.
        """
        return {
            "kind": "agent",
            "key": agent.key,
            "name": agent.name,
            "short": agent.short,
            "focus": agent.focus,
            "model": label,
            "confidence": suggestion["confidence"],
            "action": suggestion["action"],
            "label": label_for_confidence(suggestion["confidence"]),
            "weight": self._agent_weight(agent.key),
            "horizon_months": suggestion["horizon_months"],
            # "detail" is the field the compact views already read.
            "detail": suggestion["headline"],
            "reasoning": suggestion["reasoning"],
            "price_trigger": suggestion["price_trigger"],
            "risks": suggestion["risks"],
        }

    @staticmethod
    def _lead_source(sources: list):
        """The agent whose view is doing the most work in the average.

        Weight times distance from neutral: a strongly-held view at a low weight
        and a mild view at a high weight both move the blended score, and this
        picks whichever moved it further. That agent's headline becomes the
        summary line, so the one-liner explains the number instead of narrating
        whichever agent happened to be listed first.
        """
        scored = [
            s for s in sources if s.get("confidence") is not None and s.get("weight")
        ]
        if not scored:
            return None
        return max(scored, key=lambda s: s["weight"] * abs(s["confidence"] - 50.0))

    @staticmethod
    def _blended_horizon(sources: list) -> int:
        """The agents' average horizon, rounded into the 1-3 month window."""
        months = [
            s["horizon_months"] for s in sources if s.get("horizon_months") is not None
        ]
        if not months:
            return _MIN_HORIZON_MONTHS
        return max(
            _MIN_HORIZON_MONTHS,
            min(_MAX_HORIZON_MONTHS, round(sum(months) / len(months))),
        )

    def _blend(self, ticker: str, sources: list, street=None) -> dict:
        """One ticker's five (or fewer) agent votes -> one scored suggestion."""
        confidence = blend_confidence(sources)
        if confidence is None:
            confidence = NEUTRAL_CONFIDENCE
        lead = self._lead_source(sources)
        # Agreement is measured over the agents that are actually counted. A
        # silenced agent still has a view and the UI still shows it, but calling
        # a holding "split" because an agent you weighted to zero disagrees
        # would describe a number nobody is being shown.
        counted = [s["confidence"] for s in sources if s.get("weight")]
        return {
            "ticker": ticker,
            "confidence": confidence,
            "action": action_for_confidence(confidence),
            "confidence_label": label_for_confidence(confidence),
            "consensus": consensus_for(counted),
            "horizon_months": self._blended_horizon(sources),
            # The summary line comes from whichever agent drove the score, and
            # is attributed so a reader knows which lens they're looking through.
            "headline": (lead or {}).get("detail", ""),
            "headline_from": (lead or {}).get("key"),
            # No blended prose: five agents wrote five arguments from five
            # different bodies of evidence, and flattening them into one
            # paragraph would invent a synthesis nobody performed. The detail
            # card renders them side by side instead.
            "sources": sources,
            "wall_street": (street or {}).get(ticker),
        }

    def _merge_agents(self, votes: list, street: dict = None,
                      tickers: list = None) -> dict:
        """Blend the agents' profiles into one scored profile.

        ``votes`` is ``[(agent, model_label, profile), ...]`` in roster order.
        Suggestions are matched by ticker; the headline number is the weighted
        average of the agents that scored it, and each agent is recorded in
        ``sources`` so the UI can show the whole breakdown.

        Anchoring to ``tickers`` — the stocks actually owned or watched — rather
        than to the union of what the agents returned drops invented symbols: an
        agent that answers for "HIMSS" when you hold "HIMS" would otherwise add
        a phantom position to the panel.
        """
        street = street or {}
        by_agent = {
            agent.key: {s["ticker"]: s for s in profile["suggestions"] if s["ticker"]}
            for agent, _, profile in votes
        }

        if tickers:
            ordered_tickers = [
                t for t in tickers if any(t in seen for seen in by_agent.values())
            ]
        else:
            ordered_tickers = []
            for agent, _, _ in votes:
                for ticker in by_agent[agent.key]:
                    if ticker not in ordered_tickers:
                        ordered_tickers.append(ticker)

        merged, scores = [], []
        tally = {"agree": 0, "mixed": 0, "split": 0, "single": 0}
        for ticker in ordered_tickers:
            sources = [
                self._source_from(agent, label, by_agent[agent.key][ticker])
                for agent, label, _ in votes
                if ticker in by_agent[agent.key]
            ]
            suggestion = self._blend(ticker, sources, street)
            tally[suggestion["consensus"]] += 1
            scores.append(suggestion["confidence"])
            merged.append(suggestion)

        return {
            # Each agent wrote a note on the whole list from its own dimension.
            # Keeping all of them beats picking one and calling it the summary.
            "portfolio_notes": [
                {
                    "key": agent.key,
                    "name": agent.name,
                    "short": agent.short,
                    "note": profile.get("portfolio_note") or "",
                }
                for agent, _, profile in votes
                if (profile.get("portfolio_note") or "").strip()
            ],
            "suggestions": merged,
            "agents": [
                {"key": agent.key, "name": agent.name, "short": agent.short,
                 "model": label}
                for agent, label, _ in votes
            ],
            # Kept for the status line and older saved data.
            "models": sorted({label for _, label, _ in votes}),
            "avg_confidence": round(sum(scores) / len(scores), 1) if scores else None,
            "agreement": {
                "agreed": tally["agree"],
                "mixed": tally["mixed"],
                "split": tally["split"],
                "total": len(merged),
            },
        }

    @staticmethod
    def _empty_profile(votes: list) -> dict:
        """A holdings profile with nothing in it, for when only the wishlist
        answered (or there are no holdings to score)."""
        return {
            "portfolio_notes": [],
            "suggestions": [],
            "agents": [
                {"key": agent.key, "name": agent.name, "short": agent.short,
                 "model": label}
                for agent, label, _ in votes
            ],
            "models": sorted({label for _, label, _ in votes}),
            "avg_confidence": None,
            "agreement": {"agreed": 0, "mixed": 0, "split": 0, "total": 0},
        }

    def _merge_wishlist(self, votes: list, street: dict,
                        wishlist_tickers: list) -> dict:
        """Blend the wishlist calls, then keep only the names worth buying now.

        Same blend as the holdings profile, but the output is deliberately
        lossy: a watchlist name only survives if the agents' average landed on
        ``buy`` with real conviction behind it. Everything in the wait band is
        dropped, so an empty ``suggestions`` list is the normal case and the UI
        hides the section rather than showing a wall of "not yet".

        The full blended list is kept as ``candidates`` even so, because the
        weights are adjustable: raise the statistics agent and a name that was
        just under the line has to be able to come back, which it can't if the
        losers were thrown away at generation time.
        """
        if not votes:
            return {
                "portfolio_notes": [],
                "suggestions": [],
                "candidates": [],
                "agents": [],
                "models": [],
                "avg_confidence": None,
                "agreement": {"agreed": 0, "mixed": 0, "split": 0, "total": 0},
            }

        blended = self._merge_agents(votes, street, wishlist_tickers)
        return self._filter_wishlist(
            {**blended, "candidates": blended["suggestions"]}
        )

    @staticmethod
    def _filter_wishlist(profile: dict) -> dict:
        """Keep only the genuine buys, and recount the panel around them.

        The counters describe the kept buys, not the whole watchlist — an
        average taken over names nobody suggested buying would be noise. Reads
        from ``candidates`` so it can be re-run after a weight change.
        """
        candidates = profile.get("candidates") or profile.get("suggestions") or []
        buys = sorted(
            (
                s
                for s in candidates
                if s.get("action") == "buy"
                and (s.get("confidence") or 0) >= _WISHLIST_MIN_CONFIDENCE
            ),
            key=lambda s: s.get("confidence") or 0,
            reverse=True,
        )
        tally = {"agree": 0, "mixed": 0, "split": 0, "single": 0}
        for s in buys:
            key = s.get("consensus") or "single"
            tally[key] = tally.get(key, 0) + 1
        scores = [s["confidence"] for s in buys]
        return {
            **profile,
            "candidates": candidates,
            "suggestions": buys,
            "avg_confidence": round(sum(scores) / len(scores), 1) if scores else None,
            "agreement": {
                "agreed": tally["agree"],
                "mixed": tally["mixed"],
                "split": tally["split"],
                "total": len(buys),
            },
        }

    # --- re-blending (no model calls) -----------------------------------

    def _reblend_profile(self, profile: dict) -> dict:
        """Recompute a cached profile's scores under the current weights."""
        if not isinstance(profile, dict):
            return profile
        out = {**profile, "suggestions": self._reblend_all(profile.get("suggestions"))}
        scores = [
            s["confidence"] for s in out["suggestions"] if s.get("confidence") is not None
        ]
        tally = {"agree": 0, "mixed": 0, "split": 0, "single": 0}
        for s in out["suggestions"]:
            key = s.get("consensus") or "single"
            tally[key] = tally.get(key, 0) + 1
        out["avg_confidence"] = round(sum(scores) / len(scores), 1) if scores else None
        out["agreement"] = {
            "agreed": tally["agree"],
            "mixed": tally["mixed"],
            "split": tally["split"],
            "total": len(out["suggestions"]),
        }

        wishlist = profile.get("wishlist")
        if isinstance(wishlist, dict):
            candidates = self._reblend_all(
                wishlist.get("candidates") or wishlist.get("suggestions")
            )
            out["wishlist"] = self._filter_wishlist(
                {**wishlist, "candidates": candidates}
            )
        return out

    def _reblend_all(self, suggestions) -> list:
        return [self._reblend_one(s) for s in (suggestions or [])]

    def _reblend_one(self, suggestion: dict) -> dict:
        """Re-average one suggestion's agent scores at the current weights.

        Suggestions saved before the five-agent split carry ``kind: "model"``
        sources with no agent key; there is nothing to reweight there, so they
        are returned untouched rather than silently rescored against weights
        that never applied to them.
        """
        sources = suggestion.get("sources") or []
        if not any(s.get("kind") == "agent" for s in sources):
            return suggestion
        sources = [
            {**s, "weight": self._agent_weight(s["key"])}
            if s.get("kind") == "agent"
            else s
            for s in sources
        ]
        return {
            **suggestion,
            **self._blend(suggestion.get("ticker", ""), sources),
            # _blend rebuilds the identity fields from scratch; keep the
            # evidence the card renders alongside them.
            "wall_street": suggestion.get("wall_street"),
        }

    def _gather_context(self) -> dict:
        """Assemble the superset of evidence, once, for all five agents.

        Note what this method is *not*: it is not a prompt. Every field is
        gathered here and each agent then takes only its own slice in
        ``ai_agents.py``. Fetching once and slicing after keeps a ticker's news,
        fundamentals and ratings to one network round-trip apiece however many
        agents end up reading them — which is what makes five agents cost five
        LLM calls rather than five times the data fetching.
        """
        holdings = self.market.holdings_view()
        summary = self.summary.summary()

        # Compact the unrealized history to headline value + pct per window (the
        # graph series themselves are too large and not needed for reasoning).
        unrealized = {
            key: {"value": v.get("value"), "pct": v.get("pct")}
            for key, v in (summary.get("unrealized") or {}).items()
        }

        tickers = [h["ticker"] for h in holdings]
        news = self.news.get_news_many(tickers)
        fundamentals = self._gather_fundamentals(tickers)
        street = self._gather_analysts(tickers)
        history = self._gather_history(tickers)
        sales = self._gather_sales()

        # Wishlist: stocks you don't own yet but watch. Same data sources,
        # separate list so the agents can score entry timing.
        wishlist_entries = []
        wishlist_tickers = []
        if self.wishlist is not None:
            try:
                wishlist_tickers = [e["ticker"] for e in self.wishlist.list_wishlist()]
            except Exception:
                wishlist_tickers = []
        if wishlist_tickers:
            w_news = self.news.get_news_many(wishlist_tickers)
            w_fundamentals = self._gather_fundamentals(wishlist_tickers)
            w_street = self._gather_analysts(wishlist_tickers)
            w_history = self._gather_history(wishlist_tickers)
            # Live prices for the wishlist — prefer the market provider directly.
            w_quotes = {}
            w_earnings = {}
            try:
                if hasattr(self.market, "provider") and self.market.provider is not None:
                    w_quotes = self.market.provider.get_quotes(wishlist_tickers) or {}
                    w_earnings = self.market.provider.get_earnings_dates(wishlist_tickers) or {}
                else:
                    # Fallback: use the enriched wishlist view for price/open.
                    w_view = {r["ticker"]: r for r in self.market.wishlist_view(wishlist_tickers)}
                    for t in wishlist_tickers:
                        row = w_view.get(t, {})
                        w_quotes[t] = {"price": row.get("price"), "open": row.get("open"),
                                       "previous_close": None}
                        w_earnings[t] = row.get("earnings_date")
            except Exception:
                w_quotes = {}
                w_earnings = {}
            for ticker in wishlist_tickers:
                q = w_quotes.get(ticker) or {}
                wishlist_entries.append({
                    "ticker": ticker,
                    "price": q.get("price"),
                    "open": q.get("open"),
                    "previous_close": q.get("previous_close"),
                    "earnings_date": w_earnings.get(ticker),
                    "fundamentals": w_fundamentals.get(ticker),
                    "wall_street": w_street.get(ticker),
                    "recent_news": w_news.get(ticker, []),
                    "history": w_history.get(ticker),
                })

        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "total_worth": summary.get("total_worth"),
            "realized_gains": summary.get("realized"),
            "unrealized_gains_history": unrealized,
            # Market-wide, deliberately company-free: the macro agent's slice.
            "macro_news": self._gather_macro_news(),
            "holdings": [
                {
                    "ticker": h["ticker"],
                    "shares": h["shares"],
                    "avg_price": h["avg_price"],
                    "cost_basis": h["cost_basis"],
                    "price": h["price"],
                    "previous_close": h["previous_close"],
                    "market_value": h["market_value"],
                    "today_move": h["today"],
                    "total_unrealized": h["total"],
                    "earnings_date": h["earnings_date"],
                    "fundamentals": fundamentals.get(h["ticker"]),
                    "wall_street": street.get(h["ticker"]),
                    "recent_news": news.get(h["ticker"], []),
                    "history": history.get(h["ticker"]),
                    "past_sales": sales.get(h["ticker"]),
                }
                for h in holdings
            ],
            "wishlist": wishlist_entries,
        }

    def _gather_history(self, tickers) -> dict:
        """A year of daily closes per ticker, measured into a pattern.

        Returns {ticker: stats-or-None}, and {} if history isn't reachable —
        the personal agent then has nothing to read and drops out of the
        average rather than the refresh failing.
        """
        provider = getattr(self.market, "provider", None)
        if provider is None or not tickers:
            return {}
        try:
            daily = provider.get_daily_many(tickers) or {}
        except Exception as e:
            print(f"AI advisor: price history unavailable: {e}")
            return {}
        return {t: _history_stats(daily.get(t)) for t in tickers}

    def _gather_sales(self) -> dict:
        """The investor's own past exits, grouped by ticker.

        Only the personal agent sees these. Whether you have sold this name
        before, and whether it went well, is exactly the kind of pattern that
        belongs in that agent's dimension and nowhere else.
        """
        if self.sales is None:
            return {}
        try:
            records = self.sales.list_sales() or []
        except Exception:
            return {}
        out = {}
        for sale in records:
            out.setdefault(sale["ticker"], []).append(
                {
                    "date": (sale.get("timestamp") or "")[:10],
                    "shares": sale.get("shares"),
                    "sale_price": sale.get("sale_price"),
                    "cost_basis": sale.get("cost_basis"),
                    "realized_gain": sale.get("realized_gain"),
                }
            )
        return out

    def _gather_fundamentals(self, tickers) -> dict:
        """Company fundamentals per ticker, for the prompt.

        Returns {ticker: fundamentals-or-None}, and {} if the source isn't
        wired in or falls over — the company and statistics agents then have
        nothing to read and drop out, rather than the refresh failing. ``fundamentals_data.py`` guarantees nothing
        opinion-shaped is in here; a leak raises rather than reaching a prompt.
        """
        if not self.fundamentals_available() or not tickers:
            return {}
        try:
            return self.fundamentals.get_fundamentals_many(tickers) or {}
        except Exception as e:
            print(f"AI advisor: fundamentals unavailable: {e}")
            return {}

    @staticmethod
    def _clean_profile(result: dict) -> dict:
        """Clamp horizons to the 1-3 month window, normalise the agent's
        confidence onto the 0-100 scale, and tidy shapes.

        An agent that answers with an action but no usable score still counts:
        its action is mapped back onto the scale. A score with no action gets
        the action its score implies. Only when both are missing do we fall
        back to neutral.
        """
        suggestions = []
        for s in result.get("suggestions", []) or []:
            horizon = _clean_horizon(s)

            ticker = (s.get("ticker") or "").upper()
            action = s.get("action")
            action = action if action in _ACTION_CONFIDENCE else None
            confidence = clamp_confidence(s.get("confidence"))
            if confidence is None:
                confidence = _ACTION_CONFIDENCE.get(action, NEUTRAL_CONFIDENCE)
            elif action is not None:
                confidence = _reconcile(ticker, action, confidence)
            if action is None:
                action = action_for_confidence(confidence)

            suggestions.append(
                {
                    "ticker": ticker,
                    "confidence": round(confidence, 1),
                    "action": action,
                    "horizon_months": horizon,
                    "headline": s.get("headline") or "",
                    "reasoning": s.get("reasoning") or "",
                    "price_trigger": s.get("price_trigger") or "",
                    "risks": s.get("risks") or "",
                }
            )
        return {
            "portfolio_note": result.get("portfolio_note") or "",
            "suggestions": suggestions,
        }

    # --- persistence ----------------------------------------------------

    def _load_persisted(self):
        if self.storage is None:
            return None
        try:
            data = self.storage.load()
            return data.get("latest")
        except Exception:
            return None

    def _persist(self, latest: dict):
        if self.storage is None:
            return
        try:
            self.storage.save({"latest": latest})
        except Exception as e:
            print(f"AI advisor: could not persist suggestions: {e}")

    def _persist_weights(self):
        """Save the agent weights for the active portfolio.

        Weights live in their own file rather than inside the suggestions
        blob: they outlive any particular refresh, and a failed generation
        must not be able to reset how much you trust each agent.
        """
        if self.weights_storage is None:
            return
        try:
            self.weights_storage.save({"weights": dict(self.weights)})
        except Exception as e:
            print(f"AI advisor: could not persist agent weights: {e}")

    def _generated_at_epoch(self):
        if not self._latest or not self._latest.get("generated_at"):
            return None
        try:
            return datetime.fromisoformat(self._latest["generated_at"]).timestamp()
        except (TypeError, ValueError):
            return None
