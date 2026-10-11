"""Judge agent: grades an Attempt against its Ground Truth, producing Gaps.

Ticket #7. Per CONTEXT.md's Judge Agent and Gap entries and #1's spec, a grade
has two independent signals:

  objective   the target repo's own test suite, run against the Attempt's
              resulting commit — "anchored in something real" rather than an
              LLM's self-report.
  qualitative an LLM-rubric comparison against the Ground Truth PR, producing
              Gaps mapped to a specific Module (or flagged, per CONTEXT.md, as
              not covered by any Module in this Curriculum).

A real fix PR adds its regression test in the same commit as the fix, so
running the suite unchanged against an Attempt that changed nothing would
still exercise only tests that already existed before the bug was found and
report a trivial pass. `apply_ground_truth_tests` checks out only the Ground
Truth's test files (never its non-test files, which would hand the Attempt the
fix itself) before the suite runs, so "passed" means the Attempt's own code
satisfies the real regression test.

Per ADR-0002, the rubric call is on the refusing side, not the degrading one:
an empty Gaps list already means something ("the Attempt matches the Ground
Truth"), so a response this module can't use raises `JudgeGradingError`
instead of quietly returning `()` and inflating a pass rate — the project's
central claim.

Pure core (`select_test_files`, `parse_gaps`, `_graded`) is TDD'd. Running the
real suite, computing real diffs, and the live rubric call are smoke-tested
only via main(), per the project's established pattern (see
proof_of_skill.fetch_task_candidates, student_agent's live orchestration):
these need the real repo, git history and LLM on hand, which unit tests cannot
stand in for without pretending to verify something they can't.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import neatlogs

from pareto_ai.analyst import run_analyst
from pareto_ai.curriculum import Gap, Module, run_curriculum, with_proof_of_skill_tasks
from pareto_ai.llm import complete, json_object
from pareto_ai.proof_of_skill import GroundTruth, run_proof_of_skill_sourcing
from pareto_ai.retrieval import retrieve
from pareto_ai.student_agent import AttemptResult, LearnerProfile, TOTAL_BEGINNER, run_attempt

RUBRIC_MAX_TOKENS = 4096
# Each diff is capped independently: every input token is spent from the
# gateway's shared budget (llm.py), and a real OpenZeppelin fix diff is
# routinely far larger than either cap alone.
DIFF_CHARS = 3000
RETRIEVAL_K = 3
TEST_SUITE_TIMEOUT_SECONDS = 900

# Hardhat/Foundry test-file conventions. Heuristic, not authoritative — good
# enough to tell a regression test from the fix it accompanies in a real
# OpenZeppelin PR diff, which is the only thing this is used for.
TEST_PATH_MARKERS: tuple[str, ...] = ("test/", "tests/")
TEST_FILE_SUFFIXES: tuple[str, ...] = (".test.js", ".test.ts", ".t.sol")


@dataclass(frozen=True)
class JudgeResult:
    """One Attempt, graded.

    `passed` is the objective signal alone (the repo's own suite against the
    Attempt's commit, with the Ground Truth's regression test applied) —
    CONTEXT.md's Round entry needs the pass-rate threshold anchored in
    something real, not an LLM's self-report. `gaps` is the separate
    qualitative signal the spec's rubric comparison produces, feeding the next
    Round's Curriculum revision.
    """

    profile: LearnerProfile
    module_topic: str
    passed: bool
    gaps: tuple[Gap, ...]


class JudgeGradingError(RuntimeError):
    """A rubric response couldn't be used, so its Gaps can't be trusted.

    See ADR-0002: an empty Gaps list already means "the Attempt matches the
    Ground Truth", so swallowing an unusable response into `()` would be
    indistinguishable from a flawless Attempt — the one place this project
    cannot afford invisible degradation, since the pass rate is its central
    claim.
    """


# --- pure core ---------------------------------------------------------------


def is_test_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(marker in normalized for marker in TEST_PATH_MARKERS) or normalized.endswith(
        TEST_FILE_SUFFIXES
    )


def select_test_files(changed_paths: Sequence[str]) -> tuple[str, ...]:
    """Which of a diff's changed files are test files, by path convention."""
    return tuple(path for path in changed_paths if is_test_file(path))


def parse_gaps(content: str, finish_reason: str | None, known_topics: Sequence[str]) -> tuple[Gap, ...]:
    """Read the Judge's rubric Gaps, mapping each to a Module or flagging it.

    A Gap naming a module_topic this Curriculum doesn't have is read as
    unmapped (module_topic=None) rather than kept as a hallucinated Module —
    mirroring curriculum.py's _hard_prerequisites guard against the same
    failure mode, applied here to Gap.module_topic instead of a prerequisite.
    """
    if finish_reason == "length":
        raise JudgeGradingError(
            f"rubric response hit the {RUBRIC_MAX_TOKENS}-token budget before it finished"
        )
    parsed = json_object(content)
    if parsed is None or not isinstance(parsed.get("gaps"), list):
        raise JudgeGradingError(
            "rubric response was not a JSON object with a 'gaps' list "
            f"(finish_reason={finish_reason}, {len(content)} chars): {content[:300]!r}"
        )
    known = frozenset(known_topics)
    gaps: list[Gap] = []
    for item in parsed["gaps"]:
        if not isinstance(item, dict) or not item.get("description"):
            continue
        topic = item.get("module_topic")
        gaps.append(
            Gap(
                description=str(item["description"]),
                module_topic=str(topic) if isinstance(topic, str) and topic in known else None,
            )
        )
    return tuple(gaps)


def _rubric_prompt(
    module_topic: str, known_topics: Sequence[str], attempt_diff: str, ground_truth_diff: str
) -> str:
    return (
        f'A Student Agent attempted a real GitHub issue for the Curriculum Module "{module_topic}". '
        "Compare its change against the real merged fix (Ground Truth) and find concrete Gaps in "
        "what the Attempt did or didn't do — do not just restate that it differs from the fix.\n\n"
        "MODULES IN THIS CURRICULUM:\n"
        + "\n".join(f"- {topic}" for topic in known_topics)
        + "\n\n=== Attempt's diff ===\n"
        f"{attempt_diff[:DIFF_CHARS]}\n\n"
        "=== Ground Truth diff (the real merged fix; never shown to the Student Agent) ===\n"
        f"{ground_truth_diff[:DIFF_CHARS]}\n\n"
        "For each Gap, name the ONE module from the list above whose teaching would have "
        "prevented it, or null if no module above covers it.\n"
        'Respond with ONLY a JSON object of the form {"gaps": [{"description": "...", '
        '"module_topic": "..." or null}]}. An empty "gaps" list means the Attempt matches the '
        "Ground Truth with no notable gap. No prose."
    )


def _ask(prompt: str) -> tuple[str, str | None]:
    return complete(prompt, max_tokens=RUBRIC_MAX_TOKENS)


def _graded(
    prompt: str,
    known_topics: Sequence[str],
    ask: Callable[[str], tuple[str, str | None]] = _ask,
) -> tuple[Gap, ...]:
    """One rubric call, retried once before a refusal stands — mirrors curriculum.py's _planned."""
    try:
        return parse_gaps(*ask(prompt), known_topics)
    except JudgeGradingError:
        return parse_gaps(*ask(prompt), known_topics)


@neatlogs.span(kind="AGENT", role="Judge")
def grade_gaps(
    module_topic: str, known_topics: Sequence[str], attempt_diff: str, ground_truth_diff: str
) -> tuple[Gap, ...]:
    """The qualitative half of a grade: Gaps mapped to a Module, from the model.

    Spanned on its own, separately from `run_judge`'s subprocess work (git
    diffing, `npm test`), so the AGENT/Judge span roots only the LLM call —
    mirroring curriculum.py's per-call AGENT spans (`plan_prerequisites` etc.)
    rather than one span covering a whole orchestrator, which would otherwise
    make an npm install/test run (minutes) read as part of an "LLM agent" span.

    Skips the LLM call entirely when the Attempt made no change at all — that
    case needs no rubric judgement, and asking one would risk the model
    inventing a Gap more specific than "did nothing".
    """
    if not attempt_diff.strip():
        return (Gap(description="The Attempt made no changes at all.", module_topic=module_topic),)
    return _graded(_rubric_prompt(module_topic, known_topics, attempt_diff, ground_truth_diff), known_topics)


# --- live orchestration -------------------------------------------------------


def changed_files(workspace: Path, from_sha: str, to_sha: str) -> tuple[str, ...]:
    """Paths touched between two commits already reachable in `workspace`'s history."""
    result = subprocess.run(
        ["git", "diff", "--name-only", from_sha, to_sha],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def git_diff(workspace: Path, from_sha: str, to_sha: str) -> str:
    """The textual diff between two commits already reachable in `workspace`'s history.

    Both an Attempt's commit and a Ground Truth's merge commit live in the
    same full clone `student_agent.clone_isolated_workspace` made — never a
    shallow one — so no extra GitHub fetch is needed to compare them.
    """
    result = subprocess.run(
        ["git", "diff", from_sha, to_sha], cwd=workspace, check=True, capture_output=True, text=True
    )
    return result.stdout


def apply_ground_truth_tests(workspace: Path, ground_truth: GroundTruth) -> tuple[str, ...]:
    """Bring the Ground Truth's new/changed test files into the Attempt's tree.

    See the module docstring: without this, running the suite against an
    Attempt that changed nothing at all would still pass trivially, since the
    regression test that would have caught that doesn't exist until the real
    fix commit. Checks out test files only — never the fix itself.
    """
    paths = select_test_files(
        changed_files(workspace, ground_truth.pre_fix_commit_sha, ground_truth.merge_commit_sha)
    )
    if paths:
        subprocess.run(
            ["git", "checkout", ground_truth.merge_commit_sha, "--", *paths],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
    return paths


def run_test_suite(workspace: Path) -> bool:
    """Whether the target repo's own test suite passes against the current tree.

    OpenZeppelin's package.json names `test` as `hardhat test`; a fresh clone
    has no node_modules, so installing first is required. Returns False (not
    an exception) for an install failure or a failing/crashing suite alike —
    both are legitimate "did not pass" outcomes for the objective signal, not
    errors in this function.
    """
    # shell=True: npm, like entire/opencode elsewhere in this project, is a
    # Windows .cmd shim rather than a plain exe CreateProcess can launch
    # directly.
    install = subprocess.run(
        ["npm", "ci"],
        cwd=workspace,
        capture_output=True,
        shell=True,
        timeout=TEST_SUITE_TIMEOUT_SECONDS,
    )
    if install.returncode != 0:
        return False
    result = subprocess.run(
        ["npm", "test"],
        cwd=workspace,
        capture_output=True,
        shell=True,
        timeout=TEST_SUITE_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def run_judge(
    module: Module,
    ground_truth: GroundTruth,
    attempt: AttemptResult,
    known_topics: Sequence[str],
) -> JudgeResult:
    """Grade one Attempt: the repo's own suite for the objective signal, the
    LLM-rubric comparison against Ground Truth for the qualitative one.

    Not itself spanned — `grade_gaps` carries the AGENT/Judge span around the
    one LLM call this makes; `_judge_one_attempt`'s WORKFLOW span roots this
    function's own git/npm subprocess work for a standalone run.
    """
    if attempt.commit_sha is None:
        return JudgeResult(
            profile=attempt.profile,
            module_topic=module.topic,
            passed=False,
            gaps=(Gap(description="The Attempt never committed anything.", module_topic=module.topic),),
        )

    apply_ground_truth_tests(attempt.workspace, ground_truth)
    passed = run_test_suite(attempt.workspace)

    attempt_diff = git_diff(attempt.workspace, ground_truth.pre_fix_commit_sha, attempt.commit_sha)
    ground_truth_diff = git_diff(attempt.workspace, ground_truth.pre_fix_commit_sha, ground_truth.merge_commit_sha)
    gaps = grade_gaps(module.topic, known_topics, attempt_diff, ground_truth_diff)

    return JudgeResult(profile=attempt.profile, module_topic=module.topic, passed=passed, gaps=gaps)


# This is the WORKFLOW root for the RETRIEVER span inside `retrieve` and the
# AGENT spans inside `run_attempt` and `run_judge` below — without one, those
# spans arrive in Neatlogs parented to nothing once run_curriculum's own
# WORKFLOW span has closed. See retrieval.py's and student_agent.py's
# identical note on why their own smoke tests need one.
@neatlogs.span(kind="WORKFLOW", name="judge_smoke_test")
def _judge_one_attempt(module, ground_truth, curriculum, workspace_root: Path) -> JudgeResult:
    chunks = retrieve(curriculum, module.proof_of_skill_task.title, RETRIEVAL_K)
    attempt = run_attempt(module, ground_truth, TOTAL_BEGINNER, chunks, workspace_root=workspace_root)
    print(f"\n--- Attempt: {module.topic} / {attempt.profile.name} ---")
    print(f"opencode exit code: {attempt.opencode_exit_code}")
    print(f"committed: {attempt.commit_sha or '(no commit)'}")

    known_topics = tuple(m.topic for m in curriculum.modules)
    result = run_judge(module, ground_truth, attempt, known_topics)
    shutil.rmtree(attempt.workspace, ignore_errors=True)
    return result


def _print_result(result: JudgeResult) -> None:
    print(f"\n--- Judge: {result.module_topic} / {result.profile.name} ---")
    print(f"tests passed: {result.passed}")
    if not result.gaps:
        print("gaps: none")
    for gap in result.gaps:
        mapped = gap.module_topic or "(no module in this curriculum covers it)"
        print(f"gap: {gap.description}")
        print(f"     -> {mapped}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        curriculum = run_curriculum(run_analyst("smart contract development"))
        topics = [module.topic for module in curriculum.modules]
        assignment = run_proof_of_skill_sourcing(curriculum.target_skill, topics)
        curriculum = with_proof_of_skill_tasks(curriculum, assignment.tasks)

        module = next((m for m in curriculum.modules if m.proof_of_skill_task is not None), None)
        if module is None:
            print("No module carries a Proof-of-Skill Task; nothing to judge.")
            return
        ground_truth = assignment.ground_truths[module.proof_of_skill_task.key]

        with tempfile.TemporaryDirectory(prefix="pareto-judge-") as workspace_root:
            result = _judge_one_attempt(module, ground_truth, curriculum, Path(workspace_root))
            _print_result(result)
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
