"""Shared LLM client for the Analyst, Curriculum and Judge, instrumented with Neatlogs tracing.

Every agent calls get_client() to reach the same client, wrapped once so traces
land in the same Neatlogs workflow. It points at neatHack's own LiteLLM gateway,
the one the Student Agent already uses through opencode.json (ADR-0005), not
Groq: Groq's free tier stalled a single Analyst-plus-Curriculum session on
2026-10-11, sitting in a rate-limit wait for 26 minutes with nothing in flight.
"""

import json
import os
import time

from dotenv import load_dotenv

load_dotenv()

import neatlogs
from openai import OpenAI, RateLimitError

GATEWAY_URL = "https://litellm.vision.aivar.app/v1"
# Structured JSON and judgement (which Occupational Task a skill serves, which
# Module a Gap belongs to) matter more here than the cheapest tokens, and the
# gateway's Nova models are the Student Agent's. No reasoning_effort is sent:
# the gateway rejects it for every model it serves (Claude maps it to a
# thinking budget that must fit inside max_tokens; Bedrock Nova has no such
# parameter), verified live 2026-10-11.
MODEL = "claude-haiku"

# The gateway can still answer 429. Waiting a rolling window out clears it.
_RATE_LIMIT_WAITS_SECONDS = (5, 15, 30)

_client: OpenAI | None = None
_tracing_initialized = False


def init_tracing() -> None:
    global _tracing_initialized
    if _tracing_initialized:
        return
    neatlogs.init(api_key=os.environ["NEATLOGS_API_KEY"], workflow_name="pareto-ai")
    _tracing_initialized = True


def get_client() -> OpenAI:
    global _client
    if _client is None:
        init_tracing()
        _client = neatlogs.wrap(
            OpenAI(
                api_key=os.environ["LITELLM_API_KEY"],
                base_url=GATEWAY_URL,
            )
        )
    return _client


def json_object(content: str) -> dict | None:
    """The JSON object in a model response, or None if there isn't a usable one.

    Models wrap JSON in prose, so the braces are scanned for rather than the
    whole string parsed. None means unusable, which is distinct from `{}` — an
    answer of "nothing" the model actually gave. Callers decide which of those
    they can live with (see ADR-0002).
    """
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _retry_after_seconds(error: RateLimitError, fallback: int) -> float:
    header = getattr(error, "response", None) and error.response.headers.get("retry-after")
    try:
        return max(float(header), 1.0) if header else fallback
    except (TypeError, ValueError):
        return fallback


def complete(prompt: str, *, max_tokens: int) -> tuple[str, str | None]:
    """One chat completion, waiting out the gateway's rate limit.

    Returns the content and the finish reason; the caller decides whether an
    empty or truncated answer is tolerable (see ADR-0002).
    """
    for wait in (*_RATE_LIMIT_WAITS_SECONDS, None):
        try:
            response = get_client().chat.completions.create(
                model=MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except RateLimitError as error:
            if wait is None:
                raise
            time.sleep(_retry_after_seconds(error, wait))
            continue
        choice = response.choices[0]
        return choice.message.content or "", choice.finish_reason
    raise AssertionError("unreachable: the final attempt either returns or raises")
