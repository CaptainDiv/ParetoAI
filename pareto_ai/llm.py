"""Shared Groq-backed LLM client, instrumented with Neatlogs tracing.

Every agent (Analyst, Curriculum, Judge, ...) calls get_client() to reach the
same Groq client, wrapped once so traces land in the same Neatlogs workflow.
"""

import os
import time

from dotenv import load_dotenv

load_dotenv()

import neatlogs
from openai import OpenAI, RateLimitError

MODEL = "openai/gpt-oss-20b"

# MODEL is a reasoning model and charges reasoning tokens against max_tokens, so
# a budget sized for the answer alone gets spent thinking and comes back empty.
# Every agent here does mechanical extraction and mapping, not puzzles.
REASONING_EFFORT = "low"

# Groq's on-demand tier caps tokens per minute, and one Analyst run over several
# Sources followed by a Curriculum run exceeds it. The cap is per rolling minute,
# so waiting clears it.
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
                api_key=os.environ["GROQ_API_KEY"],
                base_url="https://api.groq.com/openai/v1",
            )
        )
    return _client


def _retry_after_seconds(error: RateLimitError, fallback: int) -> float:
    header = getattr(error, "response", None) and error.response.headers.get("retry-after")
    try:
        return max(float(header), 1.0) if header else fallback
    except (TypeError, ValueError):
        return fallback


def complete(prompt: str, *, max_tokens: int, reasoning_effort: str = REASONING_EFFORT) -> tuple[str, str | None]:
    """One chat completion, waiting out the tier's per-minute token limit.

    Returns the content and the finish reason; the caller decides whether an
    empty or truncated answer is tolerable (see ADR-0002).
    """
    for wait in (*_RATE_LIMIT_WAITS_SECONDS, None):
        try:
            response = get_client().chat.completions.create(
                model=MODEL,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
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
