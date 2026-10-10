"""Analyst agent: fetches live public sources and produces a ranked Pareto Table.

Fetching and LLM extraction are live I/O against real sources and are
smoke-tested only, per the spec's Testing Decisions. aggregate_pareto_table
is the pure, TDD'd core: given what each source said, rank skills by how
many distinct sources cite them.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import neatlogs
import requests
from bs4 import BeautifulSoup

from pareto_ai.llm import complete

EXTRACTION_MAX_TOKENS = 1024
ALIAS_MAX_TOKENS = 4096
# Per-Source prompt budget. Several Sources plus the Curriculum agent share one
# per-minute token quota, so each page is trimmed rather than sent whole.
PAGE_TEXT_CHARS = 5000

USER_AGENT = "ParetoAI-Analyst/0.1 (+https://github.com/CaptainDiv/ParetoAI)"


@dataclass(frozen=True)
class Source:
    name: str
    url: str


# PLACEHOLDER — these are NOT Sources as CONTEXT.md defines one.
#
# ADR-0003 settled that a Source is a published measure of market demand
# (occupational task data, developer survey results, certification exam
# objectives), and that reference documentation is explicitly not one: docs
# partition a subject instead of measuring demand for it, so counting them
# ranked these skills by which pages happened to be picked. Measured live, 45
# of 45 entries tied, and the rank order was really alphabetical.
#
# They stay only so the Analyst runs end-to-end while real demand Sources are
# sourced (#11). Every Demand Weight computed from this tuple is meaningless —
# do not build on, tune against, or demo the resulting ranking.
#
# One constraint that outlives the swap: server-rendered pages only. An SPA
# (roadmap.sh, book.getfoundry.sh) returns an empty shell to a plain GET, and a
# GitHub topic page returns its nav chrome plus a repo listing.
DEFAULT_SOURCES: tuple[Source, ...] = (
    Source("Ethereum.org developer docs index", "https://ethereum.org/en/developers/docs/"),
    Source("Ethereum.org smart contracts", "https://ethereum.org/en/developers/docs/smart-contracts/"),
    Source(
        "Ethereum.org smart contract security",
        "https://ethereum.org/en/developers/docs/smart-contracts/security/",
    ),
    Source("Solidity documentation", "https://docs.soliditylang.org/en/latest/"),
    Source(
        "Solidity security considerations",
        "https://docs.soliditylang.org/en/latest/security-considerations.html",
    ),
    Source("OpenZeppelin Contracts", "https://docs.openzeppelin.com/contracts/5.x/"),
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


_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def canonical_skill(skill: str) -> str:
    """The key that two spellings of the same skill share.

    Each Source names skills in its own words, so two names for one skill must
    fold together before what the Sources report about it can be combined at
    all. Folding case, unicode dashes, parenthetical qualifiers and punctuation
    puts "ERC-20 standard", "ERC‑20 Standard" and "erc 20 standard (fungible
    tokens)" on one Pareto Table entry. Genuinely different wordings for the
    same skill are a judgement call, so merge_skill_aliases handles those.
    """
    folded = unicodedata.normalize("NFKD", skill).casefold()
    return _NON_ALPHANUMERIC.sub(" ", _PARENTHETICAL.sub(" ", folded)).strip()


def _best_spelling(counts: Counter[str]) -> str:
    """How to display a merged entry: the most-cited spelling, then the tidiest."""
    return min(counts, key=lambda spelling: (-counts[spelling], len(spelling), spelling))


def aggregate_pareto_table(target_skill: str, extractions: list[SourceExtraction]) -> ParetoTable:
    citing_sources: dict[str, list[str]] = {}
    spellings: dict[str, Counter[str]] = {}
    for extraction in extractions:
        for raw_skill in extraction.skills:
            skill = raw_skill.strip()
            key = canonical_skill(skill)
            if not key:
                continue
            spellings.setdefault(key, Counter())[skill] += 1
            sources_for_skill = citing_sources.setdefault(key, [])
            if extraction.source.name not in sources_for_skill:
                sources_for_skill.append(extraction.source.name)

    entries = [
        ParetoEntry(
            skill=_best_spelling(spellings[key]), frequency=len(sources), sources=tuple(sources)
        )
        for key, sources in citing_sources.items()
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
    return "\n".join(lines)[:PAGE_TEXT_CHARS]


def apply_aliases(
    extractions: list[SourceExtraction], aliases: Mapping[str, str]
) -> list[SourceExtraction]:
    """Rewrite each extracted skill name to the canonical name for that skill.

    Pure counterpart to merge_skill_aliases: the model decides which names mean
    the same thing, this applies that decision. A name with no alias is kept.
    """
    return [
        SourceExtraction(
            source=extraction.source,
            skills=tuple(aliases.get(skill, skill) for skill in extraction.skills),
        )
        for extraction in extractions
    ]


@neatlogs.span(kind="AGENT", role="Analyst")
def merge_skill_aliases(target_skill: str, skills: Sequence[str]) -> dict[str, str]:
    """Group differently-worded names for one skill under a single canonical name.

    canonical_skill folds spelling variants, but Sources also disagree in
    substance — "role-based access control", "Role-based permissioning" and
    "Access Control" are one skill under three names, and left unmerged they
    stay three separate entries that each carry a share of the one skill's
    standing. Judging which names mean the same thing needs a model, and it gets
    its own call so the names are all it has to look at: the same nudge bolted
    onto each page-extraction prompt was reliably ignored.

    Degrades to no merging rather than failing the Analyst, per ADR-0002 — an
    unmerged table shows up in the spread _print_table reports.
    """
    if not skills:
        return {}
    content, _ = complete(
        f'These skill names were extracted from several sources about "{target_skill}". '
        "Some are different wordings of the SAME skill.\n\n"
        "NAMES:\n" + "\n".join(f"- {skill}" for skill in skills) + "\n\n"
        "Group the names that mean the same skill and give each group ONE canonical "
        "name, chosen as the clearest name already in that group. Map every name "
        "above to its group's canonical name (a name in no group maps to itself). "
        "Only group names that a practitioner would call the same skill — do not "
        "merely merge things that are related or adjacent.\n"
        'Respond with ONLY a JSON object of the form {"name": "canonical name"}. No prose.',
        max_tokens=ALIAS_MAX_TOKENS,
    )
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(name): str(canonical) for name, canonical in parsed.items() if isinstance(canonical, str)}


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
    prompt = (
        f'You are extracting a skills inventory for the target skill "{target_skill}" '
        f"from a page (source: {source.name}).\n\n"
        f'List the 5-10 most distinct, specific sub-skills or topics relevant to "{target_skill}" '
        'that this page actually covers (e.g. "reentrancy guards", "gas optimization", '
        '"ERC-20 standard"). Keep each one short (a few words).\n'
        "Respond with ONLY a JSON array of short skill-name strings, no prose.\n\n"
        f"PAGE TEXT:\n{raw_text}"
    )
    content, _ = complete(prompt, max_tokens=EXTRACTION_MAX_TOKENS)
    return SourceExtraction(source=source, skills=tuple(_parse_skill_list(content)))


@neatlogs.span(kind="WORKFLOW")
def run_analyst(target_skill: str, sources: tuple[Source, ...] = DEFAULT_SOURCES) -> ParetoTable:
    extractions = [
        extract_skills_from_source(target_skill, source, fetch_source_text(source)) for source in sources
    ]
    # Each Source names things in its own words, so merge the aliases before
    # counting; otherwise frequency measures phrasings rather than corroboration.
    extracted = [skill for extraction in extractions for skill in extraction.skills]
    aliases = merge_skill_aliases(target_skill, sorted(set(extracted)))
    return aggregate_pareto_table(target_skill, apply_aliases(extractions, aliases))


def _print_table(table: ParetoTable, sources: tuple[Source, ...] = DEFAULT_SOURCES) -> None:
    print(f"Pareto Table for: {table.target_skill}\n")
    for rank, entry in enumerate(table.entries, start=1):
        print(f"{rank:>2}. {entry.skill}  (frequency={entry.frequency}; sources: {', '.join(entry.sources)})")

    # ADR-0002 lets the Analyst treat an unusable extraction as "no skills" only
    # because that shows up in the table. It shows up here: a Source crediting 0
    # entries silently contributed nothing and needs looking at.
    print("\nEntries credited per source:")
    for source in sources:
        cited = sum(1 for entry in table.entries if source.name in entry.sources)
        print(f"  {cited:>3}  {source.name}{'   <- contributed nothing' if not cited else ''}")

    # The signal the top-20% cut actually ranks on. All-1s means no Source
    # corroborated another, so the cut is really alphabetical.
    spread = Counter(entry.frequency for entry in table.entries)
    print("\nFrequency spread:")
    for frequency in sorted(spread, reverse=True):
        print(f"  frequency={frequency}: {spread[frequency]} entries")
    if len(spread) <= 1:
        print("  (no spread — nothing corroborated, so the cut cannot be frequency-driven)")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        _print_table(run_analyst("smart contract development"))
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
