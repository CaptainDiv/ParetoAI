"""Curriculum agent: cuts a Pareto Table into an ordered Curriculum of Modules.

The LLM is asked only for the judgements it is actually good at — which skills
are hard prerequisites of which, the 1-2 best Resources for a topic, and which
Module a cut entry belongs under. The rules that must hold no matter what the
model says are pure functions, TDD'd here:

  select_module_topics the top ~20% cut, prerequisite closure, prerequisite order
  assemble_curriculum  Modules with 1-2 Resources, Deferred Resources, Gap carry-over

run_curriculum stitches them together, so the pipeline is
table -> prerequisites -> topic selection -> Resources/deferrals -> Curriculum.
Resources are planned *after* selection because prerequisite closure is what
decides the final topic set.
"""

from __future__ import annotations

import math
import sys
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypeVar

import neatlogs

from pareto_ai.analyst import ParetoEntry, ParetoTable, run_analyst
from pareto_ai.llm import complete, json_object

if TYPE_CHECKING:
    # proof_of_skill imports this module at runtime, so importing it back would
    # be a cycle. The annotation is all that's needed here, and
    # `from __future__ import annotations` keeps it from being evaluated.
    from pareto_ai.proof_of_skill import ProofOfSkillTask

MAX_RESOURCES_PER_MODULE = 2
TOP_FRACTION = 0.2
# A Round's Gaps may name any Pareto Table entry, so without a bound one weak
# Attempt could add a Module per Gap. Every Module added is another Attempt per
# Learner Profile in the next Round (minutes each, against a shared budget), so
# a revision takes on only the entries the Gaps blame most.
MAX_TOPICS_ADDED_BY_GAPS = 2
PLANNING_MAX_TOKENS = 8192

PlanT = TypeVar("PlanT", bound=Mapping)


@dataclass(frozen=True)
class Resource:
    title: str
    url: str | None = None  # None when the Resource is a citing source with no direct link


@dataclass(frozen=True)
class Gap:
    """A weakness the Judge agent found, mapped to the skill whose teaching would have prevented it.

    module_topic is a Module of the Curriculum the Attempt ran against, or a Pareto
    Table entry that Curriculum cut (the next Curriculum then adds it). It is None
    when the Judge found no entry in the table covers it.
    """

    description: str
    module_topic: str | None = None


@dataclass(frozen=True)
class Module:
    """One unit of the Curriculum: a topic, 1-2 Resources, one Proof-of-Skill Task.

    A Module is the whole of what a Student Agent is given, so what it can reach
    is load-bearing: a ProofOfSkillTask is the issue alone, never the merged fix
    that answers it. Ground Truth lives apart, in the TaskAssignment the
    Proof-of-Skill sourcing returns, and nothing here can reach it.
    """

    topic: str
    resources: tuple[Resource, ...]  # 1-2, best first
    prerequisites: tuple[str, ...]  # topics of Modules taught earlier in this Curriculum
    deferred_resources: tuple[ParetoEntry, ...]  # cut entries kept as "going deeper" reading
    # Prerequisites this Curriculum teaches only *after* this Module. Empty unless
    # the model's plan contained a cycle that _in_prerequisite_order had to break;
    # reported rather than hidden, because a learner hitting this Module really is
    # missing something and silence would look like a Module with no prerequisites.
    unmet_prerequisites: tuple[str, ...] = ()
    proof_of_skill_task: ProofOfSkillTask | None = None  # attached by with_proof_of_skill_tasks


@dataclass(frozen=True)
class Curriculum:
    target_skill: str
    modules: tuple[Module, ...]  # in prerequisite order
    uncovered_gaps: tuple[Gap, ...] = ()  # prior Gaps no Module in this Curriculum covers


# --- pure core -------------------------------------------------------------


def _hard_prerequisites(
    skill: str, prerequisites: Mapping[str, Sequence[str]], known_skills: frozenset[str]
) -> list[str]:
    """Prerequisites the model named for `skill`, minus hallucinations and self-edges.

    The prerequisite-closure rule is about Pareto Table entries, so a prerequisite
    the table doesn't contain can't become a Module and is dropped.
    """
    return [
        prereq for prereq in prerequisites.get(skill, ()) if prereq in known_skills and prereq != skill
    ]


def select_module_topics(
    table: ParetoTable,
    prerequisites: Mapping[str, Sequence[str]],
    *,
    prior_gaps: Sequence[Gap] = (),
    keep: Sequence[str] = (),
) -> tuple[str, ...]:
    """The topic of each Module the Curriculum will have, in prerequisite order.

    Starts from the top TOP_FRACTION of the (already Demand-Weight-ranked) table
    plus the `keep` topics (the last Curriculum's Modules: a Module a Gap added is
    no longer blamed once it is taught, so dropping it would bring the Gap back),
    then adds the entries outside that the prior Gaps blame most (at most
    MAX_TOPICS_ADDED_BY_GAPS, ties in table order), and closes transitively over
    hard prerequisites — Demand Weight decides emphasis, prerequisites decide
    inclusion. This is how a Round's Gaps grow the next Curriculum. A topic
    already in through closure is not "added", so blaming it uses no allowance.
    """
    if not table.entries:
        return ()

    known_skills = frozenset(entry.skill for entry in table.entries)
    rank_of = {entry.skill: rank for rank, entry in enumerate(table.entries)}

    cut = max(1, math.ceil(TOP_FRACTION * len(table.entries)))
    seeds = [entry.skill for entry in table.entries[:cut]]
    seeds += [topic for topic in keep if topic in known_skills]
    already_in = _closed_over_prerequisites(seeds, prerequisites, known_skills)

    blamed = Counter(
        gap.module_topic for gap in prior_gaps if gap.module_topic in known_skills and gap.module_topic not in already_in
    )
    seeds += sorted(blamed, key=lambda topic: (-blamed[topic], rank_of[topic]))[:MAX_TOPICS_ADDED_BY_GAPS]

    return _in_prerequisite_order(
        _closed_over_prerequisites(seeds, prerequisites, known_skills), prerequisites, rank_of
    )


def _closed_over_prerequisites(
    seeds: Sequence[str], prerequisites: Mapping[str, Sequence[str]], known_skills: frozenset[str]
) -> set[str]:
    selected: set[str] = set()
    pending = deque(seeds)
    while pending:
        skill = pending.popleft()
        if skill in selected:
            continue
        selected.add(skill)
        pending.extend(_hard_prerequisites(skill, prerequisites, known_skills))
    return selected


def _in_prerequisite_order(
    topics: set[str], prerequisites: Mapping[str, Sequence[str]], rank_of: Mapping[str, int]
) -> tuple[str, ...]:
    known_skills = frozenset(topics)
    blocked_by = {
        topic: set(_hard_prerequisites(topic, prerequisites, known_skills)) for topic in topics
    }

    def by_rank(topic: str) -> tuple[int, str]:
        return (rank_of.get(topic, len(rank_of)), topic)

    ordered: list[str] = []
    placed: set[str] = set()
    remaining = set(topics)
    while remaining:
        ready = [topic for topic in remaining if blocked_by[topic] <= placed]
        # No ready topic means the model proposed a prerequisite cycle. Break it at
        # the best-ranked topic left rather than dropping topics from the Curriculum.
        ordered.append(min(ready or remaining, key=by_rank))
        placed.add(ordered[-1])
        remaining.discard(ordered[-1])
    return tuple(ordered)


def assemble_curriculum(
    table: ParetoTable,
    topics: Sequence[str],
    prerequisites: Mapping[str, Sequence[str]],
    resources: Mapping[str, Sequence[Resource]],
    deferrals: Mapping[str, str],
    *,
    prior_gaps: Sequence[Gap] = (),
) -> Curriculum:
    """Build the Curriculum from selected topics plus the model's content plan.

    Every table entry that isn't a topic is linked from a Module as a Deferred
    Resource — `deferrals` maps it to the Module the model found most relevant,
    and anything unmapped falls to the first Module so the 80% cut is a
    deprioritization, never an information loss.
    """
    if not topics:
        return Curriculum(target_skill=table.target_skill, modules=(), uncovered_gaps=tuple(prior_gaps))

    entry_of = {entry.skill: entry for entry in table.entries}
    deferred_by_topic: dict[str, list[ParetoEntry]] = {topic: [] for topic in topics}
    for entry in table.entries:
        if entry.skill in deferred_by_topic:
            continue
        target = deferrals.get(entry.skill)
        deferred_by_topic[target if target in deferred_by_topic else topics[0]].append(entry)

    modules: list[Module] = []
    for position, topic in enumerate(topics):
        required = set(prerequisites.get(topic, ()))
        modules.append(
            Module(
                topic=topic,
                resources=_module_resources(topic, resources, entry_of.get(topic)),
                prerequisites=tuple(prereq for prereq in topics[:position] if prereq in required),
                deferred_resources=tuple(deferred_by_topic[topic]),
                unmet_prerequisites=tuple(
                    prereq for prereq in topics[position + 1 :] if prereq in required
                ),
            )
        )

    taught = set(topics)
    uncovered = tuple(gap for gap in prior_gaps if gap.module_topic not in taught)
    return Curriculum(target_skill=table.target_skill, modules=tuple(modules), uncovered_gaps=uncovered)


def with_proof_of_skill_tasks(
    curriculum: Curriculum, tasks: Mapping[str, ProofOfSkillTask]
) -> Curriculum:
    """A copy of the Curriculum whose Modules carry the tasks assigned to them.

    Takes only the Student-Agent-visible half of a TaskAssignment. The Ground
    Truth half is deliberately not a parameter here, so a Curriculum cannot be
    built that carries it.
    """
    return replace(
        curriculum,
        modules=tuple(
            replace(module, proof_of_skill_task=tasks.get(module.topic)) for module in curriculum.modules
        ),
    )


def _module_resources(
    topic: str, resources: Mapping[str, Sequence[Resource]], entry: ParetoEntry | None
) -> tuple[Resource, ...]:
    planned = tuple(resources.get(topic, ()))[:MAX_RESOURCES_PER_MODULE]
    if planned:
        return planned
    # A Module always ships with something to read; fall back to whichever source
    # cited the skill in the first place.
    fallback = entry.sources[0] if entry and entry.sources else topic
    return (Resource(title=fallback),)


# --- live LLM planning -----------------------------------------------------


class CurriculumPlanningError(RuntimeError):
    """A planning response couldn't be used, so the Curriculum can't be trusted.

    Degrading quietly here is worse than failing: an empty prerequisite plan
    reads as "nothing has prerequisites" and silently switches off the
    prerequisite-closure rule, which CONTEXT.md calls non-negotiable.
    """


def _plan_object(content: str, finish_reason: str | None) -> dict:
    if finish_reason == "length":
        raise CurriculumPlanningError(
            f"planning response hit the {PLANNING_MAX_TOKENS}-token budget before it finished"
        )
    parsed = json_object(content)
    if parsed is None:
        raise CurriculumPlanningError(
            f"planning response was not a JSON object (finish_reason={finish_reason}, "
            f"{len(content)} chars): {content[:300]!r}"
        )
    return parsed


def parse_prerequisites(content: str, finish_reason: str | None = None) -> dict[str, tuple[str, ...]]:
    """Read the prerequisite plan, refusing to quietly lose an edge.

    A stated prerequisite dropped for arriving in the wrong JSON shape reads
    downstream as "this skill has no prerequisites" — the silent closure failure
    this module exists to prevent. So a lone string is read as the one
    prerequisite it plainly means, and any other shape is refused outright.
    """
    plan: dict[str, tuple[str, ...]] = {}
    for skill, prereqs in _plan_object(content, finish_reason).items():
        listed = [prereqs] if isinstance(prereqs, str) else prereqs
        if not isinstance(listed, list):
            raise CurriculumPlanningError(
                f"prerequisites for {skill!r} were neither a list nor a string: {prereqs!r}"
            )
        plan[str(skill)] = tuple(str(prereq) for prereq in listed)
    return plan


def parse_resources(content: str, finish_reason: str | None = None) -> dict[str, tuple[Resource, ...]]:
    planned: dict[str, tuple[Resource, ...]] = {}
    for topic, items in _plan_object(content, finish_reason).items():
        if not isinstance(items, list):
            continue
        planned[str(topic)] = tuple(
            Resource(title=str(item["title"]), url=str(item["url"]) if item.get("url") else None)
            for item in items
            if isinstance(item, dict) and item.get("title")
        )
    return planned


def parse_deferrals(content: str, finish_reason: str | None = None) -> dict[str, str]:
    return {
        str(skill): str(topic)
        for skill, topic in _plan_object(content, finish_reason).items()
        if isinstance(topic, str)
    }


def _ask(prompt: str) -> tuple[str, str | None]:
    """Run one planning call, returning its content and finish reason."""
    return complete(prompt, max_tokens=PLANNING_MAX_TOKENS)


def _planned(
    prompt: str,
    parse: Callable[[str, str | None], PlanT],
    ask: Callable[[str], tuple[str, str | None]] = _ask,
) -> PlanT:
    """One planning call, retried once.

    MODEL intermittently emits malformed JSON on the larger plans, and a plan is
    cheap to re-ask for. If the second attempt is unusable too, the error stands.
    `ask` is injected so the retry is testable without reaching the model.
    """
    try:
        return parse(*ask(prompt))
    except CurriculumPlanningError:
        return parse(*ask(prompt))


def _bulleted(skills: Sequence[str]) -> str:
    return "\n".join(f"- {skill}" for skill in skills)


@neatlogs.span(kind="AGENT", role="Curriculum")
def plan_prerequisites(target_skill: str, skills: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Ask which skills are *hard* prerequisites of which, over the whole table."""
    return _planned(
        f'You are sequencing a learning curriculum for the target skill "{target_skill}".\n\n'
        "SKILLS:\n"
        f"{_bulleted(skills)}\n\n"
        "For each skill, list the skills from the list above that are HARD prerequisites: "
        "a learner genuinely cannot learn this skill without already knowing them. "
        "Be strict — most skills have zero or one hard prerequisite. "
        "Never use a skill that is not in the list, never list a skill as its own "
        "prerequisite, and never create a cycle.\n"
        'Respond with ONLY a JSON object of the form {"skill": ["prerequisite", ...]}, '
        "omitting skills with no hard prerequisites. No prose.",
        parse_prerequisites,
    )


def _resources_prompt(target_skill: str, topics: Sequence[str]) -> str:
    return (
        f'You are choosing learning Resources for a curriculum on "{target_skill}".\n\n'
        "TOPICS:\n"
        f"{_bulleted(topics)}\n\n"
        f"For each topic pick the 1 or {MAX_RESOURCES_PER_MODULE} best freely-available resources "
        "(official docs, a canonical guide, a well-known tutorial) — one is better than two if "
        "only one is genuinely good, and never more than two. Only give resources you are "
        "confident exist, with their real URL.\n"
        "Cover EVERY topic listed above.\n"
        'Respond with ONLY a JSON object of the form {"topic": [{"title": "...", "url": "..."}]}. '
        "No prose."
    )


@neatlogs.span(kind="AGENT", role="Curriculum")
def plan_resources(target_skill: str, topics: Sequence[str]) -> dict[str, tuple[Resource, ...]]:
    """Ask for the 1-2 best Resources for each Module topic.

    On a long topic list the model quietly skips some. A skipped topic leaves its
    Module falling back to a bare citing-source name with no link, so the topics
    it missed get asked for again on their own.
    """
    planned = _planned(_resources_prompt(target_skill, topics), parse_resources)
    missing = [topic for topic in topics if not planned.get(topic)]
    if missing:
        planned = {**planned, **_planned(_resources_prompt(target_skill, missing), parse_resources)}
    return planned


@neatlogs.span(kind="AGENT", role="Curriculum")
def plan_deferrals(target_skill: str, cut_skills: Sequence[str], topics: Sequence[str]) -> dict[str, str]:
    """Ask which Module each cut entry should hang off as a Deferred Resource."""
    if not cut_skills:
        return {}
    return _planned(
        f'A curriculum on "{target_skill}" was cut to these modules:\n'
        f"{_bulleted(topics)}\n\n"
        "These skills did not make the cut, but are kept as 'going deeper' reading "
        "linked from whichever module they extend:\n"
        f"{_bulleted(cut_skills)}\n\n"
        "Assign every skill in the second list to exactly one module from the first list — "
        "the module whose learner would most naturally read it next.\n"
        'Respond with ONLY a JSON object of the form {"cut skill": "module topic"}. No prose.',
        parse_deferrals,
    )


@neatlogs.span(kind="WORKFLOW")
def run_curriculum(table: ParetoTable, *, prior_gaps: Sequence[Gap] = (), keep: Sequence[str] = ()) -> Curriculum:
    skills = [entry.skill for entry in table.entries]
    prerequisites = plan_prerequisites(table.target_skill, skills)
    topics = select_module_topics(table, prerequisites, prior_gaps=prior_gaps, keep=keep)
    resources = plan_resources(table.target_skill, topics)
    cut_skills = [skill for skill in skills if skill not in set(topics)]
    deferrals = plan_deferrals(table.target_skill, cut_skills, topics)
    return assemble_curriculum(
        table, topics, prerequisites, resources, deferrals, prior_gaps=prior_gaps
    )


def _print_curriculum(curriculum: Curriculum) -> None:
    print(f"Curriculum for: {curriculum.target_skill}\n")
    for position, module in enumerate(curriculum.modules, start=1):
        print(f"{position}. {module.topic}")
        if module.prerequisites:
            print(f"   requires: {', '.join(module.prerequisites)}")
        if module.unmet_prerequisites:
            print(f"   UNMET REQUIREMENT (taught later): {', '.join(module.unmet_prerequisites)}")
        for resource in module.resources:
            print(f"   resource: {resource.title}{f' <{resource.url}>' if resource.url else ''}")
        for entry in module.deferred_resources:
            print(f"   deferred: {entry.skill} (demand weight {entry.demand_weight:.2f})")
        print()
    for gap in curriculum.uncovered_gaps:
        print(f"UNCOVERED GAP: {gap.description}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        _print_curriculum(run_curriculum(run_analyst("smart contract development")))
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
