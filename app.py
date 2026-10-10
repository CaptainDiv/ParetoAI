"""Minimal Anthropic agent demo, instrumented with Neatlogs tracing."""

import neatlogs

from pareto_ai.llm import MODEL, get_client

client = get_client()


@neatlogs.span(kind="WORKFLOW")
def answer_question(question: str) -> str:
    response = client.chat.completions.create(
        model=MODEL,
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
