"""AI advisor — a daily agent that turns your portfolio into suggestions.

This is the brain of the assistant. It composes the data the rest of the app
already produces (holdings + cost basis, live prices + momentum, the full
unrealized-gain history, realized gains) with company fundamentals, Wall
Street's published research, and recent news, sends it all to two LLMs, and
blends *their* answers into a single **confidence score** per holding over the
next one to three months — one set tuned for low risk, one for high risk.

The horizon is deliberately one to three months rather than a week. The
sell-side ratings the models read are twelve-month views, so asking the
models for a few days' outlook made them disagree with the street about the
question rather than the answer — a 12-month "Strong Buy" and a 3-day "trim"
are not actually contradictory. Matching the timescales keeps the street's
research comparable with the models' own view of the same holding.

The confidence score
--------------------
Every holding gets one number from 0 to 100, and the number *is* the call:

    100 ── maximum conviction to buy more
     50 ── neutral: hold
      0 ── maximum conviction to sell the whole position

so the mid-40s-to-mid-50s band reads as hold, and the further a score sits from
50 the stronger the buy (above) or sell (below) signal. ``_CONFIDENCE_BANDS``
turns a score into the label and colour the UI shows next to it.

That score is the weighted average of the **two AI models**, and nothing else.

Wall Street is an input, not a vote
-----------------------------------
The analyst research from ``analyst_data.py`` — the consensus rating, the
bull / bear head count, the price-target spread, and which firms upgraded or
downgraded lately — goes *into both models' prompts* as one more piece of
evidence, alongside fundamentals, momentum, the gain history and the news. Each
model weighs it against everything else and produces its own number. The street
never scores anything directly.

This replaced an earlier design that averaged the street in as a third source.
Mechanical averaging was the wrong tool for it. Sell-side ratings sit almost
entirely in the bullish half of their own scale — measured across a real
23-holding portfolio, the consensus mean only ranged 1.3-3.5 out of a nominal
1-5, and 22 of 23 names scored as "buy" — so any fixed mapping onto a 0-100
conviction scale is mis-centred by construction, and the blend inherited a
systematic upward bias that no choice of weights really fixed. A model can do
what an average cannot: notice the skew, discount it, and read *why* the desks
disagree instead of collapsing them into a mean.

The trade-off is deliberate and worth stating: the two remaining sources are no
longer independent of each other, since both read the same street view. When
they agree it is weaker evidence than it used to be. What is gained is that the
street's opinion is now *reasoned about* rather than averaged in.

Weights live in ``DEFAULT_SOURCE_WEIGHTS``; both models count equally by
default. Pass ``source_weights`` to ``AIAdvisorService`` (or set
``AI_SOURCE_WEIGHTS``) to lean on one model more, keyed by its label. A model
that errors on a ticker drops out and the remainder is renormalised, so the
score is always a true average of whoever answered.

Layers, kept separate like the rest of the app:

  - The clients — ``GeminiClient``, ``GroqClient``, ``ClaudeClient``,
    ``LlamaClient``, ``OllamaClient``. Each is an I/O boundary onto one LLM API,
    reached with the Python standard library only (``urllib``), matching
    ``market_data.py`` and ``news_data.py``. They share one method,
    ``complete_json``, so they are interchangeable.
  - ``AIAdvisorService`` — the business logic. Gathers context (including the
    fundamentals and street research the models reason over), builds the
    prompt, asks every configured model for structured JSON, blends their
    scores, caches the result, persists it so it survives restarts, and
    refreshes every two hours during market hours.

Three design notes worth keeping:

  - The models are given headlines; they never search. Free-tier Gemini cannot
    use Google Search grounding (the request is rejected outright), and a model
    asked about "recent news" with none supplied will invent specific, confident,
    wrong headlines. ``news_data.py`` supplies real ones without an API key.
  - Two models are cheaper than they look here — a refresh is a handful of
    ~1.5k-token calls — so both are asked and their agreement is surfaced. When
    they disagree, that is a signal, not a bug.
  - The models get the whole picture: company fundamentals from
    ``fundamentals_data.py`` (valuation, margins, growth, leverage, cash
    generation) *and* the street's research from ``analyst_data.py``. They are
    told plainly that sell-side ratings skew bullish, so they weigh that view
    rather than deferring to it.

Everything degrades gracefully: no key at all -> the advisor reports it's "not
configured" and the UI shows a hint; one model failing -> the other still
answers and the error is noted; every model failing -> the last good suggestions
stay on screen.

Not financial advice — suggestions are generated by a model and can be wrong.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

try:  # stdlib on Python 3.9+, used only for US market-hours math
    from zoneinfo import ZoneInfo

    _EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if tz data is unavailable
    _EASTERN = None


# --- US market hours ----------------------------------------------------

def is_market_open(now_utc: datetime = None) -> bool:
    """True during regular US market hours (Mon-Fri, 9:30-16:00 ET).

    Ignores holidays — good enough to gate a background refresh loop. If tz data
    isn't available, assume open so the advisor still refreshes on schedule.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    if _EASTERN is None:
        return now_utc.weekday() < 5
    et = now_utc.astimezone(_EASTERN)
    if et.weekday() >= 5:  # Saturday / Sunday
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


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
    # the average of the models, so one dropped call halves the consensus for a
    # whole risk profile. A long prompt held open for tens of seconds gets its
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
    ``risk profiles x models x {holdings, wishlist}`` requests — 8 on the
    default two-model setup — and the auto-refresh fires every two hours during
    market hours. A model whose free tier is 20 requests/day (as
    ``gemini-3.6-flash`` was) is therefore exhausted in about two refreshes, and
    for the rest of the day the panel silently drops to a single model with no
    consensus behind its scores. If that happens, pair a small model with a
    high daily cap against the better one, or move the key to a paid tier.

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
# One number per holding, 0-100, blended from the AI models. See the module
# docstring for the scale; this section is the whole of the arithmetic.

# How much each AI model counts toward the blended score — equal by default.
# The street is deliberately absent: it is evidence inside both prompts, not a
# source with a weight (see the module docstring). To lean on one model, pass
# ``source_weights=`` to AIAdvisorService or set AI_SOURCE_WEIGHTS keyed by that
# model's label ("gemini:gemini-3.6-flash=2").
DEFAULT_SOURCE_WEIGHTS = {
    "model": 1.0,  # each configured AI model, unless its label overrides this
}

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
# The action is a four-value enum the models have always got right, so it acts
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


# How far apart the sources' scores may sit before we call it a disagreement.
# Measured as a spread (highest score minus lowest) rather than by comparing
# action labels: on a continuous scale, 50 vs. 55 is agreement that happens to
# straddle a band edge, while 50 vs. 92 is a real split even though both are
# nominally "buy". Comparing labels flagged essentially every holding as split,
# because the sell-side rarely publishes anything below "buy".
_AGREEMENT_SPREAD = 15.0  # within this -> agree
_SPLIT_SPREAD = 35.0      # beyond this -> split; in between -> mixed


def consensus_for(scores) -> str:
    """Classify how well the sources agree: single / agree / mixed / split."""
    usable = [s for s in scores if s is not None]
    if len(usable) < 2:
        return "single"
    spread = max(usable) - min(usable)
    if spread <= _AGREEMENT_SPREAD:
        return "agree"
    if spread <= _SPLIT_SPREAD:
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


def blend_confidence(sources) -> float:
    """Weighted average of the sources that produced a score.

    ``sources`` is the per-suggestion list of ``{confidence, weight, ...}``
    dicts — one per AI model. A model that skipped the ticker is simply absent,
    so the weights of those that did answer are renormalised against each other
    and a lone survivor's score passes through unchanged rather than being
    dragged toward neutral by the gap.
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
# sell-side ratings the models read are written on, so the street's view and
# the models' own are answering the same question — see the module docstring.
_MIN_HORIZON_MONTHS = 1
_MAX_HORIZON_MONTHS = 3

# Attempts per (model, risk profile) call before giving up on that model.
_MAX_ATTEMPTS = 3

# Wishlist uses the same conviction scale but the question is different:
# "should I *enter* this name now?" 0 = avoid, 50 = wait, 100 = buy now. The
# old 45-55 hold band becomes a wait band, and anything >= Lean buy (55) is
# worth surfacing as a filtered buy signal.
_WISHLIST_SYSTEM_PROMPT = (
    "You are a sharp, concise equity analyst helping a retail investor decide "
    "when to ENTER new positions from a watchlist. You are given the watchlist "
    "tickers (stocks the investor does NOT own), with live prices, company "
    "fundamentals per ticker, the Wall Street research on each ticker, and "
    "recent news headlines per ticker.\n\n"
    "WALL STREET IS EVIDENCE, NOT AN ANSWER. Each watchlist entry carries a "
    "'wall_street' block like holdings do. Weigh it with the same standards: "
    "sell-side ratings skew bullish, disagreement matters more than the mean, "
    "recent upgrades/downgrades beat stale consensus, and fundamentals trump "
    "a target that sits far above the price.\n\n"
    "Weigh the FUNDAMENTALS, not just momentum. Same figures as holdings: "
    "trailing multiples, margins, growth, leverage, cash generation, 52-week "
    "range, moving averages, short interest. Percentages are already percent.\n\n"
    "You are scoring conviction to BUY / WAIT over the coming one to three "
    "months — business quality and entry valuation over that horizon, with "
    "near-term noise only for the entry point.\n\n"
    "For EACH watchlist ticker, express your view as a CONFIDENCE SCORE 0-100:\n"
    "  100 = maximum conviction to BUY NOW\n"
    "   75 = a solid buy\n"
    "   50 = neutral, WAIT — not a good entry right now\n"
    "   25 = weak, avoid\n"
    "    0 = avoid entirely\n"
    "Scores 45-55 all mean wait; further from 50 = stronger signal. Use the "
    "whole range. Also give the matching 'action' label: 'buy' means buy now, "
    "'hold' means wait — trim/sell are not used for the watchlist but accepted "
    "as wait signals.\n\n"
    "'horizon_months' is 1, 2 or 3 — never longer than a quarter, never in days. "
    "Combine risk and expected gain and tailor to the requested risk tolerance. "
    "Low risk: only flag clear risk/reward with durable business at defensible "
    "valuation. High risk: willing to enter on weakness where growth is intact.\n\n"
    "Be specific and actionable. When relevant give a concrete entry trigger "
    "(e.g. 'buy below $280; avoid above $320'). Ground reasoning in the data — "
    "cite valuation multiples, margins, growth, upcoming earnings, and specific "
    "headlines. Keep each reasoning to roughly 6-10 short lines. Keep the "
    "headline to one short line. Do not give generic disclaimers; be direct."
)

# Filter for wishlist: only show buys where the blended confidence is a
# genuine buy signal (>= Lean buy). Anything in the hold/wait band is
# hidden entirely — the section renders as if it doesn't exist.
_WISHLIST_MIN_CONFIDENCE = 55

_SYSTEM_PROMPT = (
    "You are a sharp, concise equity analyst helping a retail investor manage a "
    "small personal stock portfolio. You are given the investor's holdings (with "
    "average cost and cost basis), live prices and today's move, the full history "
    "of unrealized gains across 1D/1W/1M/YTD/1Y/Total windows, realized gains, "
    "company fundamentals per ticker, the Wall Street research on each ticker, "
    "and recent news headlines per ticker.\n\n"
    "WALL STREET IS EVIDENCE, NOT AN ANSWER. Each holding carries a "
    "'wall_street' block: the sell-side consensus rating and its 1-5 mean "
    "(1 = Strong Buy, 5 = Strong Sell), how many desks rate it buy, hold and "
    "sell, the mean / high / low price targets and what they imply from "
    "today's price, which firms upgraded, downgraded, initiated or reiterated "
    "lately and at what target, and a 'disagreement_note' summarising how far "
    "apart the desks are. Weigh it as you would any other analyst's argument, "
    "and hold it to these standards:\n"
    "  - Sell-side ratings are structurally bullish. Desks rarely publish "
    "sell ratings, so 'Buy' is closer to their neutral than to real "
    "enthusiasm. Judge a rating against that baseline: a consensus mean near "
    "2.0 is ordinary, and anything at 2.5 or worse is unusually cool. Do not "
    "read a Buy consensus as confirmation on its own.\n"
    "  - Disagreement matters more than the average. Forty desks quietly "
    "agreeing is a real signal; a wide bull/bear split or price targets "
    "spanning 100%+ of the mean means the street has no idea either, and you "
    "should discount the consensus accordingly and say so.\n"
    "  - Recent upgrades and downgrades carry more information than standing "
    "ratings, which often go stale. A fresh downgrade against a Strong Buy "
    "consensus is worth more than the consensus.\n"
    "  - Check the street against the fundamentals and the price. If the "
    "numbers contradict the rating, back the numbers and explain the "
    "conflict. If a mean target sits far above the current price, ask what "
    "has to go right for it, not whether it is achievable.\n"
    "You may agree with the street, disagree with it, or discount it as "
    "uninformative — but say explicitly what you did with it and why.\n\n"
    "Weigh the FUNDAMENTALS, not just the price action. Each holding carries "
    "trailing valuation multiples (P/E, P/S, P/B, EV/EBITDA), margins and "
    "returns on equity and assets, year-over-year revenue and earnings growth, "
    "cash, debt and free cash flow, beta, the 52-week range and 50/200-day "
    "averages, short interest, and the last four quarters of actual EPS with "
    "how far each beat or missed. Percentages are already in percent. Read "
    "them together: a rich multiple is only a problem if growth or margins "
    "don't support it, a cheap one is only an opportunity if the business "
    "isn't deteriorating, and leverage matters more when cash flow is thin. "
    "Say which specific figures drove your score.\n\n"
    "You are scoring conviction to buy, hold, or sell over the coming one to "
    "three months — not forecasting a few days of price action. Business "
    "quality, valuation and the direction of the fundamentals should carry "
    "real weight over that horizon; day-to-day momentum and a single "
    "headline should carry much less. Ask what this position is worth owning "
    "into the next quarter, and let near-term noise inform the entry or exit "
    "point rather than the verdict.\n\n"
    "For EACH holding, express your view as a CONFIDENCE SCORE — an integer "
    "from 0 to 100 that measures how strongly you'd act:\n"
    "  100 = maximum conviction to BUY MORE\n"
    "   75 = a solid buy\n"
    "   50 = neutral, HOLD — you'd neither add nor reduce\n"
    "   25 = you'd trim the position\n"
    "    0 = maximum conviction to SELL the entire position\n"
    "Scores from about 45 to 55 all mean hold; the further a score is from 50, "
    "the stronger the signal. Use the whole range — reserve the extremes for "
    "genuine conviction, and don't park everything near 50 to hedge. Also give "
    "the matching 'action' label, and make sure the two agree.\n\n"
    "'horizon_months' is when you expect this call to play out: 1, 2 or 3 "
    "months — never longer than a quarter, never expressed in days. Combine "
    "both risk and expected gain, and tailor everything to the requested risk "
    "tolerance.\n\n"
    "Be specific and actionable. When relevant, give a concrete price trigger "
    "for the period (e.g. 'add below $280; if it closes under $240 the thesis "
    "is broken, sell'). Ground your reasoning in the data provided — cite the "
    "valuation multiples, margins and growth, the cost basis, upcoming "
    "earnings, and specific headlines. Keep each detailed "
    "reasoning to roughly 8-12 short lines. Keep the summary headline to one "
    "short line. Do not give generic disclaimers; be direct."
)


class AIAdvisorService:
    """Generates, caches, and refreshes the AI suggestions.

    Composes one *or more* LLM clients with the market/portfolio/summary
    services, the news provider, and the analyst-ratings provider. Produces both
    a low-risk and a high-risk set every refresh and caches them together, so
    the UI's risk toggle never triggers a new API call. Persists the latest
    result so a restart shows the last suggestions.

    Blending: every model is asked the same question, and their scores are
    averaged with the analyst consensus into one confidence per ticker (see the
    module docstring). Each suggestion carries the full ``sources`` breakdown —
    who scored it what, at what weight — so the UI can show how the number was
    reached, plus a ``consensus`` of ``agree`` / ``split`` / ``single``:
    disagreement between the street and the models is a signal worth seeing.
    The lead model (first in the list) supplies the prose.

    This is affordable because the workload is tiny — two risk profiles times a
    couple of models, a few times a day, at roughly 1.5k input tokens a call.
    All calls in a refresh run concurrently (analyst ratings included), so
    latency is the slowest single call rather than their sum.
    """

    def __init__(self, clients, news, market, portfolio, summary,
                 storage=None, refresh_hours: int = 2, analysts=None,
                 source_weights=None, fundamentals=None, wishlist=None):
        # Accept a single client or a list, so existing callers keep working.
        if not isinstance(clients, (list, tuple)):
            clients = [clients]
        self.clients = [c for c in clients if c is not None]
        self.news = news
        self.market = market
        self.portfolio = portfolio
        self.summary = summary
        self.storage = storage
        self.refresh_seconds = refresh_hours * 3600
        # The third opinion. Optional: without it the blend is just the models.
        self.analysts = analysts
        # Company fundamentals for the prompt — the evidence the street works
        # from, never its conclusions. Optional; the models simply reason on
        # price, momentum and news without it, as they originally did.
        self.fundamentals = fundamentals
        # Wishlist: stocks you don't own yet but may want to buy. Optional;
        # when wired in the advisor also scores each wishlist name for a buy
        # entry and filters to the ones worth showing.
        self.wishlist = wishlist
        self.weights = {**DEFAULT_SOURCE_WEIGHTS, **(source_weights or {})}

        self._lock = threading.Lock()
        self._refreshing = False
        self._latest = self._load_persisted()

    # --- source weighting -----------------------------------------------

    def _model_weight(self, label: str) -> float:
        """Weight for one model: its own label if given one, else the shared
        "model" weight."""
        return float(self.weights.get(label, self.weights.get("model", 1.0)))

    # --- public API -----------------------------------------------------

    def available(self) -> bool:
        """True when at least one model is configured and usable."""
        return any(c.available() for c in self.clients)

    def active_clients(self) -> list:
        """The configured clients, in priority order (first one leads)."""
        return [c for c in self.clients if c.available()]

    def analysts_available(self) -> bool:
        """True when the Wall Street consensus source is wired in and usable."""
        return self.analysts is not None and self.analysts.available()

    def fundamentals_available(self) -> bool:
        """True when company fundamentals can be fed to the models."""
        return self.fundamentals is not None and self.fundamentals.available()

    def get(self) -> dict:
        """Return the latest cached suggestions plus status for the UI."""
        latest = self._latest
        return {
            "configured": self.available(),
            "models": [c.label for c in self.active_clients()],
            "news_configured": self.news.available(),
            "news_sources": getattr(self.news, "describe", lambda: "")(),
            # Evidence inside both models' prompts — not a scoring source.
            "analysts_configured": self.analysts_available(),
            "analyst_source": (
                self.analysts.describe() if self.analysts_available() else None
            ),
            # Fundamentals feed the models' reasoning, not the blend.
            "fundamentals_configured": self.fundamentals_available(),
            "fundamentals_source": (
                self.fundamentals.describe()
                if self.fundamentals_available()
                else None
            ),
            "source_weights": self.weights,
            "refreshing": self._refreshing,
            "market_open": is_market_open(),
            "refresh_hours": self.refresh_seconds // 3600,
            "generated_at": latest.get("generated_at") if latest else None,
            "risk_profiles": latest.get("risk_profiles") if latest else None,
            "model_errors": latest.get("model_errors") if latest else None,
            "error": latest.get("error") if latest else None,
        }

    def reload(self) -> None:
        """Re-read persisted suggestions for the now-active portfolio.

        Called after the active portfolio is switched or deleted: the cached
        ``_latest`` belongs to the old portfolio, so drop it and load whatever
        the new one has (which may be nothing yet)."""
        with self._lock:
            self._latest = self._load_persisted()

    def request_refresh(self) -> bool:
        """Kick off a background regeneration. Returns False if one is running
        or the advisor isn't configured (nothing to do)."""
        if not self.available() or self._refreshing:
            return False
        threading.Thread(target=self._safe_generate, daemon=True).start()
        return True

    def start_scheduler(self):
        """Start the background loop: generate once on boot (if nothing is
        cached), then refresh every ``refresh_hours`` during market hours."""
        if not self.available():
            print("AI advisor: no model configured — advisor disabled.")
            return
        labels = ", ".join(c.label for c in self.active_clients())
        print(f"AI advisor: {labels}")
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    # --- scheduler ------------------------------------------------------

    def _scheduler_loop(self):
        # Generate immediately on boot if we have nothing to show yet.
        if self._latest is None:
            self._safe_generate()
        while True:
            time.sleep(300)  # re-check every 5 minutes
            if not is_market_open():
                continue
            last = self._generated_at_epoch()
            if last is None or (time.time() - last) >= self.refresh_seconds:
                self._safe_generate()

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

        # Every (risk profile x model x kind) triple is an independent call —
        # fan them all out at once so a refresh costs one round-trip of
        # wall-clock. Holdings and wishlist are asked separately: they're
        # different questions ("stay in?" vs "get in?") and one prompt per job
        # keeps each focused and token-light. The wishlist set is skipped
        # entirely when there's nothing on the list.
        def jobs(kind):
            return [(risk, client, kind) for risk in _RISK_PROFILES for client in clients]

        # An empty portfolio still gets a holdings call — it's what produces
        # the portfolio note the panel leads with.
        all_jobs = jobs("holdings")
        if wishlist_tickers:
            all_jobs += jobs("wishlist")

        raw = {risk: {} for risk in _RISK_PROFILES}
        raw_wishlist = {risk: {} for risk in _RISK_PROFILES}
        errors = []

        def ask(risk, client, kind):
            """One model, one risk profile, retrying transient upstream errors."""
            if kind == "wishlist":
                system = _WISHLIST_SYSTEM_PROMPT
                prompt = self._build_wishlist_prompt(context, risk)
            else:
                system = _SYSTEM_PROMPT
                prompt = self._build_prompt(context, risk)
            backoff = 2.0
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    return client.complete_json(system, prompt, _SUGGESTION_SCHEMA)
                except Exception as e:
                    if attempt == _MAX_ATTEMPTS - 1 or not _is_transient(e):
                        raise
                    # Take the provider at its word when it names a delay —
                    # a rate limit clears on its own schedule, not ours.
                    wait = max(backoff, _retry_after(e))
                    print(
                        f"AI advisor: {client.label} ({risk}/{kind}) transient error, "
                        f"retrying in {wait:.0f}s — {e}"
                    )
                    time.sleep(wait)
                    backoff *= 2

        tickers = [h["ticker"] for h in context["holdings"]]
        # Cap the fan-out at what it was before the wishlist doubled the job
        # count. Firing all eight at once burst straight through the free
        # tier's per-minute quota, and the holdings calls — the ones the panel
        # is actually built around — were the ones that lost the race.
        # Holdings jobs are queued first, so they claim the first wave.
        lanes = max(1, min(len(all_jobs), len(clients) * len(_RISK_PROFILES)))
        with ThreadPoolExecutor(max_workers=lanes) as pool:
            futures = {pool.submit(ask, r, c, k): (r, c, k) for r, c, k in all_jobs}
            for future in as_completed(futures):
                risk, client, kind = futures[future]
                try:
                    cleaned = self._clean_profile(future.result())
                except Exception as e:
                    tag = f"{client.label} ({risk}/{kind})"
                    errors.append(f"{tag}: {e}")
                    continue
                if kind == "wishlist":
                    raw_wishlist[risk][client.label] = cleaned
                else:
                    raw[risk][client.label] = cleaned

        # Street views for detail cards (evidence, not scored).
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
            ordered = [
                (c.label, raw[risk][c.label]) for c in clients if c.label in raw[risk]
            ]
            w_ordered = [
                (c.label, raw_wishlist[risk][c.label]) for c in clients if c.label in raw_wishlist[risk]
            ]
            if ordered:
                profile = self._merge_profiles(ordered, street, tickers)
            elif w_ordered:
                # The holdings call failed, or there are no holdings at all,
                # but the wishlist answered — give the buys somewhere to live
                # rather than dropping this risk profile entirely.
                profile = {
                    "portfolio_note": w_ordered[0][1].get("portfolio_note", ""),
                    "suggestions": [],
                    "models": [label for label, _ in w_ordered],
                    "avg_confidence": None,
                    "agreement": {"agreed": 0, "mixed": 0, "split": 0, "total": 0},
                }
            else:
                continue  # nothing came back for this risk at all

            profile["wishlist"] = self._merge_wishlist(
                w_ordered, w_street, wishlist_tickers
            )
            profiles[risk] = profile

        if not profiles:
            raise RuntimeError("; ".join(errors) or "All models failed.")

        self._latest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "risk_profiles": profiles,
            # Partial failures are not fatal, but the UI should still say so.
            "model_errors": errors or None,
            "error": None,
        }
        self._persist(self._latest)

        note = ", ".join(
            f"{risk}: {p['agreement']['agreed']}/{p['agreement']['total']} agreed"
            for risk, p in profiles.items()
        )
        # Append wishlist counts when present for log visibility.
        w_note = ""
        for risk, p in profiles.items():
            wl = (p.get("wishlist") or {}).get("suggestions") or []
            if wl:
                w_note += f" {risk}/wishlist:{len(wl)}"
        print(f"AI advisor: refreshed at {self._latest['generated_at']} ({note}{w_note}).")

    def _gather_analysts(self, tickers) -> dict:
        """Wall Street's view per ticker — evidence for the models' prompts.

        Returns {ticker: view-or-None}, and {} if the source isn't wired in or
        falls over: a ratings outage must only cost the models one input, not
        the whole refresh.
        """
        if not self.analysts_available() or not tickers:
            return {}
        try:
            return self.analysts.get_ratings_many(tickers) or {}
        except Exception as e:
            print(f"AI advisor: analyst ratings unavailable: {e}")
            return {}

    def _merge_profiles(self, ordered: list, street: dict = None,
                        holdings: list = None) -> dict:
        """Blend one cleaned profile per model into a single scored profile.

        ``ordered`` is ``[(label, profile), ...]`` in priority order, ``street``
        is ``{ticker: analyst-view-or-None}`` and ``holdings`` is the tickers
        actually owned. Suggestions are matched by ticker; the lead model
        supplies the prose, while the headline number — ``confidence`` — is the
        weighted average of the models that scored that ticker. Each model is
        recorded in ``sources`` so the UI can show the breakdown behind the
        score.

        The street's view is attached to each suggestion as ``wall_street``. It
        carries no score and takes no part in the average: it is the evidence
        both models were shown, kept alongside the result so the UI can display
        what they were reasoning from.
        """
        street = street or {}
        lead_profile = ordered[0][1]
        by_model = {
            label: {s["ticker"]: s for s in profile["suggestions"] if s["ticker"]}
            for label, profile in ordered
        }

        # One row per holding, in the portfolio's own order. Anchoring to the
        # holdings rather than to the union of what the models returned drops
        # invented symbols — a model that answers for "HIMSS" when you hold
        # "HIMS" would otherwise add a phantom position to the panel.
        if holdings:
            tickers = [t for t in holdings if any(t in m for m in by_model.values())]
        else:
            tickers = []
            for label, _ in ordered:
                for ticker in by_model[label]:
                    if ticker not in tickers:
                        tickers.append(ticker)

        merged, scores = [], []
        tally = {"agree": 0, "mixed": 0, "split": 0, "single": 0}
        for ticker in tickers:
            model_votes = [
                (label, by_model[label][ticker])
                for label, _ in ordered
                if ticker in by_model[label]
            ]
            lead = model_votes[0][1]

            sources = [
                {
                    "kind": "model",
                    "name": label,
                    "confidence": s["confidence"],
                    "action": s["action"],
                    "label": label_for_confidence(s["confidence"]),
                    "weight": self._model_weight(label),
                    "detail": s["headline"],
                }
                for label, s in model_votes
            ]

            # The blended score is the call; the action label follows from it.
            confidence = blend_confidence(sources)
            if confidence is None:
                confidence = NEUTRAL_CONFIDENCE

            # How far apart the two models landed, having read the same evidence.
            consensus = consensus_for([s["confidence"] for s in sources])
            tally[consensus] += 1

            scores.append(confidence)
            merged.append(
                {
                    **lead,
                    "confidence": confidence,
                    "action": action_for_confidence(confidence),
                    "confidence_label": label_for_confidence(confidence),
                    "consensus": consensus,
                    "sources": sources,
                    # Evidence, not a vote — see the docstring.
                    "wall_street": street.get(ticker),
                }
            )

        return {
            "portfolio_note": lead_profile.get("portfolio_note") or "",
            "suggestions": merged,
            "models": [label for label, _ in ordered],
            "avg_confidence": round(sum(scores) / len(scores), 1) if scores else None,
            "agreement": {
                "agreed": tally["agree"],
                "mixed": tally["mixed"],
                "split": tally["split"],
                "total": len(merged),
            },
        }

    def _merge_wishlist(self, w_ordered: list, street: dict,
                        wishlist_tickers: list) -> dict:
        """Blend the wishlist calls, then keep only the names worth buying now.

        Same blend as the holdings profile, but the output is deliberately
        lossy: a watchlist name only survives if the models landed on ``buy``
        with real conviction behind it. Everything in the wait band is dropped,
        so an empty ``suggestions`` list is the normal case and the UI hides
        the section rather than showing a wall of "not yet".

        The panel's counters describe the kept buys, not the whole watchlist —
        an average taken over names nobody suggested buying would be noise.
        """
        empty = {
            "portfolio_note": "",
            "suggestions": [],
            "models": [label for label, _ in w_ordered],
            "avg_confidence": None,
            "agreement": {"agreed": 0, "mixed": 0, "split": 0, "total": 0},
        }
        if not w_ordered:
            return empty

        blended = self._merge_profiles(w_ordered, street, wishlist_tickers)
        buys = sorted(
            (
                s
                for s in blended["suggestions"]
                if s.get("action") == "buy"
                and (s.get("confidence") or 0) >= _WISHLIST_MIN_CONFIDENCE
            ),
            key=lambda s: s.get("confidence") or 0,
            reverse=True,
        )
        empty["models"] = blended.get("models", [])
        if not buys:
            return empty

        tally = {"agree": 0, "mixed": 0, "split": 0, "single": 0}
        for s in buys:
            tally[s.get("consensus") or "single"] = (
                tally.get(s.get("consensus") or "single", 0) + 1
            )
        scores = [s["confidence"] for s in buys]
        return {
            "portfolio_note": blended.get("portfolio_note") or "",
            "suggestions": buys,
            "models": blended.get("models", []),
            "avg_confidence": round(sum(scores) / len(scores), 1),
            "agreement": {
                "agreed": tally["agree"],
                "mixed": tally["mixed"],
                "split": tally["split"],
                "total": len(buys),
            },
        }

    def _gather_context(self) -> dict:
        """Assemble everything the model needs from the existing services."""
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
        # The street's research goes into the prompt as evidence. Both models
        # read it and decide for themselves what it's worth.
        street = self._gather_analysts(tickers)

        # Wishlist: stocks you don't own yet but watch. Same data sources,
        # separate list so the models can score entry timing.
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
                })

        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "total_worth": summary.get("total_worth"),
            "realized_gains": summary.get("realized"),
            "unrealized_gains_history": unrealized,
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
                }
                for h in holdings
            ],
            "wishlist": wishlist_entries,
        }

    def _gather_fundamentals(self, tickers) -> dict:
        """Company fundamentals per ticker, for the prompt.

        Returns {ticker: fundamentals-or-None}, and {} if the source isn't
        wired in or falls over — the models then reason without them rather
        than the refresh failing. ``fundamentals_data.py`` guarantees nothing
        opinion-shaped is in here; a leak raises rather than reaching a prompt.
        """
        if not self.fundamentals_available() or not tickers:
            return {}
        try:
            return self.fundamentals.get_fundamentals_many(tickers) or {}
        except Exception as e:
            print(f"AI advisor: fundamentals unavailable: {e}")
            return {}

    def _build_prompt(self, context: dict, risk: str) -> str:
        if risk == "low":
            stance = (
                "The investor wants a LOW-RISK stance: prioritise capital "
                "preservation and steady gains over aggressive upside. Over the "
                "next quarter, favour durable businesses at defensible "
                "valuations, trim positions whose fundamentals no longer "
                "support the multiple, and only suggest buying more when the "
                "risk/reward is clearly favourable. Volatility alone is not a "
                "reason to sell a sound position — deteriorating fundamentals "
                "or a stretched valuation is."
            )
        else:
            stance = (
                "The investor wants a HIGH-RISK stance: prioritise maximising "
                "gains and is comfortable with volatility. Over the next "
                "quarter, be willing to buy weakness and add to conviction "
                "positions where growth is intact, and only trim or sell when "
                "the fundamental case is breaking down or the valuation has run "
                "far ahead of the business."
            )
        # Holdings prompt should not carry wishlist payload — keeps tokens low
        # and prevents the model from mixing the two jobs.
        holdings_context = {k: v for k, v in context.items() if k != "wishlist"}
        return (
            f"{stance}\n\n"
            "Here is the current portfolio and market context as JSON. All "
            "monetary values are in the position's currency; gains are shown as "
            "{value, pct}. 'unrealized_gains_history' shows the accrued gain over "
            "each window so you can read momentum. Each holding's 'fundamentals' "
            "block carries trailing valuation, margins, growth, balance-sheet and "
            "price-range figures — every field ending in '_pct' is already a "
            "percentage. The 'wall_street' block is the sell-side research: "
            "weigh it as evidence against everything else, per the standards in "
            "your instructions. A missing field or block simply wasn't reported; "
            "don't guess at it.\n\n"
            f"{json.dumps(holdings_context, indent=2, default=str)}\n\n"
            "Return one suggestion per holding. 'confidence' is your 0-100 "
            "score (100 = buy hard, 50 = hold, 0 = sell out). 'action' is the "
            "matching label — one of buy (buy more), hold, trim (sell part), "
            "sell (sell the whole position). 'horizon_months' must be 1, 2 or 3 "
            "— the number of months you expect the call to play out over. "
            "'headline' is the one-line summary; "
            "'reasoning' is the ~10-line explanation — and it must state what "
            "you made of the Wall Street view, whether you sided with it or "
            "against it; 'price_trigger' is a concrete price-based action for "
            "the period (empty string if none); 'risks' names the main risk to "
            "this call."
        )

    def _build_wishlist_prompt(self, context: dict, risk: str) -> str:
        if risk == "low":
            stance = (
                "The investor wants a LOW-RISK stance for NEW ENTRIES: only flag "
                "clear risk/reward with durable business at defensible valuation. "
                "Be selective — a wishlist name must earn a buy with business quality "
                "and entry price that compensate for being a new, concentrated bet."
            )
        else:
            stance = (
                "The investor wants a HIGH-RISK stance for NEW ENTRIES: willing to "
                "enter on weakness where growth is intact and the setup is timely. "
                "You may flag momentum breakouts or oversold quality if the "
                "risk/reward is there, but still ground timing in valuation and "
                "fundamentals — not just a hot headline."
            )
        # Minimal wishlist payload — keep tokens low since we only need entry timing.
        wishlist_payload = {
            "as_of": context.get("as_of"),
            "wishlist": context.get("wishlist", []),
        }
        return (
            f"{stance}\n\n"
            "Here is the current watchlist context as JSON. Each entry's "
            "'fundamentals' block carries trailing valuation, margins, growth, "
            "balance-sheet and price-range figures (fields ending in '_pct' are "
            "percent). The 'wall_street' block is sell-side research: weigh it as "
            "evidence per the standards in your instructions. A missing field simply "
            "wasn't reported; don't guess at it.\n\n"
            f"{json.dumps(wishlist_payload, indent=2, default=str)}\n\n"
            "Return one suggestion per watchlist ticker. 'confidence' is your 0-100 "
            "score (100 = buy now, 50 = wait, 0 = avoid). 'action' is the matching "
            "label — 'buy' means buy now, 'hold' means wait; trim/sell both mean wait. "
            "'horizon_months' must be 1, 2 or 3 — months you expect the entry to "
            "pay off over. 'headline' is the one-line summary; 'reasoning' is the "
            "~6-10 line explanation grounded in valuation, margins/growth, earnings "
            "date, and headlines; 'price_trigger' is a concrete entry trigger "
            "(empty if none); 'risks' names the main risk to this entry."
        )

    @staticmethod
    def _clean_profile(result: dict) -> dict:
        """Clamp horizons to the 1-7 day window, normalise each model's
        confidence onto the 0-100 scale, and tidy shapes.

        A model that answers with an action but no usable score still counts:
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

    def _generated_at_epoch(self):
        if not self._latest or not self._latest.get("generated_at"):
            return None
        try:
            return datetime.fromisoformat(self._latest["generated_at"]).timestamp()
        except (TypeError, ValueError):
            return None
