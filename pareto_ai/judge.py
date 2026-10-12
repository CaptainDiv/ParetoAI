"""Judge agent: grades an Attempt against its Ground Truth, producing Gaps.

Ticket #7. Per CONTEXT.md's Judge Agent and Gap entries and #1's spec, a grade
has two independent signals:

  objective   the target repo's own test suite, run against the Attempt's
              resulting commit — "anchored in something real" rather than an
              LLM's self-report.
  qualitative an LLM-rubric comparison against the Ground Truth PR, producing
              Gaps mapped to a Pareto Table entry (a Module, or one the
              Curriculum cut; flagged, per CONTEXT.md, when none covers it).

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

from pareto_ai.curriculum import Gap, Module
from pareto_ai.frozen import FROZEN_RUN_PATH, load_or_build_frozen_run
from pareto_ai.llm import complete, json_object
from pareto_ai.processes import run_killing_tree
from pareto_ai.proof_of_skill import GroundTruth
from pareto_ai.retrieval import retrieve
from pareto_ai.student_agent import (
    AttemptResult,
    LearnerProfile,
    TOTAL_BEGINNER,
    prepare_warm_base,
    remote_url_for,
    run_attempt,
)
from pareto_ai.warm_base import WARM_CACHE_DIR, WarmBase, discard_attempt_workspace

RUBRIC_MAX_TOKENS = 4096
# Each diff is capped independently: every input token is spent from the
# gateway's shared budget (llm.py), and a real OpenZeppelin fix diff is
# routinely far larger than either cap alone.
DIFF_CHARS = 3000
RETRIEVAL_K = 3
INSTALL_TIMEOUT_SECONDS = 900  # only a cold workspace ever pays this
TEST_SUITE_TIMEOUT_SECONDS = 300

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

    `known_topics` is every skill a Gap may blame: the Curriculum's Modules and
    the Pareto Table entries it cut. A Gap naming anything else is read as
    unmapped (module_topic=None) rather than kept as a hallucinated skill —
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


def regression_test_command(regression_tests: Sequence[str]) -> list[str]:
    """The command the objective signal runs: the Ground Truth's regression tests only.

    `npm test` is `hardhat test` over all 163 of OpenZeppelin's test files
    after a full compile, which is most of the Judge's wall-clock time, and
    adds nothing the grade needs: the regression test the real fix shipped is
    the one that says whether the Attempt fixed *this* issue. Only Hardhat
    test files can be named to `hardhat test`; a fix whose only tests are
    Foundry `.t.sol` falls back to the whole suite rather than running nothing
    and reporting a pass.
    """
    hardhat_tests = [path for path in regression_tests if path.endswith((".test.js", ".test.ts"))]
    return ["npx", "hardhat", "test", *hardhat_tests] if hardhat_tests else ["npm", "test"]


def _rubric_prompt(
    module_topic: str,
    known_topics: Sequence[str],
    cut_topics: Sequence[str],
    attempt_diff: str,
    ground_truth_diff: str,
) -> str:
    cut_list = (
        "\n\nSKILLS NOT IN THIS CURRICULUM (cut from it, but available to add):\n"
        + "\n".join(f"- {topic}" for topic in cut_topics)
        if cut_topics
        else ""
    )
    return (
        f'A Student Agent attempted a real GitHub issue for the Curriculum Module "{module_topic}". '
        "Compare its change against the real merged fix (Ground Truth) and find concrete Gaps in "
        "what the Attempt did or didn't do — do not just restate that it differs from the fix.\n\n"
        "MODULES IN THIS CURRICULUM:\n"
        + "\n".join(f"- {topic}" for topic in known_topics)
        + cut_list
        + "\n\n=== Attempt's diff ===\n"
        f"{attempt_diff[:DIFF_CHARS]}\n\n"
        "=== Ground Truth diff (the real merged fix; never shown to the Student Agent) ===\n"
        f"{ground_truth_diff[:DIFF_CHARS]}\n\n"
        "For each Gap, name the ONE skill from the lists above whose teaching would have "
        "prevented it — a module the Curriculum already has, or a skill it left out — or null "
        "if none of them covers it.\n"
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
    module_topic: str,
    known_topics: Sequence[str],
    attempt_diff: str,
    ground_truth_diff: str,
    cut_topics: Sequence[str] = (),
) -> tuple[Gap, ...]:
    """The qualitative half of a grade: Gaps mapped to a Pareto Table entry, from the model.

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
    return _graded(
        _rubric_prompt(module_topic, known_topics, cut_topics, attempt_diff, ground_truth_diff),
        (*known_topics, *cut_topics),
    )


# --- live orchestration -------------------------------------------------------


def fetch_ground_truth(workspace: Path, ground_truth: GroundTruth, *, remote_url: str | None = None) -> None:
    """Bring the real fix into an Attempt's workspace, now that the Attempt is over.

    The workspace was built without it (`student_agent.clone_isolated_workspace`)
    so a Student Agent could not read its own answer; the Judge needs it to
    diff against and to take its regression tests from. Called only from
    `run_judge`, after the Attempt has ended.
    """
    subprocess.run(
        ["git", "fetch", "--no-tags", remote_url or remote_url_for(ground_truth), ground_truth.merge_commit_sha],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


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

    Both commits must already be in `workspace`: an Attempt's own, and the
    Ground Truth's merge commit, which `fetch_ground_truth` brings in.
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


def needs_install(workspace: Path) -> bool:
    """Whether `workspace` still has to install its dependencies.

    A workspace built from a warm base has them already, as a link into the
    base (`warm_base.build_attempt_workspace`). `npm ci` deletes `node_modules`
    before reinstalling it, so running it through that link would wipe the
    base's dependencies for every Attempt that follows.
    """
    return not (workspace / "node_modules").exists()


def run_test_suite(workspace: Path, regression_tests: Sequence[str] = ()) -> bool:
    """Whether the Ground Truth's regression tests pass against the current tree.

    OpenZeppelin's package.json names `test` as `hardhat test`; a cold clone has
    no node_modules, so installing first is required, and a warm one does not
    (see `needs_install`). Returns False (not an exception) for an install
    failure, a failing or crashing run, or one that outruns its deadline — all
    legitimate "did not pass" outcomes for the objective signal, not errors in
    this function. See `regression_test_command` for why only the regression
    tests run.
    """
    # shell=True: npm and npx, like entire/opencode elsewhere in this project,
    # are Windows .cmd shims rather than plain exes CreateProcess can launch
    # directly. run_killing_tree stops the whole tree at a deadline, which a
    # shim's child `node` would otherwise outlive.
    quiet = {"shell": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if needs_install(workspace):
        installed = run_killing_tree(["npm", "ci"], cwd=workspace, timeout_seconds=INSTALL_TIMEOUT_SECONDS, **quiet)
        if installed != 0:
            return False
    tested = run_killing_tree(
        regression_test_command(regression_tests),
        cwd=workspace,
        timeout_seconds=TEST_SUITE_TIMEOUT_SECONDS,
        **quiet,
    )
    return tested == 0


def run_judge(
    module: Module,
    ground_truth: GroundTruth,
    attempt: AttemptResult,
    known_topics: Sequence[str],
    cut_topics: Sequence[str] = (),
) -> JudgeResult:
    """Grade one Attempt: the repo's own suite for the objective signal, the
    LLM-rubric comparison against Ground Truth for the qualitative one.

    Not itself spanned — `grade_gaps` carries the AGENT/Judge span around the
    one LLM call this makes; `_judge_one_attempt`'s WORKFLOW span roots this
    function's own git/npm subprocess work for a standalone run.

    `known_topics` are the Curriculum's Modules and `cut_topics` the Pareto Table
    entries it left out: a Gap may blame either, which is how a Round can ask
    for a Module the Curriculum does not have yet (ADR-0009).
    """
    if attempt.commit_sha is None:
        return JudgeResult(
            profile=attempt.profile,
            module_topic=module.topic,
            passed=False,
            gaps=(Gap(description="The Attempt never committed anything.", module_topic=module.topic),),
        )

    fetch_ground_truth(attempt.workspace, ground_truth)
    regression_tests = apply_ground_truth_tests(attempt.workspace, ground_truth)
    passed = run_test_suite(attempt.workspace, regression_tests)

    attempt_diff = git_diff(attempt.workspace, ground_truth.pre_fix_commit_sha, attempt.commit_sha)
    ground_truth_diff = git_diff(attempt.workspace, ground_truth.pre_fix_commit_sha, ground_truth.merge_commit_sha)
    gaps = grade_gaps(module.topic, known_topics, attempt_diff, ground_truth_diff, cut_topics)

    return JudgeResult(profile=attempt.profile, module_topic=module.topic, passed=passed, gaps=gaps)


# This is the WORKFLOW root for the RETRIEVER span inside `retrieve` and the
# AGENT spans inside `run_attempt` and `run_judge` below — without one, those
# spans arrive in Neatlogs parented to nothing once run_curriculum's own
# WORKFLOW span has closed. See retrieval.py's and student_agent.py's
# identical note on why their own smoke tests need one.
@neatlogs.span(kind="WORKFLOW", name="judge_smoke_test")
def _judge_one_attempt(module, ground_truth, curriculum, base: WarmBase, workspace_root: Path) -> JudgeResult:
    chunks = retrieve(curriculum, module.proof_of_skill_task.title, RETRIEVAL_K)
    attempt = run_attempt(module, TOTAL_BEGINNER, chunks, base, workspace_root=workspace_root)
    print(f"\n--- Attempt: {module.topic} / {attempt.profile.name} ---")
    print(f"opencode exit code: {attempt.opencode_exit_code}")
    print(f"committed: {attempt.commit_sha or '(no commit)'}")

    known_topics = tuple(m.topic for m in curriculum.modules)
    # Every entry the Curriculum cut is linked from some Module as a Deferred Resource.
    cut_topics = tuple(entry.skill for m in curriculum.modules for entry in m.deferred_resources)
    result = run_judge(module, ground_truth, attempt, known_topics, cut_topics)
    discard_attempt_workspace(attempt.workspace)
    return result


def _print_result(result: JudgeResult) -> None:
    print(f"\n--- Judge: {result.module_topic} / {result.profile.name} ---")
    print(f"tests passed: {result.passed}")
    if not result.gaps:
        print("gaps: none")
    for gap in result.gaps:
        mapped = gap.module_topic or "(no Pareto Table entry covers it)"
        print(f"gap: {gap.description}")
        print(f"     -> {mapped}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        frozen = load_or_build_frozen_run(FROZEN_RUN_PATH)
        curriculum = frozen.curriculum
        module = next((m for m in curriculum.modules if m.proof_of_skill_task is not None), None)
        if module is None:
            print("No module carries a Proof-of-Skill Task; nothing to judge.")
            return
        ground_truth = frozen.assignment.ground_truths[module.proof_of_skill_task.key]

        base = prepare_warm_base(ground_truth, WARM_CACHE_DIR)
        with tempfile.TemporaryDirectory(prefix="pareto-judge-") as workspace_root:
            result = _judge_one_attempt(module, ground_truth, curriculum, base, Path(workspace_root))
            _print_result(result)
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
