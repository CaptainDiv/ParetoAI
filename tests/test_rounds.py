import zlib
from pathlib import Path

import pytest

from pareto_ai.analyst import ParetoEntry, ParetoTable
from pareto_ai.curriculum import Curriculum, Gap, Module, Resource
from pareto_ai.judge import JudgeResult
from pareto_ai.proof_of_skill import GroundTruth, ProofOfSkillTask, TaskAssignment
from pareto_ai.rounds import (
    PASS_RATE_THRESHOLD,
    ProfileScore,
    RoundError,
    assign_tasks,
    curriculum_changes,
    run_round,
    run_rounds,
)
from pareto_ai.student_agent import SOME_IT_BACKGROUND, TOTAL_BEGINNER, AttemptResult

BEGINNER, IT = TOTAL_BEGINNER, SOME_IT_BACKGROUND
PROFILES = (BEGINNER, IT)


def task_for(topic: str) -> ProofOfSkillTask:
    return ProofOfSkillTask(repo="o/r", issue_number=zlib.crc32(topic.encode()), title=f"fix {topic}", body="", url="u")


def module(topic: str, *, tasked: bool = True) -> Module:
    return Module(
        topic=topic,
        resources=(Resource(title="docs"),),
        prerequisites=(),
        deferred_resources=(),
        proof_of_skill_task=task_for(topic) if tasked else None,
    )


def curriculum(*topics: str, untasked: tuple[str, ...] = ()) -> Curriculum:
    return Curriculum(
        target_skill="smart contract development",
        modules=tuple(module(topic, tasked=topic not in untasked) for topic in topics),
    )


def pareto_table(*skills: str) -> ParetoTable:
    return ParetoTable(
        target_skill="smart contract development",
        entries=tuple(ParetoEntry(skill=skill, demand_weight=1.0, sources=("s",)) for skill in skills),
    )


TABLE = pareto_table("a", "b", "c", "d", "e", "f")


class FakeStudent:
    """Records who attempted what; commits for everyone."""

    def __init__(self):
        self.calls = []

    def __call__(self, module, profile, curriculum):
        self.calls.append((module.topic, profile.name))
        return AttemptResult(
            profile=profile,
            module_topic=module.topic,
            workspace=Path("workspace"),
            opencode_exit_code=0,
            commit_sha="abc",
            checkpoint_verified=True,
        )


class FakeJudge:
    """Passes an Attempt unless `failing` lists its (profile, topic); a fail blames `blames`."""

    def __init__(self, failing=(), blames=None):
        self.failing = set(failing)
        self.blames = blames or {}
        self.calls = []

    def __call__(self, module, attempt, known_topics, cut_topics):
        self.calls.append((module.topic, attempt.profile.name, tuple(known_topics), tuple(cut_topics)))
        failed = (attempt.profile.name, module.topic) in self.failing
        gaps = (Gap(description=f"weak on {module.topic}", module_topic=self.blames.get(module.topic, module.topic)),) if failed else ()
        return JudgeResult(profile=attempt.profile, module_topic=module.topic, passed=not failed, gaps=gaps)


def run(curriculum_, judge, *, prior_gaps=(), table=TABLE, student=None):
    return run_round(table, curriculum_, prior_gaps, student=student or FakeStudent(), judge=judge)


# --- ProfileScore ------------------------------------------------------------


def test_exactly_seventy_percent_clears_the_threshold():
    assert PASS_RATE_THRESHOLD == 0.7
    assert ProfileScore(BEGINNER, passed=7, attempted=10).clears_threshold


def test_just_under_seventy_percent_does_not():
    assert not ProfileScore(BEGINNER, passed=2, attempted=3).clears_threshold


# --- run_round: scoring and per-profile gating --------------------------------


def test_a_round_attempts_every_module_with_every_profile_and_scores_each_profile():
    student = FakeStudent()

    result = run(curriculum("a", "b"), FakeJudge(), student=student)

    assert sorted(student.calls) == sorted([(t, p.name) for t in ("a", "b") for p in PROFILES])
    assert [(s.profile, s.passed, s.attempted) for s in result.scores] == [(BEGINNER, 2, 2), (IT, 2, 2)]
    assert result.converged


def test_a_path_that_works_for_one_profile_but_not_the_other_does_not_converge():
    # Beginner passes 5 of 5; IT passes 1 of 5. Averaged that is 60%, but the point of two
    # profiles is that the average must not decide: IT alone is far below the bar.
    topics = ("a", "b", "c", "d", "e")
    judge = FakeJudge(failing={(IT.name, t) for t in topics[1:]})

    result = run(curriculum(*topics), judge)

    assert [s.clears_threshold for s in result.scores] == [True, False]
    assert not result.converged


def test_a_path_that_works_for_one_profile_fails_even_when_the_average_would_clear():
    # Beginner 5/5 and IT 3/5 average to 80%, yet IT is at 60%.
    topics = ("a", "b", "c", "d", "e")
    judge = FakeJudge(failing={(IT.name, "a"), (IT.name, "b")})

    result = run(curriculum(*topics), judge)

    assert not result.converged


def test_the_judge_is_told_which_skills_the_curriculum_cut():
    judge = FakeJudge()

    run(curriculum("a", "b"), judge)

    _, _, known, cut = judge.calls[0]
    assert known == ("a", "b")
    assert cut == ("c", "d", "e", "f")


def test_every_gap_from_every_attempt_comes_back_in_the_round_result():
    judge = FakeJudge(failing={(BEGINNER.name, "a"), (IT.name, "b")})

    result = run(curriculum("a", "b"), judge)

    assert {gap.module_topic for gap in result.gaps} == {"a", "b"}


def test_a_module_with_no_task_is_reported_and_not_attempted():
    student = FakeStudent()

    result = run(curriculum("a", "b", untasked=("b",)), FakeJudge(), student=student)

    assert {topic for topic, _ in student.calls} == {"a"}
    assert result.untested_topics == ("b",)


def test_a_curriculum_with_an_untestable_module_does_not_converge_on_the_rest_alone():
    # Everything attempted passed, but "b" was never proven: not a validated Curriculum.
    result = run(curriculum("a", "b", untasked=("b",)), FakeJudge())

    assert all(score.clears_threshold for score in result.scores)
    assert not result.converged


def test_a_round_with_nothing_to_attempt_refuses_instead_of_reading_as_a_perfect_score():
    with pytest.raises(RoundError):
        run(curriculum("a", untasked=("a",)), FakeJudge())


def test_the_round_remembers_the_gaps_that_shaped_its_curriculum():
    prior = (Gap(description="was weak", module_topic="b"),)

    assert run(curriculum("a"), FakeJudge(), prior_gaps=prior).prior_gaps == prior


# --- run_rounds: the loop ------------------------------------------------------


class Planner:
    """Stands in for the Curriculum agent, recording the Gaps each revision was asked to act on."""

    def __init__(self, *topics_by_round):
        self.topics_by_round = topics_by_round
        self.gaps_seen = []
        self.kept_seen = []

    def __call__(self, table, prior_gaps, keep):
        self.gaps_seen.append(tuple(prior_gaps))
        self.kept_seen.append(tuple(keep))
        return curriculum(*self.topics_by_round[min(len(self.gaps_seen), len(self.topics_by_round)) - 1])


def loop(planner, judge, *, max_rounds=3):
    return run_rounds(
        TABLE,
        plan=planner,
        assign=lambda curriculum_: curriculum_,
        student=FakeStudent(),
        judge=judge,
        max_rounds=max_rounds,
    )


def test_the_loop_stops_at_the_first_round_that_clears_the_bar_for_every_profile():
    planner = Planner(("a", "b"))

    result = loop(planner, FakeJudge())

    assert len(result.rounds) == 1
    assert result.report.converged
    assert len(planner.gaps_seen) == 1  # no revision after a Round that passed


def test_the_loop_gives_up_after_three_rounds_with_a_readiness_report_not_a_false_done():
    planner = Planner(("a", "b"))
    judge = FakeJudge(failing={(IT.name, "a"), (IT.name, "b")})

    result = loop(planner, judge)

    assert len(result.rounds) == 3
    assert not result.report.converged
    assert [(s.profile, s.passed, s.attempted) for s in result.report.final_scores] == [(BEGINNER, 2, 2), (IT, 0, 2)]
    assert {gap.module_topic for gap in result.report.unresolved_gaps} == {"a", "b"}
    assert len(planner.gaps_seen) == 3  # the last Round's Gaps are reported, not revised into an untested Curriculum


def test_the_report_of_a_loop_that_converged_lists_no_unresolved_gaps():
    # Round 1: IT fails "a" (1 of 2 = 50%). Round 2 passes everything.
    planner = Planner(("a", "b"), ("a", "b", "c"))

    def judge_that_improves(module, attempt, known, cut):
        in_round_two = len(planner.gaps_seen) >= 2
        return JudgeResult(
            profile=attempt.profile,
            module_topic=module.topic,
            passed=in_round_two or not (attempt.profile is IT and module.topic == "a"),
            gaps=() if in_round_two or not (attempt.profile is IT and module.topic == "a") else (Gap("weak", "c"),),
        )

    result = loop(planner, judge_that_improves)

    assert len(result.rounds) == 2
    assert result.report.converged
    assert result.report.unresolved_gaps == ()
    assert result.report.rounds_run == 2


def test_gaps_from_one_round_reach_the_curriculum_agent_for_the_next():
    planner = Planner(("a", "b"), ("a", "b", "c"), ("a", "b", "c"))
    judge = FakeJudge(failing={(IT.name, "a"), (IT.name, "b")}, blames={"a": "c", "b": "c"})

    result = loop(planner, judge)

    assert planner.gaps_seen[0] == ()
    assert {gap.module_topic for gap in planner.gaps_seen[1]} == {"c"}
    assert planner.gaps_seen[1] == result.rounds[0].gaps
    assert result.rounds[1].prior_gaps == result.rounds[0].gaps


def test_a_round_whose_gaps_added_a_module_shows_it_in_the_curriculum_changes():
    planner = Planner(("a", "b"), ("a", "b", "c"), ("a", "b", "c"))
    judge = FakeJudge(failing={(IT.name, "a"), (IT.name, "b")}, blames={"a": "c", "b": "c"})

    result = loop(planner, judge)

    assert curriculum_changes(result.rounds[0].curriculum, result.rounds[1].curriculum) == (("c",), ())
    assert curriculum_changes(result.rounds[1].curriculum, result.rounds[2].curriculum) == ((), ())


def test_the_curriculum_agent_is_told_which_modules_the_last_round_taught():
    planner = Planner(("a", "b"), ("a", "b", "c"), ("a", "b", "c"))
    judge = FakeJudge(failing={(IT.name, "a"), (IT.name, "b")}, blames={"a": "c", "b": "c"})

    loop(planner, judge)

    assert planner.kept_seen == [(), ("a", "b"), ("a", "b", "c")]


def test_each_round_is_attempted_against_the_curriculum_after_task_assignment():
    seen = []
    tasked = lambda curriculum_: seen.append(curriculum_) or curriculum_

    run_rounds(
        TABLE,
        plan=Planner(("a",)),
        assign=tasked,
        student=FakeStudent(),
        judge=FakeJudge(),
    )

    assert [m.topic for m in seen[0].modules] == ["a"]


# --- curriculum_changes ----------------------------------------------------------


def test_curriculum_changes_names_added_and_dropped_modules():
    assert curriculum_changes(curriculum("a", "b"), curriculum("b", "c")) == (("c",), ("a",))


# --- assign_tasks: a topic keeps its task across Rounds -----------------------------


def ground_truth(topic: str) -> GroundTruth:
    task = task_for(topic)
    return GroundTruth(
        repo=task.repo,
        issue_number=task.issue_number,
        pull_number=1,
        pull_title="fix",
        pull_body="",
        merge_commit_sha="m",
        pre_fix_commit_sha="p",
        url="u",
    )


def held_for(*topics: str) -> TaskAssignment:
    return TaskAssignment(
        tasks={topic: task_for(topic) for topic in topics},
        ground_truths={ground_truth(topic).key: ground_truth(topic) for topic in topics},
    )


def test_a_topic_that_had_a_task_keeps_it_and_only_new_topics_are_sourced():
    sourced_for = []

    def source(topics, taken):
        sourced_for.append((tuple(topics), frozenset(taken)))
        return held_for(*topics)

    revised, held = assign_tasks(
        Curriculum("s", modules=tuple(module(t, tasked=False) for t in ("a", "c"))), held_for("a", "b"), source
    )

    assert sourced_for == [(("c",), frozenset({task_for("a").key}))]
    assert {m.topic: m.proof_of_skill_task for m in revised.modules} == {"a": task_for("a"), "c": task_for("c")}
    assert set(held.tasks) == {"a", "c"}
    assert set(held.ground_truths) == {ground_truth(t).key for t in ("a", "c")}


def test_nothing_is_sourced_when_every_topic_already_has_a_task():
    def source(topics, taken):
        raise AssertionError("should not source")

    revised, _ = assign_tasks(Curriculum("s", modules=(module("a", tasked=False),)), held_for("a"), source)

    assert revised.modules[0].proof_of_skill_task == task_for("a")


def test_a_topic_the_sourcing_could_not_task_stays_reported_as_without_one():
    def source(topics, taken):
        return TaskAssignment(topics_without_tasks=tuple(topics))

    revised, held = assign_tasks(Curriculum("s", modules=(module("c", tasked=False),)), TaskAssignment(), source)

    assert revised.modules[0].proof_of_skill_task is None
    assert held.topics_without_tasks == ("c",)
