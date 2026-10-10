"""Shared Groq-backed LLM client, instrumented with Neatlogs tracing.

Every agent (Analyst, Curriculum, Judge, ...) calls get_client() to reach the
same Groq client, wrapped once so traces land in the same Neatlogs workflow.
"""

import os

from dotenv import load_dotenv

load_dotenv()

import neatlogs
from openai import OpenAI

MODEL = "openai/gpt-oss-20b"

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
