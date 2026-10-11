"""Analyst agent: fetches live demand Sources and produces a ranked Pareto Table.

Fetching and LLM extraction are live I/O against real Sources and are
smoke-tested only, per the spec's Testing Decisions. aggregate_pareto_table
is the pure, TDD'd core: given the demand each Source reported for each skill
on its own scale, rank skills by Demand Weight (ADR-0006).
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

from pareto_ai.llm import complete, json_object

# Sized for every rated item of a weighted Source (O*NET rates 17 tasks), plus
# the reasoning tokens MODEL charges against the same budget.
EXTRACTION_MAX_TOKENS = 2048
ALIAS_MAX_TOKENS = 4096
# Per-Source prompt budget. Several Sources plus the Curriculum agent share one
# per-minute token quota, so each page is trimmed rather than sent whole.
PAGE_TEXT_CHARS = 5000

USER_AGENT = "ParetoAI-Analyst/0.1 (+https://github.com/CaptainDiv/ParetoAI)"


@dataclass(frozen=True)
class Rating:
    """Demand reported as a number where higher is more demanded, e.g. O*NET's 0-100 importance."""

    maximum: float

    @property
    def instruction(self) -> str:
        return (
            f"This page rates items on a scale up to {self.maximum:g}, higher meaning more important. "
            "Give each skill the rating of the item it comes from, and leave out any skill whose "
            "item has no rating."
        )

    def normalize(self, reported: float) -> float | None:
        return reported / self.maximum if 0 <= reported <= self.maximum else None


@dataclass(frozen=True)
class Rank:
    """Demand reported as a place in a list, 1 being most demanded, e.g. OWASP's SC01-SC10."""

    count: int

    @property
    def instruction(self) -> str:
        return (
            f"This page ranks items from 1 (most important) to {self.count}. "
            "Give each skill the rank of the item it comes from."
        )

    def normalize(self, reported: float) -> float | None:
        # Range first: it also rejects inf and NaN, which int() would raise on.
        if not 1 <= reported <= self.count or reported != int(reported):
            return None
        # Linear in place: an ordinal ranking says nothing about the gaps between places.
        return (self.count - reported + 1) / self.count


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    # The Source's Demand Scale. None means it only enumerates skills (a
    # certification's objectives list): it can add entries but not weigh them.
    scale: Rating | Rank | None
    measures_whole_target_skill: bool

    @property
    def counts_silence_as_no_demand(self) -> bool:
        """Per CONTEXT.md, absence counts against a skill only from a Source measuring
        the whole Target Skill; and only a Source with a scale can report any demand."""
        return self.scale is not None and self.measures_whole_target_skill


# Demand Sources for smart contract development, chosen and fetch-verified in
# docs/research/demand-sources-for-smart-contract-development.md. Reference
# documentation is deliberately absent: it is not a Source (ADR-0003).
#
# - O*NET rates the whole occupation's tasks 0-100; the only numeric weighting
#   found, but occupation-level, so it is coarse where Modules are fine.
# - OWASP's Top 10 ordering comes from a practitioner survey plus incident data.
#   It covers security only, so its silence about a skill says nothing. It is
#   the one OWASP signal: SCWE and EthTrust would add no weight, only more
#   security entries, the security-bias trap ADR-0003 warns about.
# - Cyfrin's exam objectives decompose the skill to Solidity level but publish
#   no weighting. robots.txt permits /certifications/; no terms of use found.
#
# O*NET is CC BY 4.0: anything that publishes a table built from it must credit
# O*NET and the U.S. Department of Labor, Employment and Training Administration.
#
# Server-rendered pages only. An SPA returns an empty shell to a plain GET.
DEFAULT_SOURCES: tuple[Source, ...] = (
    Source(
        "O*NET 15-1299.07 Blockchain Engineers",
        "https://www.onetonline.org/link/details/15-1299.07",
        scale=Rating(maximum=100),
        measures_whole_target_skill=True,
    ),
    Source(
        "OWASP Smart Contract Top 10",
        "https://scs.owasp.org/sctop10/",
        scale=Rank(count=10),
        measures_whole_target_skill=False,
    ),
    Source(
        "Cyfrin Updraft SSCD+ exam objectives",
        "https://updraft.cyfrin.io/certifications/solidity-certification",
        scale=None,
        measures_whole_target_skill=True,
    ),
)


@dataclass(frozen=True)
class CitedSkill:
    name: str
    demand: float | None = None  # in the citing Source's own scale; None if it has none


@dataclass(frozen=True)
class SourceExtraction:
    source: Source
    skills: tuple[CitedSkill, ...]


@dataclass(frozen=True)
class ParetoEntry:
    skill: str
    demand_weight: float
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ParetoTable:
    target_skill: str
    entries: tuple[ParetoEntry, ...]  # sorted by Demand Weight desc, then skill asc
    # (Source name, skill) citations left out because the Source's scale couldn't
    # read the demand given. Recorded so the shortfall is visible (ADR-0002).
    unreadable_citations: tuple[tuple[str, str], ...] = ()


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
    # canonical key -> citing Source name -> demand reported as a fraction of the
    # Source's scale, or None from a Source with no scale
    reported: dict[str, dict[str, float | None]] = {}
    spellings: dict[str, Counter[str]] = {}
    unreadable: list[tuple[str, str]] = []
    for extraction in extractions:
        source = extraction.source
        for cited in extraction.skills:
            skill = cited.name.strip()
            key = canonical_skill(skill)
            if not key:
                continue
            demand = None
            if source.scale is not None:
                demand = source.scale.normalize(cited.demand) if cited.demand is not None else None
                if demand is None:
                    # Kept unweighed it would escape this Source's 0 (ADR-0006).
                    unreadable.append((source.name, skill))
                    continue
            spellings.setdefault(key, Counter())[skill] += 1
            by_source = reported.setdefault(key, {})
            earlier = by_source.get(source.name)
            by_source[source.name] = demand if earlier is None or demand is None else max(earlier, demand)

    reporting = {name for by_source in reported.values() for name in by_source}
    silence_counts = {
        extraction.source.name
        for extraction in extractions
        if extraction.source.counts_silence_as_no_demand and extraction.source.name in reporting
    }
    entries = [
        ParetoEntry(
            skill=_best_spelling(spellings[key]),
            demand_weight=_demand_weight(by_source, silence_counts),
            sources=tuple(by_source),
        )
        for key, by_source in reported.items()
    ]
    entries.sort(key=lambda entry: (-entry.demand_weight, entry.skill))
    return ParetoTable(target_skill=target_skill, entries=tuple(entries), unreadable_citations=tuple(unreadable))


def _demand_weight(by_source: Mapping[str, float | None], silence_counts: set[str]) -> float:
    """Mean demand over the Sources entitled to weigh this skill (ADR-0006).

    Those are the Sources with a scale that cite it, plus the Sources with a
    scale that measure the whole Target Skill and don't: their silence reports
    no demand. A Source measuring one part of it is silent about the rest only
    because it is about something else, so it is left out rather than read as 0.
    """
    weighed = [demand for demand in by_source.values() if demand is not None]
    weighed += [0.0] * len(silence_counts.difference(by_source))
    return sum(weighed) / len(weighed) if weighed else 0.0


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
            skills=tuple(
                CitedSkill(aliases.get(cited.name, cited.name), cited.demand) for cited in extraction.skills
            ),
        )
        for extraction in extractions
    ]


class AliasMergeError(RuntimeError):
    """The model twice failed to say which skill names are aliases, so the table can't be trusted."""


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

    Refuses rather than degrading to no merging, per ADR-0002: an unmerged skill
    stays split across its names, and the name only a partial Source used takes
    a whole-skill Source's 0 (ADR-0006), with nothing in the table to show it.
    """
    if not skills:
        return {}
    prompt = (
        f'These skill names were extracted from several sources about "{target_skill}". '
        "Some are different wordings of the SAME skill.\n\n"
        "NAMES:\n" + "\n".join(f"- {skill}" for skill in skills) + "\n\n"
        "Group the names that mean the same skill and give each group ONE canonical "
        "name, chosen as the clearest name already in that group. Map every name "
        "above to its group's canonical name (a name in no group maps to itself). "
        "Only group names that a practitioner would call the same skill — do not "
        "merely merge things that are related or adjacent.\n"
        'Respond with ONLY a JSON object of the form {"name": "canonical name"}. No prose.'
    )
    content, finish_reason = complete(prompt, max_tokens=ALIAS_MAX_TOKENS)
    parsed = json_object(content)
    if parsed is None:
        # One retry, as for the Curriculum agent's plans: a malformed answer is cheap to re-ask for.
        content, finish_reason = complete(prompt, max_tokens=ALIAS_MAX_TOKENS)
        parsed = json_object(content)
    if parsed is None:
        raise AliasMergeError(
            f"alias-merge response was not a JSON object twice (finish_reason={finish_reason}): {content[:300]!r}"
        )
    return {str(name): str(canonical) for name, canonical in parsed.items() if isinstance(canonical, str)}


def parse_cited_skills(content: str) -> tuple[CitedSkill, ...]:
    """The skills an extraction response says a Source cites, with any demand reported.

    An unusable response yields nothing, which ADR-0002 accepts because it shows
    up as a Source credited with no entries. A demand that isn't a number is read
    as none; aggregate_pareto_table decides what that means for the Source's scale.
    """
    start, end = content.find("["), content.rfind("]")
    if start == -1 or end == -1:
        return ()
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return ()
    cited: list[CitedSkill] = []
    for item in parsed:
        if isinstance(item, str):
            cited.append(CitedSkill(item))
        elif isinstance(item, dict) and isinstance(item.get("skill"), str):
            demand = item.get("demand")
            is_number = isinstance(demand, (int, float)) and not isinstance(demand, bool)
            cited.append(CitedSkill(item["skill"], demand if is_number else None))
    return tuple(cited)


@neatlogs.span(kind="AGENT", role="Analyst")
def extract_skills_from_source(target_skill: str, source: Source, raw_text: str) -> SourceExtraction:
    if source.scale is None:
        wanted = f'the 5-10 most distinct, specific sub-skills or topics relevant to "{target_skill}" that this page actually covers'
        response_format = "Respond with ONLY a JSON array of short skill-name strings, no prose."
    else:
        # Every item, not a sample: a skill the extractor leaves out of a
        # whole-skill Source is read as that Source reporting no demand for it.
        wanted = f'the sub-skill behind EVERY item this page rates or ranks that is relevant to "{target_skill}"'
        response_format = (
            f"{source.scale.instruction}\n"
            'Respond with ONLY a JSON array of objects of the form {"skill": "short name", '
            '"demand": number}, no prose.'
        )
    prompt = (
        f'You are extracting a skills inventory for the target skill "{target_skill}" '
        f"from a page (source: {source.name}).\n\n"
        f'List {wanted} (e.g. "reentrancy guards", "gas optimization", '
        '"ERC-20 standard"). Keep each one short (a few words).\n'
        f"{response_format}\n\n"
        f"PAGE TEXT:\n{raw_text}"
    )
    content, _ = complete(prompt, max_tokens=EXTRACTION_MAX_TOKENS)
    return SourceExtraction(source=source, skills=parse_cited_skills(content))


@neatlogs.span(kind="WORKFLOW")
def run_analyst(target_skill: str, sources: tuple[Source, ...] = DEFAULT_SOURCES) -> ParetoTable:
    extractions = [
        extract_skills_from_source(target_skill, source, fetch_source_text(source)) for source in sources
    ]
    # Each Source names things in its own words, so merge the aliases before
    # combining; otherwise one skill's demand splits across its phrasings.
    extracted = {cited.name for extraction in extractions for cited in extraction.skills}
    aliases = merge_skill_aliases(target_skill, sorted(extracted))
    return aggregate_pareto_table(target_skill, apply_aliases(extractions, aliases))


def _print_table(table: ParetoTable, sources: tuple[Source, ...] = DEFAULT_SOURCES) -> None:
    print(f"Pareto Table for: {table.target_skill}\n")
    for rank, entry in enumerate(table.entries, start=1):
        print(f"{rank:>2}. {entry.skill}  (demand weight {entry.demand_weight:.2f}; sources: {', '.join(entry.sources)})")

    # ADR-0002 lets the Analyst treat an unusable extraction as "no skills", and
    # a citation with an unreadable demand as no citation, only because both
    # show up in the table. They show up here.
    print("\nEntries credited per source:")
    for source in sources:
        cited = sum(1 for entry in table.entries if source.name in entry.sources)
        left_out = sum(1 for name, _ in table.unreadable_citations if name == source.name)
        notes = (["contributed nothing"] if not cited else []) + (
            [f"{left_out} cited without a readable demand, left out"] if left_out else []
        )
        suffix = f"   <- {'; '.join(notes)}" if notes else ""
        print(f"  {cited:>3}  {source.name}{suffix}")

    # The signal the top-20% cut actually ranks on. Entries tied on it fall back
    # to alphabetical order, and every entry no weighing Source reported demand
    # for ties at 0, so a cut that reaches them is alphabetical from there on.
    weights = [entry.demand_weight for entry in table.entries]
    unweighed = weights.count(0.0)
    print(
        f"\nDemand Weight spread: {len(set(weights))} distinct values across {len(weights)} entries; "
        f"{unweighed} at 0 (no Source that weighs skills reported demand for them)"
    )
    if len(set(weights) - {0.0}) <= 1:
        print("  (no spread — the cut cannot be demand-driven)")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        _print_table(run_analyst("smart contract development"))
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
