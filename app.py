"""Minimal Anthropic agent demo, instrumented with Neatlogs tracing."""

import os

from dotenv import load_dotenv

load_dotenv()

import neatlogs
from openai import OpenAI

neatlogs.init(
    api_key=os.environ["NEATLOGS_API_KEY"],
    workflow_name="pareto-ai-demo",
)

client = neatlogs.wrap(
    OpenAI(
        api_key=os.environ["GROQ_API_KEY"],
        base_url="https://api.groq.com/openai/v1",
    )
)


@neatlogs.span(kind="WORKFLOW")
def answer_question(question: str) -> str:
    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    return response.choices[0].message.content


def main() -> None:
    try:
        answer = answer_question("What is the capital of France?")
        print(answer)
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
