"""Student Agent: a real Gemini CLI Attempt against a Proof-of-Skill Task.

Ticket #6. One Attempt is one Student Agent, conditioned on one Learner
Profile, working a Module's Proof-of-Skill Task in its own isolated clone of
the target repo — a real coding-agent session (clone, edit, run tests, commit),
not a text-only simulation, per CONTEXT.md's Student Agent entry.

What bounds a Student Agent's knowledge is the whole point of this module, so
it is structural rather than a convention to remember:

  - `build_attempt_prompt` takes a Module, a LearnerProfile, retrieved Chunks
    and a Repo Map string — nothing else reaches it, and in particular nothing
    in its signature could carry a GroundTruth (a test pins this, mirroring
    proof_of_skill's own pin on ProofOfSkillTask's fields). The Chunks
    themselves already came from `retrieval.retrieve`, which is bounded to the
    Curriculum; this module never calls the Curriculum or Pareto Table directly.
  - The isolated clone is set up *using* the GroundTruth (its repo and
    pre-fix commit decide where to clone), but that setup step is the harness
    preparing the Attempt's environment, not the Student Agent's visible
    context — the clone is just files on disk, and the GroundTruth itself is
    never written into any prompt.

Checkpoint capture for the Gemini CLI session runs through `entire-agent-gemini`
(see that module and ADR-0004) — Entire's native Gemini support was retired,
so this ticket depends on that integration being installed and registered
(`entire agent add gemini`) and the machine having already run `entire login`,
neither of which an agent can do on a user's behalf.

Pure core (prompt assembly, workspace naming) is TDD'd. Everything that shells
out — git, entire, gemini — is live and smoke-tested via main(), per the
project's established pattern (see proof_of_skill.fetch_task_candidates,
retrieval's live smoke test): these genuinely need the real tools on $PATH,
which unit tests cannot stand in for without pretending to verify something
they can't.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import neatlogs

from pareto_ai.analyst import run_analyst
from pareto_ai.curriculum import Curriculum, Module, run_curriculum, with_proof_of_skill_tasks
from pareto_ai.proof_of_skill import GroundTruth, run_proof_of_skill_sourcing
from pareto_ai.retrieval import Chunk, retrieve

GEMINI_TIMEOUT_SECONDS = 1800
RETRIEVAL_K = 3


@dataclass(frozen=True)
class LearnerProfile:
    """A named archetype a Student Agent simulates (CONTEXT.md's Learner Profile).

    `framing` is written into the Attempt prompt verbatim, so it is prose aimed
    at an LLM ("You are a..."), not a label.
    """

    name: str
    framing: str


TOTAL_BEGINNER = LearnerProfile(
    name="Total Beginner",
    framing=(
        "You are a total beginner to smart contract development. You know general "
        "programming concepts but have never written Solidity before, and you are "
        "unfamiliar with blockchain- and EVM-specific terminology. Look things up in "
        "the Repo Map and the provided notes rather than assuming you already know them."
    ),
)

SOME_IT_BACKGROUND = LearnerProfile(
    name="Some IT/Tech Background",
    framing=(
        "You have a general IT/software background (scripting, web development, or "
        "similar) but are new to smart contract development specifically. You can "
        "pick up unfamiliar syntax quickly, but you don't yet know Solidity's "
        "idioms or EVM-specific pitfalls — rely on the Repo Map and the provided "
        "notes for those rather than guessing."
    ),
)

MVP_LEARNER_PROFILES: tuple[LearnerProfile, ...] = (TOTAL_BEGINNER, SOME_IT_BACKGROUND)


@dataclass(frozen=True)
class AttemptResult:
    profile: LearnerProfile
    module_topic: str
    workspace: Path
    gemini_exit_code: int
    commit_sha: str | None  # None if the Attempt never committed anything
    checkpoint_verified: bool  # a Checkpoint for commit_sha was found on entire/checkpoints/v1


# --- pure core ---------------------------------------------------------------


def build_attempt_prompt(
    module: Module,
    profile: LearnerProfile,
    chunks: Sequence[Chunk],
    repo_map: str,
) -> str:
    """The whole of a Student Agent's starting context for one Attempt.

    Raises ValueError if `module` carries no Proof-of-Skill Task: there would
    be nothing for the Attempt to do, and silently skipping it would look like
    an Attempt that ran and did nothing, rather than one that was never set up.
    """
    task = module.proof_of_skill_task
    if task is None:
        raise ValueError(f"module {module.topic!r} has no Proof-of-Skill Task assigned")

    chunk_notes = "\n\n".join(chunk.text for chunk in chunks) or "(nothing in the Curriculum matched this task)"

    return (
        f"=== Learner Profile: {profile.name} ===\n"
        f"{profile.framing}\n\n"
        "=== Repo Map ===\n"
        f"{repo_map}\n\n"
        "=== Notes from the Curriculum ===\n"
        f"{chunk_notes}\n\n"
        "=== Your task ===\n"
        f"Resolve this issue: {task.title}\n\n"
        f"{task.body}\n\n"
        f"Issue URL: {task.url}\n\n"
        "Make the smallest change that fixes the issue, run the project's existing "
        "tests, and once they pass, commit your change with a clear commit message. "
        "Do not reference this prompt or any Curriculum material in the commit message."
    )


def attempt_workspace_name(module_topic: str, profile: LearnerProfile, *, token: str) -> str:
    """A short, filesystem-safe, collision-resistant name for an Attempt's clone.

    Deterministic given `token`, so a caller controls uniqueness explicitly
    (live callers pass a fresh uuid; tests pass a fixed token) rather than this
    function reaching for randomness itself.
    """
    digest = hashlib.sha256(f"{module_topic}|{profile.name}|{token}".encode()).hexdigest()[:12]
    return f"attempt-{digest}"


# --- live orchestration -------------------------------------------------------


def clone_isolated_workspace(ground_truth: GroundTruth, parent_dir: Path, *, dir_name: str) -> Path:
    """A fresh clone of `ground_truth`'s repo, checked out at its pre-fix commit.

    Every Attempt gets its own clone under `parent_dir`, per CONTEXT.md's rule
    that Attempts never share a workspace. GroundTruth is used here only to
    locate the clone — nothing from it is written anywhere a Student Agent
    reads from.
    """
    workspace = parent_dir / dir_name
    subprocess.run(
        ["git", "clone", f"https://github.com/{ground_truth.repo}.git", str(workspace)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", ground_truth.pre_fix_commit_sha],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    return workspace


def current_head(workspace: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True
    ).stdout.strip()


def enable_entire(workspace: Path) -> None:
    """Turn on Entire's checkpoint capture for this one clone.

    Required before a commit in `workspace` can produce a Checkpoint at all —
    "enable Entire" is explicit in issue #6's "what to build". `--agent gemini`
    does double duty here, confirmed live: `entire agent add gemini` alone
    answers "Unknown agent" (external-agent discovery is gated behind an
    `external_agents` setting nothing sets automatically), but `entire enable
    --agent gemini` bypasses that gate for this one named, explicit selection
    and both registers *and* enables the integration in a single call — see
    ADR-0004. `--checkpoint-backend branch` is explicit because Entire's
    current default is per-commit git refs, not the `entire/checkpoints/v1`
    branch CONTEXT.md's Checkpoint entry names.
    """
    subprocess.run(
        ["entire", "enable", "--agent", "gemini", "--checkpoint-backend", "branch"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


def compute_repo_map(workspace: Path) -> str:
    """Entire's `entire-graph` Repo Map for `workspace`, computed once per task.

    Requires `entire` on $PATH with the graph plugin installed
    (`entire plugin install graph`). Raises CalledProcessError if either command
    fails — a missing Repo Map should stop the Attempt, not quietly start it
    with an empty one that reads as "this repo has no structure".
    """
    subprocess.run(["entire", "graph", "index"], cwd=workspace, check=True, capture_output=True)
    snapshot = subprocess.run(
        ["entire", "graph", "snapshot"], cwd=workspace, check=True, capture_output=True
    )
    return snapshot.stdout.decode("utf-8", errors="replace")


def run_gemini_attempt(workspace: Path, prompt: str) -> int:
    """Run the Attempt as a real, non-interactive Gemini CLI session.

    `--skip-trust` is required for a freshly cloned, never-opened-interactively
    workspace to run headlessly at all (see ADR-0004's note on Gemini CLI's own
    trusted-folders gate). Checkpoint capture happens as a side effect of this
    call, through hooks `entire-agent-gemini` installed — nothing here talks to
    Entire directly.
    """
    result = subprocess.run(
        ["gemini", "-p", prompt, "--skip-trust"],
        cwd=workspace,
        timeout=GEMINI_TIMEOUT_SECONDS,
    )
    return result.returncode


def committed_sha(workspace: Path, before_sha: str) -> str | None:
    """The workspace's current HEAD if the Attempt committed, else None."""
    after = current_head(workspace)
    return after if after != before_sha else None


def verify_checkpoint(workspace: Path, commit_sha: str) -> bool:
    """Whether `entire/checkpoints/v1` carries a Checkpoint naming `commit_sha`.

    This is the acceptance criterion in issue #6 that makes a Checkpoint "real"
    rather than merely attempted: `run_gemini_attempt` only triggers capture via
    the hooks `entire-agent-gemini` installs, so this is what actually confirms
    one landed. Best-effort by design, not by neglect — Entire's exact
    checkpoint storage format on that branch isn't documented, so this looks
    for the one thing that must be true of any encoding: the branch exists and
    some commit on it mentions `commit_sha`. False (not an exception) on any
    git failure, including the branch not existing yet: that is itself a
    legitimate "not verified" answer the caller needs to see, not a crash.
    """
    try:
        log = subprocess.run(
            ["git", "log", "entire/checkpoints/v1", "--all", "--oneline", f"--grep={commit_sha}"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return False
    return bool(log.stdout.strip())


@neatlogs.span(kind="AGENT", role="Student")
def run_attempt(
    module: Module,
    ground_truth: GroundTruth,
    profile: LearnerProfile,
    chunks: Sequence[Chunk],
    *,
    workspace_root: Path,
) -> AttemptResult:
    """One full Attempt: isolated clone, Repo Map, bounded prompt, real session."""
    dir_name = attempt_workspace_name(module.topic, profile, token=uuid.uuid4().hex)
    workspace = clone_isolated_workspace(ground_truth, workspace_root, dir_name=dir_name)
    before_sha = current_head(workspace)
    enable_entire(workspace)

    repo_map = compute_repo_map(workspace)
    prompt = build_attempt_prompt(module, profile, chunks, repo_map)
    exit_code = run_gemini_attempt(workspace, prompt)

    commit_sha = committed_sha(workspace, before_sha)
    return AttemptResult(
        profile=profile,
        module_topic=module.topic,
        workspace=workspace,
        gemini_exit_code=exit_code,
        commit_sha=commit_sha,
        checkpoint_verified=commit_sha is not None and verify_checkpoint(workspace, commit_sha),
    )


# This function is the WORKFLOW root for every `retrieve` call and `run_attempt`
# (an AGENT span) below it. Without it those spans would arrive in Neatlogs
# parented to nothing, once run_curriculum's own WORKFLOW span has closed — see
# retrieval.py's identical note on why its own smoke test needs one.
@neatlogs.span(kind="WORKFLOW", name="student_agent_attempts")
def _run_attempts_for_every_profile(
    module: Module, ground_truth: GroundTruth, curriculum: Curriculum, workspace_root: Path
) -> list[AttemptResult]:
    results = []
    for profile in MVP_LEARNER_PROFILES:
        chunks = retrieve(curriculum, module.proof_of_skill_task.title, RETRIEVAL_K)
        print(f"\n--- Attempt: {module.topic} / {profile.name} ---")
        result = run_attempt(module, ground_truth, profile, chunks, workspace_root=workspace_root)
        print(f"gemini exit code: {result.gemini_exit_code}")
        print(f"committed: {result.commit_sha or '(no commit)'}")
        print(f"checkpoint verified on entire/checkpoints/v1: {result.checkpoint_verified}")
        shutil.rmtree(result.workspace, ignore_errors=True)
        results.append(result)
    return results


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        curriculum = run_curriculum(run_analyst("smart contract development"))
        topics = [module.topic for module in curriculum.modules]
        assignment = run_proof_of_skill_sourcing(curriculum.target_skill, topics)
        curriculum = with_proof_of_skill_tasks(curriculum, assignment.tasks)

        module = next((m for m in curriculum.modules if m.proof_of_skill_task is not None), None)
        if module is None:
            print("No module carries a Proof-of-Skill Task; nothing to attempt.")
            return
        ground_truth = assignment.ground_truths[module.proof_of_skill_task.key]

        with tempfile.TemporaryDirectory(prefix="pareto-attempts-") as workspace_root:
            _run_attempts_for_every_profile(module, ground_truth, curriculum, Path(workspace_root))
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
