"""Round orchestrator: the full loop, per-profile gating, and the Readiness Report.

Ticket #8, Seam 1 of the spec. A Round is one Curriculum put through real
Attempts: every Module's Proof-of-Skill Task is attempted by a Student Agent of
every Learner Profile, the Judge grades each, and the Gaps come back. Between
Rounds the Curriculum agent revises the Curriculum from those Gaps; the loop
stops when every Learner Profile clears the pass-rate threshold on its own, or
after MAX_ROUNDS, whichever comes first.

Everything that touches a model, a repo or a CLI is passed in (`plan`, `assign`,
`student`, `judge`), so the rules that must hold regardless of what they return
are testable without any of them:

  - a Learner Profile's pass rate is its own: a path that works for one profile
    and not the other must not stop the loop, so nothing here averages them;
  - a Round that attempted nothing for a profile refuses rather than reading as
    a perfect score (ADR-0002);
  - the cap ends in a Readiness Report that names the unresolved Gaps, never in
    an unfinished Curriculum presented as done;
  - the last Round's Gaps are reported, not revised into a Curriculum nobody
    tested, so the loop makes one `plan` call per Round and no more.

A Gap can name any Pareto Table entry, not only a Module the Curriculum already
has, which is what lets a Round grow the next Curriculum (ADR-0009).

A topic keeps its Proof-of-Skill Task from Round to Round (`assign_tasks`):
otherwise a pass rate would move because the exam changed, and every new task
would cost a warm-base build (about 9 minutes) for nothing.

The live harness at the bottom is smoke-tested only, like the other agents'
`main()`s: a full run is 2 Learner Profiles x every Module x up to 3 Rounds of
real OpenCode sessions.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

import neatlogs

from pareto_ai.analyst import ParetoTable, run_analyst
from pareto_ai.curriculum import Curriculum, Gap, Module, run_curriculum, with_proof_of_skill_tasks
from pareto_ai.judge import JudgeResult, run_judge
from pareto_ai.proof_of_skill import (
    GroundTruth,
    ProofOfSkillTask,
    TaskAssignment,
    assign_proof_of_skill_tasks,
    fetch_task_candidates,
    pair_tasks_to_modules,
)
from pareto_ai.retrieval import retrieve
from pareto_ai.student_agent import (
    MVP_LEARNER_PROFILES,
    RETRIEVAL_K,
    AttemptResult,
    LearnerProfile,
    prepare_warm_base,
    run_attempt,
)
from pareto_ai.warm_base import WARM_CACHE_DIR, discard_attempt_workspace

PASS_RATE_THRESHOLD = 0.7
MAX_ROUNDS = 3

# The injected agents. `Plan` is the Curriculum agent, revising from the last
# Round's Gaps and told which topics that Round's Curriculum already taught; `Assign` attaches Proof-of-Skill Tasks; `Student` runs one
# Attempt (it is given a Module, never a Ground Truth); `Judge` grades one, told
# which skills the Curriculum teaches and which it cut.
Plan = Callable[[ParetoTable, Sequence[Gap], Sequence[str]], Curriculum]
Assign = Callable[[Curriculum], Curriculum]
Student = Callable[[Module, LearnerProfile, Curriculum], AttemptResult]
Judge = Callable[[Module, AttemptResult, Sequence[str], Sequence[str]], JudgeResult]


class RoundError(RuntimeError):
    """A Round could not produce a pass rate it can stand behind."""


@dataclass(frozen=True)
class ProfileScore:
    """One Learner Profile's results across a Round's Modules."""

    profile: LearnerProfile
    passed: int
    attempted: int

    @property
    def rate(self) -> float:
        return self.passed / self.attempted

    @property
    def clears_threshold(self) -> bool:
        return self.rate >= PASS_RATE_THRESHOLD


@dataclass(frozen=True)
class RoundResult:
    number: int
    curriculum: Curriculum
    prior_gaps: tuple[Gap, ...]  # the Gaps this Curriculum was revised from
    judged: tuple[JudgeResult, ...]
    untested_topics: tuple[str, ...]  # Modules with no Proof-of-Skill Task, so never attempted
    scores: tuple[ProfileScore, ...]

    @property
    def gaps(self) -> tuple[Gap, ...]:
        return tuple(gap for result in self.judged for gap in result.gaps)

    @property
    def converged(self) -> bool:
        # A Module nobody could attempt is unproven, and a pass rate over the rest
        # would present a partly tested Curriculum as done.
        return not self.untested_topics and all(score.clears_threshold for score in self.scores)


@dataclass(frozen=True)
class ReadinessReport:
    """The honest end of the loop (CONTEXT.md): each profile's final pass rate, and
    — when the cap was hit first — the Gaps nothing resolved."""

    converged: bool
    rounds_run: int
    final_scores: tuple[ProfileScore, ...]
    unresolved_gaps: tuple[Gap, ...]  # empty when converged
    untested_topics: tuple[str, ...]  # Modules the final Round could not attempt


@dataclass(frozen=True)
class LoopResult:
    rounds: tuple[RoundResult, ...]
    report: ReadinessReport


# --- pure core ---------------------------------------------------------------


def score_profiles(profiles: Sequence[LearnerProfile], judged: Sequence[JudgeResult]) -> tuple[ProfileScore, ...]:
    scores = []
    for profile in profiles:
        own = [result for result in judged if result.profile == profile]
        if not own:
            raise RoundError(
                f"no Attempt was graded for {profile.name!r}: a pass rate over nothing would read as "
                "a perfect score, so the Round refuses instead (ADR-0002)"
            )
        scores.append(ProfileScore(profile=profile, passed=sum(result.passed for result in own), attempted=len(own)))
    return tuple(scores)


def curriculum_changes(before: Curriculum, after: Curriculum) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(Modules `after` added, Modules it dropped): what a Round's Gaps visibly changed."""
    previous = {module.topic for module in before.modules}
    current = {module.topic for module in after.modules}
    return (
        tuple(module.topic for module in after.modules if module.topic not in previous),
        tuple(module.topic for module in before.modules if module.topic not in current),
    )


def readiness_report(rounds: Sequence[RoundResult]) -> ReadinessReport:
    final = rounds[-1]
    return ReadinessReport(
        converged=final.converged,
        rounds_run=len(rounds),
        final_scores=final.scores,
        unresolved_gaps=() if final.converged else final.gaps,
        untested_topics=final.untested_topics,
    )


def assign_tasks(
    curriculum: Curriculum,
    held: TaskAssignment,
    source: Callable[[Sequence[str], frozenset[str]], TaskAssignment],
) -> tuple[Curriculum, TaskAssignment]:
    """Attach a task to every Module: the one a topic already had, else a newly sourced one.

    `source` is given only the topics that need one and the keys of tasks already
    in use, so a new Module can never be proven by an issue another Module has.
    Returns the Curriculum with its tasks attached and the assignment now held,
    which keeps only Ground Truths for tasks the Curriculum still uses.
    """
    topics = [module.topic for module in curriculum.modules]
    kept = {topic: held.tasks[topic] for topic in topics if topic in held.tasks}
    new_topics = [topic for topic in topics if topic not in kept]
    sourced = source(new_topics, frozenset(task.key for task in kept.values())) if new_topics else TaskAssignment()

    tasks = {**kept, **sourced.tasks}
    used = {task.key for task in tasks.values()}
    now_held = TaskAssignment(
        tasks=tasks,
        ground_truths={key: truth for key, truth in {**held.ground_truths, **sourced.ground_truths}.items() if key in used},
        topics_without_tasks=sourced.topics_without_tasks,
        unpaired_topics=tuple(topic for topic in held.unpaired_topics if topic in kept) + sourced.unpaired_topics,
    )
    return with_proof_of_skill_tasks(curriculum, tasks), now_held


# --- orchestration -----------------------------------------------------------


def run_round(
    table: ParetoTable,
    curriculum: Curriculum,
    prior_gaps: Sequence[Gap],
    *,
    student: Student,
    judge: Judge,
    profiles: Sequence[LearnerProfile] = MVP_LEARNER_PROFILES,
    number: int = 1,
) -> RoundResult:
    """Attempt every tasked Module with every Learner Profile and grade each Attempt.

    `curriculum` is the one under test and `prior_gaps` the Gaps it was revised
    from, kept on the result so a Round shows what fed it. Each Round is its own
    WORKFLOW span, so the Attempts and gradings under it read per Round in neatlogs.
    """
    return neatlogs.span(kind="WORKFLOW", name=f"round_{number}", capture_input=False)(_run_round)(
        table, curriculum, tuple(prior_gaps), student, judge, tuple(profiles), number
    )


def _run_round(
    table: ParetoTable,
    curriculum: Curriculum,
    prior_gaps: tuple[Gap, ...],
    student: Student,
    judge: Judge,
    profiles: tuple[LearnerProfile, ...],
    number: int,
) -> RoundResult:
    known_topics = tuple(module.topic for module in curriculum.modules)
    cut_topics = tuple(entry.skill for entry in table.entries if entry.skill not in known_topics)
    tested = [module for module in curriculum.modules if module.proof_of_skill_task is not None]

    judged = tuple(
        judge(module, student(module, profile, curriculum), known_topics, cut_topics)
        for module in tested
        for profile in profiles
    )
    return RoundResult(
        number=number,
        curriculum=curriculum,
        prior_gaps=prior_gaps,
        judged=judged,
        untested_topics=tuple(module.topic for module in curriculum.modules if module.proof_of_skill_task is None),
        scores=score_profiles(profiles, judged),
    )


def run_rounds(
    table: ParetoTable,
    *,
    plan: Plan,
    assign: Assign,
    student: Student,
    judge: Judge,
    profiles: Sequence[LearnerProfile] = MVP_LEARNER_PROFILES,
    max_rounds: int = MAX_ROUNDS,
) -> LoopResult:
    """Round after Round until every profile clears the bar or `max_rounds` is spent.

    The Curriculum agent revises between Rounds, from the Gaps of the Round just
    graded and keeping the Modules it taught (so what a Gap added stays); it is
    never called after the last one.
    """
    rounds: list[RoundResult] = []
    gaps: tuple[Gap, ...] = ()
    taught: tuple[str, ...] = ()
    while True:
        curriculum = assign(plan(table, gaps, taught))
        rounds.append(run_round(table, curriculum, gaps, student=student, judge=judge, profiles=profiles, number=len(rounds) + 1))
        if rounds[-1].converged or len(rounds) >= max_rounds:
            return LoopResult(rounds=tuple(rounds), report=readiness_report(rounds))
        gaps = rounds[-1].gaps
        taught = tuple(module.topic for module in curriculum.modules)


# --- the live harness --------------------------------------------------------


@neatlogs.span(kind="WORKFLOW", name="proof_of_skill_sourcing", capture_input=False)
def source_new_tasks(target_skill: str, topics: Sequence[str], taken: Collection[str]) -> TaskAssignment:
    """Real closed issues for `topics`, never one already assigned (`taken` holds task keys)."""
    candidates = tuple(c for c in fetch_task_candidates() if c.task.key not in taken)
    return assign_proof_of_skill_tasks(topics, candidates, pair_tasks_to_modules(target_skill, topics, candidates))


class LiveHarness:
    """The real Student Agent, Judge and task sourcing, sharing the Ground Truths they need.

    The Ground Truths live here, apart from the Curriculum: `student` looks one up
    only to build the warm base (the repo at the pre-fix commit), and what it
    hands `run_attempt` is a Module, which cannot reach one.
    """

    def __init__(self, target_skill: str, workspace_root: Path, cache_dir: Path = WARM_CACHE_DIR):
        self._target_skill = target_skill
        self._workspace_root = workspace_root
        self._cache_dir = cache_dir
        self._held = TaskAssignment()

    def _task(self, module: Module) -> ProofOfSkillTask:
        if module.proof_of_skill_task is None:  # run_round only hands over tasked Modules
            raise RoundError(f"Module {module.topic!r} reached the harness without a Proof-of-Skill Task")
        return module.proof_of_skill_task

    def _ground_truth(self, module: Module) -> GroundTruth:
        return self._held.ground_truths[self._task(module).key]

    def assign(self, curriculum: Curriculum) -> Curriculum:
        revised, self._held = assign_tasks(
            curriculum, self._held, lambda topics, taken: source_new_tasks(self._target_skill, topics, taken)
        )
        for topic in self._held.topics_without_tasks:
            print(f"NO TASK for Module {topic!r}: no unclaimed candidate was left", flush=True)
        for topic in self._held.unpaired_topics:
            print(f"Module {topic!r} was given a task only to fill the slot (NOT MATCHED)", flush=True)
        return revised

    def student(self, module: Module, profile: LearnerProfile, curriculum: Curriculum) -> AttemptResult:
        print(f"\n--- Attempt: {module.topic} / {profile.name} ---", flush=True)
        base = prepare_warm_base(self._ground_truth(module), self._cache_dir)
        chunks = retrieve(curriculum, self._task(module).title, RETRIEVAL_K)
        attempt = run_attempt(module, profile, chunks, base, workspace_root=self._workspace_root)
        print(f"opencode exit {attempt.opencode_exit_code}, committed {attempt.commit_sha or '(no commit)'}", flush=True)
        return attempt

    def judge(
        self, module: Module, attempt: AttemptResult, known_topics: Sequence[str], cut_topics: Sequence[str]
    ) -> JudgeResult:
        try:
            result = run_judge(module, self._ground_truth(module), attempt, known_topics, cut_topics)
        finally:
            discard_attempt_workspace(attempt.workspace)
        print(f"judged: passed={result.passed}, {len(result.gaps)} gaps", flush=True)
        return result


def _print_loop(result: LoopResult) -> None:
    previous: Curriculum | None = None
    for round_result in result.rounds:
        print(f"\n=== Round {round_result.number} ===")
        print("Modules: " + ", ".join(module.topic for module in round_result.curriculum.modules))
        if previous is not None:
            added, dropped = curriculum_changes(previous, round_result.curriculum)
            print(f"changed by the last Round's Gaps: added {list(added) or 'nothing'}, dropped {list(dropped) or 'nothing'}")
        for score in round_result.scores:
            verdict = "clears" if score.clears_threshold else "BELOW"
            print(f"{score.profile.name}: {score.passed}/{score.attempted} = {score.rate:.0%} ({verdict} {PASS_RATE_THRESHOLD:.0%})")
        if round_result.untested_topics:
            print(f"NOT ATTEMPTED (no task): {', '.join(round_result.untested_topics)}")
        previous = round_result.curriculum

    report = result.report
    print("\n=== Readiness Report ===")
    print(f"rounds run: {report.rounds_run} of {MAX_ROUNDS}; " + ("CONVERGED" if report.converged else "DID NOT CONVERGE"))
    for score in report.final_scores:
        print(f"{score.profile.name}: {score.rate:.0%} ({score.passed}/{score.attempted})")
    for gap in report.unresolved_gaps:
        print(f"unresolved gap: {gap.description}  -> {gap.module_topic or '(no Pareto Table entry covers it)'}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        table = run_analyst("smart contract development")
        with tempfile.TemporaryDirectory(prefix="pareto-rounds-") as workspace_root:
            harness = LiveHarness(table.target_skill, Path(workspace_root))
            result = run_rounds(
                table,
                plan=lambda table, gaps, keep: run_curriculum(table, prior_gaps=gaps, keep=keep),
                assign=harness.assign,
                student=harness.student,
                judge=harness.judge,
            )
        _print_loop(result)
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
