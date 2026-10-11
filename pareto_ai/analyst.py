"""Analyst agent: fetches live demand Sources and produces a ranked Pareto Table.

Fetching and LLM extraction are live I/O against real Sources and are
smoke-tested only, per the spec's Testing Decisions. aggregate_pareto_table
is the pure, TDD'd core: given the demand each Source reported on its own
scale, and which Occupational Tasks each skill serves, rank skills by Demand
Weight (ADR-0006, ADR-0007). parse_served_occupational_tasks, which reads the
model's answer to the latter, is TDD'd too.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TypeGuard

import neatlogs
import requests
from bs4 import BeautifulSoup

from pareto_ai.llm import complete, json_object

# Sized for every rated item of a weighted Source (O*NET rates 17 Occupational
# Tasks, quoted whole), plus the reasoning tokens MODEL charges against the
# same budget.
EXTRACTION_MAX_TOKENS = 2048
ALIAS_MAX_TOKENS = 4096
# Which Occupational Task a skill serves is a judgement that decides its Demand
# Weight, not mechanical extraction, so it gets more reasoning and the budget for it.
CLASSIFICATION_MAX_TOKENS = 8192
CLASSIFICATION_REASONING_EFFORT = "medium"
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
            "Give each one the rating of the item it comes from, and leave out any whose item "
            "has no rating."
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
            "Give each one the rank of the item it comes from."
        )

    def normalize(self, reported: float) -> float | None:
        # Range first: it also rejects inf and NaN, which int() would raise on.
        if not 1 <= reported <= self.count or reported != int(reported):
            return None
        # Only the order of this fraction is used: a Rank orders skills but does
        # not weigh them, since a place says nothing about how much less the
        # last one is worth (ADR-0007).
        return (self.count - reported + 1) / self.count


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    # The Source's Demand Scale. None means it only enumerates skills (a
    # certification's objectives list): it can add entries but not weigh them.
    scale: Rating | Rank | None
    measures_whole_target_skill: bool
    # True when the Source rates Occupational Tasks (O*NET) rather than naming
    # skills. It then adds no entries of its own: it weighs the skills other
    # Sources name through the Occupational Tasks they serve (ADR-0007).
    rates_occupational_tasks: bool = False

    @property
    def counts_silence_as_no_demand(self) -> bool:
        """Per CONTEXT.md, absence counts against a skill only from a Source measuring
        the whole Target Skill; and only a Rating weighs, so only it can report no demand."""
        return isinstance(self.scale, Rating) and self.measures_whole_target_skill


# Demand Sources for smart contract development, chosen and fetch-verified in
# docs/research/demand-sources-for-smart-contract-development.md. Reference
# documentation is deliberately absent: it is not a Source (ADR-0003).
#
# - O*NET rates the whole occupation's Occupational Tasks 0-100: the only
#   numeric weighting found, but occupation-level, never naming a skill a
#   Module could teach. So it adds no entries, and weighs each skill the other
#   Sources name through the Occupational Tasks it serves (ADR-0007).
# - OWASP's Top 10 ordering comes from a practitioner survey plus incident data.
#   It covers security only, so its silence about a skill says nothing, and as
#   a Rank it orders skills without weighing them (ADR-0007). It is the one
#   OWASP signal: SCWE and EthTrust would add only more security entries, each
#   weighed through O*NET's top security Occupational Tasks, flooding the cut — the
#   security-bias trap ADR-0003 warns about.
# - Cyfrin's exam objectives decompose the skill to Solidity level but publish
#   no weighting; O*NET weighs them. robots.txt permits /certifications/; no
#   terms of use found.
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
        rates_occupational_tasks=True,
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
class OccupationalTask:
    description: str  # the Source's own words, not a paraphrase
    demand: float | None = None  # in the rating Source's own scale


@dataclass(frozen=True)
class SourceExtraction:
    source: Source
    skills: tuple[CitedSkill, ...]
    occupational_tasks: tuple[OccupationalTask, ...] = ()  # only from a Source that rates them


@dataclass(frozen=True)
class ParetoEntry:
    skill: str
    demand_weight: float
    sources: tuple[str, ...]
    # The Occupational Tasks this skill was weighed through, strongest first.
    # Which Occupational Task a skill serves is the model's call, so it is shown.
    occupational_tasks: tuple[OccupationalTask, ...] = ()


@dataclass(frozen=True)
class ParetoTable:
    target_skill: str
    entries: tuple[ParetoEntry, ...]  # by Demand Weight desc, then a Rank's order, then skill asc
    # (Source name, skill or Occupational Task) reports left out because the
    # Source's scale couldn't read the demand given. Recorded so the shortfall
    # is visible (ADR-0002).
    unreadable_citations: tuple[tuple[str, str], ...] = ()
    # Occupational Tasks a Source rated that no skill here serves, strongest
    # first: the parts of the job this table doesn't reach.
    unserved_occupational_tasks: tuple[OccupationalTask, ...] = ()


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


def aggregate_pareto_table(
    target_skill: str,
    extractions: list[SourceExtraction],
    served_occupational_tasks: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
) -> ParetoTable:
    """Rank the skills the Sources name by Demand Weight.

    served_occupational_tasks maps each Source that rates Occupational Tasks to
    which of its Occupational Tasks each skill serves (an empty sequence: serves
    none).
    """
    # canonical key -> citing Source name -> demand reported as a fraction of the
    # Source's scale, or None from a Source with no scale
    reported: dict[str, dict[str, float | None]] = {}
    spellings: dict[str, Counter[str]] = {}
    unreadable: list[tuple[str, str]] = []
    for extraction in extractions:
        source = extraction.source
        if source.rates_occupational_tasks:
            continue  # it adds no entries: it weighs the skills others name, below
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

    # Sources that rated at least one Occupational Task readably. They reported
    # something even if no skill serves any of their Occupational Tasks.
    rated_occupational_tasks: set[str] = set()
    # canonical key -> (demand as a fraction, Occupational Task) for each one it serves
    weighed_through: dict[str, list[tuple[float, OccupationalTask]]] = {}
    unserved: list[tuple[float, OccupationalTask]] = []
    for extraction in extractions:
        source = extraction.source
        if not source.rates_occupational_tasks or source.scale is None:
            continue
        readable: dict[str, tuple[float, OccupationalTask]] = {}
        for task in extraction.occupational_tasks:
            demand = source.scale.normalize(task.demand) if task.demand is not None else None
            if demand is None:
                # A skill serving only this one is left to the Source's silence (ADR-0006).
                unreadable.append((source.name, task.description))
                continue
            earlier = readable.get(task.description)
            if earlier is None or demand > earlier[0]:
                readable[task.description] = (demand, task)
        if readable:
            rated_occupational_tasks.add(source.name)
        served_by = (served_occupational_tasks or {}).get(source.name, {})
        for key, by_source in reported.items():
            if readable and not any(spelling in served_by for spelling in spellings[key]):
                # Only an explicit answer says a skill serves none (ADR-0007).
                raise ValueError(
                    f"no answer for which of {source.name}'s Occupational Tasks "
                    f"{_best_spelling(spellings[key])!r} serves"
                )
            served = {
                readable[task]
                for spelling in spellings[key]
                for task in served_by.get(spelling, ())
                if task in readable
            }
            if served:
                # One skill serving several Occupational Tasks counts once, at the strongest.
                by_source[source.name] = max(demand for demand, _ in served)
                weighed_through.setdefault(key, []).extend(served)
        serving = {task for served in weighed_through.values() for _, task in served}
        unserved += [(demand, task) for demand, task in readable.values() if task not in serving]

    reporting = {name for by_source in reported.values() for name in by_source} | rated_occupational_tasks
    silence_counts = {
        extraction.source.name
        for extraction in extractions
        if extraction.source.counts_silence_as_no_demand and extraction.source.name in reporting
    }
    rating_sources = {extraction.source.name for extraction in extractions if isinstance(extraction.source.scale, Rating)}
    rank_sources = {extraction.source.name for extraction in extractions if isinstance(extraction.source.scale, Rank)}
    ranked = [
        (
            ParetoEntry(
                skill=_best_spelling(spellings[key]),
                demand_weight=_demand_weight(by_source, rating_sources, silence_counts),
                sources=tuple(by_source),
                occupational_tasks=_strongest_first(weighed_through.get(key, [])),
            ),
            _rank_standing(by_source, rank_sources),
        )
        for key, by_source in reported.items()
    ]
    # A Rank orders skills of equal Demand Weight: one it places comes first, in
    # its order, ahead of one it doesn't place (ADR-0007).
    ranked.sort(key=lambda pair: (-pair[0].demand_weight, pair[1] is None, -(pair[1] or 0.0), pair[0].skill))
    return ParetoTable(
        target_skill=target_skill,
        entries=tuple(entry for entry, _ in ranked),
        unreadable_citations=tuple(unreadable),
        unserved_occupational_tasks=_strongest_first(unserved),
    )


def _demand_weight(
    by_source: Mapping[str, float | None], rating_sources: set[str], silence_counts: set[str]
) -> float:
    """Mean demand over the Ratings entitled to weigh this skill (ADR-0006, ADR-0007).

    Those are the Sources with a Rating that report on it, plus the Sources with
    a Rating that measure the whole Target Skill and don't: their silence
    reports no demand. A Source measuring one part of it is silent about the
    rest only because it is about something else, so it is left out rather than
    read as 0. A Rank says only which comes first, so it does not weigh.
    """
    weighed = [demand for name, demand in by_source.items() if name in rating_sources and demand is not None]
    weighed += [0.0] * len(silence_counts.difference(by_source))
    return sum(weighed) / len(weighed) if weighed else 0.0


def _strongest_first(rated: Sequence[tuple[float, OccupationalTask]]) -> tuple[OccupationalTask, ...]:
    return tuple(task for _, task in sorted(rated, key=lambda pair: (-pair[0], pair[1].description)))


def _rank_standing(by_source: Mapping[str, float | None], rank_sources: set[str]) -> float | None:
    """The best place any Rank gives this skill, as a fraction of its list, or None.

    The best rather than the mean: averaging places reads them as levels, and
    would let one more Rank placing a skill lower it (ADR-0007).
    """
    placed = [demand for name, demand in by_source.items() if name in rank_sources and demand is not None]
    return max(placed) if placed else None


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
        replace(
            extraction,
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
    return tuple(CitedSkill(name, demand) for name, demand in _parse_named_items(content, "skill"))


def parse_occupational_tasks(content: str) -> tuple[OccupationalTask, ...]:
    """The Occupational Tasks an extraction response says a Source rates, with any rating given."""
    return tuple(OccupationalTask(text, demand) for text, demand in _parse_named_items(content, "task"))


def _parse_named_items(content: str, key: str) -> list[tuple[str, float | None]]:
    """(name, demand) for each item of the JSON array in a response, named under `key`."""
    start, end = content.find("["), content.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return []
    named: list[tuple[str, float | None]] = []
    for item in parsed:
        if isinstance(item, str):
            named.append((item, None))
        elif isinstance(item, dict) and isinstance(item.get(key), str):
            demand = item.get("demand")
            is_number = isinstance(demand, (int, float)) and not isinstance(demand, bool)
            named.append((item[key], demand if is_number else None))
    return named


def parse_served_occupational_tasks(
    content: str, skills: Sequence[str], occupational_tasks: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """Which Occupational Tasks a classification response says each skill serves.

    occupational_tasks are numbered from 1 in the prompt, and the model answers
    with those numbers. A skill appears in the result only if its answer is
    usable, with an explicit empty list meaning it serves none. A skill left out
    is unanswered, never "serves none": read as none it would take a whole-skill
    Source's 0 the Source never reported, so the caller re-asks or refuses.
    """
    parsed = json_object(content) or {}
    count = len(occupational_tasks)
    served: dict[str, tuple[str, ...]] = {}
    for skill in skills:
        answer = parsed.get(skill)
        # A lone number is read as the one Occupational Task it plainly means.
        answered = [answer] if _is_occupational_task_number(answer, count) else answer
        if not isinstance(answered, list):
            continue
        numbers = [number for number in answered if _is_occupational_task_number(number, count)]
        if len(numbers) == len(answered):
            served[skill] = tuple(occupational_tasks[number - 1] for number in numbers)
    return served


def _is_occupational_task_number(number: object, count: int) -> TypeGuard[int]:
    # bool is an int subclass, and 0 would index the last Occupational Task from the end.
    return isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= count


class OccupationalTaskClassificationError(RuntimeError):
    """The model twice left skills unclassified, so their Demand Weight can't be trusted."""


def _served_occupational_tasks_prompt(
    target_skill: str, skills: Sequence[str], occupational_tasks: Sequence[str]
) -> str:
    return (
        f'A demand source rates these occupational tasks for the job behind "{target_skill}":\n\n'
        + "\n".join(f"{number}. {task}" for number, task in enumerate(occupational_tasks, start=1))
        + "\n\nSKILLS:\n"
        + "\n".join(f"- {skill}" for skill in skills)
        + "\n\nFor each skill, give the number of the task above that the skill most directly "
        "serves: the work someone in this job actually does with that skill. Be strict — most "
        "skills serve exactly one task. Add a second number only if the skill is just as central "
        "to that task too; never list a task the skill is merely related to or could help with. "
        "If no task above needs the skill, give an empty list [].\n"
        "Answer for EVERY skill above, using its name exactly as written.\n"
        'Respond with ONLY a JSON object of the form {"skill": [task numbers]}. No prose.'
    )


def _classify(prompt: str) -> tuple[str, str | None]:
    return complete(
        prompt, max_tokens=CLASSIFICATION_MAX_TOKENS, reasoning_effort=CLASSIFICATION_REASONING_EFFORT
    )


@neatlogs.span(kind="AGENT", role="Analyst")
def classify_served_occupational_tasks(
    target_skill: str, skills: Sequence[str], occupational_tasks: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """Which of a Source's Occupational Tasks each skill serves (ADR-0007).

    The model judges this; the Source supplies every rating. A skill it leaves
    unanswered is asked about once more on its own, and if it is still
    unanswered the Analyst refuses (ADR-0002) rather than read it as serving
    none, which would hand it a whole-skill Source's 0 that Source never gave.
    """
    if not skills or not occupational_tasks:
        return {}
    prompt = _served_occupational_tasks_prompt(target_skill, skills, occupational_tasks)
    content, finish_reason = _classify(prompt)
    served = parse_served_occupational_tasks(content, skills, occupational_tasks)
    missing = [skill for skill in skills if skill not in served]
    if missing:
        prompt = _served_occupational_tasks_prompt(target_skill, missing, occupational_tasks)
        content, finish_reason = _classify(prompt)
        served |= parse_served_occupational_tasks(content, missing, occupational_tasks)
        missing = [skill for skill in skills if skill not in served]
    if missing:
        raise OccupationalTaskClassificationError(
            f"no usable Occupational Task answer for {missing!r} after one retry "
            f"(finish_reason={finish_reason}): {content[:300]!r}"
        )
    return served


@neatlogs.span(kind="AGENT", role="Analyst")
def extract_skills_from_source(target_skill: str, source: Source, raw_text: str) -> SourceExtraction:
    if source.rates_occupational_tasks and source.scale is not None:
        return _extract_occupational_tasks(target_skill, source, raw_text)
    if source.scale is None:
        # Every one, not a sample: a skill left out here is a skill the Source
        # that rates Occupational Tasks never gets to weigh.
        wanted = f'the sub-skill or topic behind EVERY objective or item this page lists as required for "{target_skill}"'
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


def _extract_occupational_tasks(target_skill: str, source: Source, raw_text: str) -> SourceExtraction:
    assert source.scale is not None
    # Word for word, not paraphrased into a skill name: a paraphrase once turned
    # "Discuss and plan systems with solution architects…" into "security audits".
    # Every one, because a skill serving a task left out would take this Source's 0.
    prompt = (
        f'You are extracting the occupational tasks a page rates for the job behind "{target_skill}" '
        f"(source: {source.name}).\n\n"
        "List EVERY task in the page's list of tasks — the statements of work the occupation "
        "performs, not its work activities, skills, knowledge or abilities. Copy each task word "
        "for word as the page writes it; do not shorten, paraphrase or rename it.\n"
        f"{source.scale.instruction}\n"
        'Respond with ONLY a JSON array of objects of the form {"task": "the task exactly as written", '
        '"demand": number}, no prose.\n\n'
        f"PAGE TEXT:\n{raw_text}"
    )
    content, _ = complete(prompt, max_tokens=EXTRACTION_MAX_TOKENS)
    return SourceExtraction(source=source, skills=(), occupational_tasks=parse_occupational_tasks(content))


@neatlogs.span(kind="WORKFLOW")
def run_analyst(target_skill: str, sources: tuple[Source, ...] = DEFAULT_SOURCES) -> ParetoTable:
    extractions = [
        extract_skills_from_source(target_skill, source, fetch_source_text(source)) for source in sources
    ]
    # Each Source names things in its own words, so merge the aliases before
    # combining; otherwise one skill's demand splits across its phrasings.
    extracted = {cited.name for extraction in extractions for cited in extraction.skills}
    aliases = merge_skill_aliases(target_skill, sorted(extracted))
    merged = apply_aliases(extractions, aliases)
    # Classified after merging, so each skill is judged once under one name.
    skills = sorted(
        {
            cited.name.strip()
            for extraction in merged
            if not extraction.source.rates_occupational_tasks
            for cited in extraction.skills
        }
        - {""}
    )
    served_occupational_tasks = {
        extraction.source.name: classify_served_occupational_tasks(
            target_skill,
            skills,
            list(dict.fromkeys(task.description for task in extraction.occupational_tasks if task.demand is not None)),
        )
        for extraction in merged
        if extraction.source.rates_occupational_tasks
    }
    return aggregate_pareto_table(target_skill, merged, served_occupational_tasks)


def _print_table(table: ParetoTable, sources: tuple[Source, ...] = DEFAULT_SOURCES) -> None:
    print(f"Pareto Table for: {table.target_skill}\n")
    occupational_tasks_rated = bool(table.unserved_occupational_tasks) or any(
        entry.occupational_tasks for entry in table.entries
    )
    for rank, entry in enumerate(table.entries, start=1):
        print(f"{rank:>2}. {entry.skill}  (demand weight {entry.demand_weight:.2f}; sources: {', '.join(entry.sources)})")
        # The model chose this Occupational Task, or none; the Source rated it.
        # Either choice moves the weight, so it is shown to be checked.
        if entry.occupational_tasks:
            strongest, others = entry.occupational_tasks[0], len(entry.occupational_tasks) - 1
            more = f" (+{others} more)" if others else ""
            print(f"      via {_with_rating(strongest)}{more}")
        elif occupational_tasks_rated:
            print("      serves none of the rated Occupational Tasks")

    # ADR-0002 lets the Analyst treat an unusable extraction as "no skills", and
    # a citation with an unreadable demand as no citation, only because both
    # show up in the table. They show up here.
    print("\nEntries credited per source:")
    for source in sources:
        cited = sum(1 for entry in table.entries if source.name in entry.sources)
        left_out = sum(1 for name, _ in table.unreadable_citations if name == source.name)
        notes = (["contributed nothing"] if not cited else []) + (
            [f"{left_out} reported without a readable demand, left out"] if left_out else []
        )
        suffix = f"   <- {'; '.join(notes)}" if notes else ""
        print(f"  {cited:>3}  {source.name}{suffix}")

    if table.unserved_occupational_tasks:
        print("\nOccupational Tasks no skill here serves (parts of the job this table doesn't reach):")
        for task in table.unserved_occupational_tasks:
            print(f"  {_with_rating(task)}")

    # The signal the top-20% cut actually ranks on. Entries tied on it fall back
    # to a Rank's order, then alphabetical; every entry no Rating reported demand
    # for ties at 0, so a cut that reaches them is ordered that way from there on.
    weights = [entry.demand_weight for entry in table.entries]
    unweighed = weights.count(0.0)
    print(
        f"\nDemand Weight spread: {len(set(weights))} distinct values across {len(weights)} entries; "
        f"{unweighed} at 0 (no Rating reported demand for them; a Rank, then name, orders them)"
    )
    if len(set(weights) - {0.0}) <= 1:
        print("  (no spread — the cut cannot be demand-driven)")


def _with_rating(task: OccupationalTask) -> str:
    rating = f"({task.demand:g}) " if task.demand is not None else ""
    return f"{rating}{task.description}"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        _print_table(run_analyst("smart contract development"))
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
