"""Analyst agent: fetches live public sources and produces a ranked Pareto Table.

Fetching and LLM extraction are live I/O against real sources and are
smoke-tested only, per the spec's Testing Decisions. aggregate_pareto_table
is the pure, TDD'd core: given what each source said, rank skills by how
many distinct sources cite them.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

import neatlogs
import requests
from bs4 import BeautifulSoup

from pareto_ai.llm import MODEL, get_client

USER_AGENT = "ParetoAI-Analyst/0.1 (+https://github.com/CaptainDiv/ParetoAI)"


@dataclass(frozen=True)
class Source:
    name: str
    url: str


# Live, no-auth-required sources for the MVP Target Skill (smart contract
# development): canonical docs and a community-signal source (top repos and
# their topic tags for the "solidity" GitHub topic). Swapping this tuple is
# how the Analyst generalizes to other Target Skills. Server-rendered pages
# only: an SPA (e.g. roadmap.sh) returns an empty shell to a plain GET.
DEFAULT_SOURCES: tuple[Source, ...] = (
    Source("Ethereum.org developer docs", "https://ethereum.org/en/developers/docs/smart-contracts/"),
    Source("Solidity documentation", "https://docs.soliditylang.org/en/latest/"),
    Source("GitHub solidity topic (top repos)", "https://github.com/topics/solidity"),
)


@dataclass(frozen=True)
class SourceExtraction:
    source: Source
    skills: tuple[str, ...]


@dataclass(frozen=True)
class ParetoEntry:
    skill: str
    frequency: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ParetoTable:
    target_skill: str
    entries: tuple[ParetoEntry, ...]  # sorted by frequency desc, then skill asc


def aggregate_pareto_table(target_skill: str, extractions: list[SourceExtraction]) -> ParetoTable:
    citing_sources: dict[str, list[str]] = {}
    for extraction in extractions:
        for raw_skill in extraction.skills:
            skill = raw_skill.strip()
            if not skill:
                continue
            sources_for_skill = citing_sources.setdefault(skill, [])
            if extraction.source.name not in sources_for_skill:
                sources_for_skill.append(extraction.source.name)

    entries = [
        ParetoEntry(skill=skill, frequency=len(sources), sources=tuple(sources))
        for skill, sources in citing_sources.items()
    ]
    entries.sort(key=lambda entry: (-entry.frequency, entry.skill))
    return ParetoTable(target_skill=target_skill, entries=tuple(entries))


def fetch_source_text(source: Source, timeout: float = 15.0) -> str:
    response = requests.get(source.url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text(separator="\n").splitlines() if line.strip()]
    return "\n".join(lines)[:8000]  # bound prompt size


def _parse_skill_list(content: str) -> list[str]:
    start, end = content.find("["), content.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


@neatlogs.span(kind="AGENT", role="Analyst")
def extract_skills_from_source(target_skill: str, source: Source, raw_text: str) -> SourceExtraction:
    client = get_client()
    prompt = (
        f'You are extracting a skills inventory for the target skill "{target_skill}" '
        f"from a page (source: {source.name}).\n\n"
        f'List the 5-10 most distinct, specific sub-skills or topics relevant to "{target_skill}" '
        'that this page actually covers (e.g. "reentrancy guards", "gas optimization", '
        '"ERC-20 standard"). Keep each one short (a few words).\n'
        "Respond with ONLY a JSON array of short skill-name strings, no prose.\n\n"
        f"PAGE TEXT:\n{raw_text}"
    )
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.choices[0].message.content or "[]"
    return SourceExtraction(source=source, skills=tuple(_parse_skill_list(content)))


@neatlogs.span(kind="WORKFLOW")
def run_analyst(target_skill: str, sources: tuple[Source, ...] = DEFAULT_SOURCES) -> ParetoTable:
    extractions = [
        extract_skills_from_source(target_skill, source, fetch_source_text(source)) for source in sources
    ]
    return aggregate_pareto_table(target_skill, extractions)


def _print_table(table: ParetoTable) -> None:
    print(f"Pareto Table for: {table.target_skill}\n")
    for rank, entry in enumerate(table.entries, start=1):
        print(f"{rank:>2}. {entry.skill}  (frequency={entry.frequency}; sources: {', '.join(entry.sources)})")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        _print_table(run_analyst("smart contract development"))
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
