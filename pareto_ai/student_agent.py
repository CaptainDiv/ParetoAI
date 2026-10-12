"""Student Agent: a real OpenCode Attempt against a Proof-of-Skill Task.

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

The coding-agent engine is OpenCode, not Gemini CLI — see ADR-0005 for the
switch and why. In short: ADR-0004's custom `entire-agent-gemini` integration
genuinely works (confirmed live: login, enablement, hook installation, a real
commit), but Gemini CLI's own Policy Engine denies every `run_shell_command`
call under full headless automation (`--yolo` plus workspace trust isn't
enough — confirmed by hitting the same wall in two independent environments),
which blocks every edit-run-commit step an Attempt needs. OpenCode is
natively supported by Entire (`entire enable --agent opencode` — no custom
integration required) and has no equivalent headless-shell gate. The LLM
behind it is the neatHack hackathon's own LiteLLM gateway
(`opencode.json` at the repo root, `LITELLM_API_KEY` in `.env`) rather than a
provider's small free tier — Groq's free-tier organization-wide token budget
(8,000 TPM) is smaller than OpenCode's own baseline tool-definition overhead,
so every call failed before Entire or the task ever entered the picture,
regardless of which Groq model was chosen.

Pure core (prompt assembly, workspace naming) is TDD'd. Everything that shells
out — git, entire, opencode — is live and smoke-tested via main(), per the
project's established pattern (see proof_of_skill.fetch_task_candidates,
retrieval's live smoke test): these genuinely need the real tools on $PATH,
which unit tests cannot stand in for without pretending to verify something
they can't. Verified live end-to-end on 2026-10-11: a real edit, a real
commit, and a real Entire Checkpoint linked to it on `entire/checkpoints/v1`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import neatlogs

from pareto_ai.curriculum import Curriculum, Module
from pareto_ai.frozen import FROZEN_RUN_PATH, load_or_build_frozen_run
from pareto_ai.processes import run_killing_tree
from pareto_ai.proof_of_skill import GroundTruth
from pareto_ai.retrieval import Chunk, retrieve
from pareto_ai.warm_base import (
    WARM_CACHE_DIR,
    WarmBase,
    build_attempt_workspace,
    disable_commit_hooks,
    discard_attempt_workspace,
    remove_attempt_workspace,
    warm_base_dir,
)

# An Attempt is held to five minutes, which is the deadline for the whole OpenCode
# session *including its own start-up*. Measured 2026-10-12: that start-up alone is
# ~100 s here (about the same with and without Entire once its plugin dependencies are
# pre-installed), the agent then read, planned and edited within ~80 s of model time, and
# a test run on a warm workspace takes ~25 s. A stuck agent costs its whole deadline, so
# it stays short; what used up eight minutes before was a cold recompile and a plugin
# install, both now done once up front (warm_base.py, ensure_opencode_plugin_dependencies).
OPENCODE_TIMEOUT_SECONDS = 300
OPENCODE_TIMEOUT_EXIT_CODE = 124  # `timeout(1)`'s convention for a killed-for-time command
ENTIRE_GRAPH_TIMEOUT_SECONDS = 300
WARM_BASE_STEP_TIMEOUT_SECONDS = 15 * 60  # one-off per pre-fix commit: npm ci, then the first compile
RETRIEVAL_K = 3
# The prompt travels as one command-line argument (CreateProcess allows ~32,000
# characters), and the issue body (up to 4,000) and Chunks share it with the Repo Map.
REPO_MAP_CHARS = 2000
REPO_MAP_SYMBOLS_PER_FILE = 8

# "litellm" names the provider block in opencode.json, which points at
# neatHack's own gateway rather than a provider's small free tier. The model is
# Claude Haiku, not the cheaper Amazon Nova Lite this started on: confirmed
# live 2026-10-11 that Nova Lite, given the whole task, looped 90 times on one
# identical failed file read for 17 minutes and never opened the file the
# issue named, so an Attempt graded its model's reliability rather than what the
# Curriculum taught. Haiku is also what the Analyst, Curriculum and Judge already
# run on (llm.py), so the whole Round shares one gateway budget line.
OPENCODE_MODEL = "litellm/claude-haiku"

# opencode.json lives at the repo root, not inside pareto_ai/, so it survives
# being pointed at from any workspace OpenCode runs in via OPENCODE_CONFIG —
# see run_opencode_attempt.
OPENCODE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "opencode.json"


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
    opencode_exit_code: int
    commit_sha: str | None  # None if the Attempt never committed anything
    checkpoint_verified: bool  # Entire has a real Checkpoint linked to commit_sha


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
        "The project's dependencies are already installed and its build output is "
        "already in place — do not run `npm install` or `npm ci`; run a single test file "
        "with `npx hardhat test <file>`.\n\n"
        "Always give your file tools (read, edit, write) paths relative to the workspace root, "
        "such as `contracts/Vault.sol`, exactly as the Repo Map writes them. Your shell is Git "
        "Bash and shows paths like `/tmp/...` or `/c/...`; the file tools do not understand "
        "those, so never copy a path from the shell into them.\n\n"
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


def remote_url_for(ground_truth: GroundTruth) -> str:
    return f"https://github.com/{ground_truth.repo}.git"


def clone_isolated_workspace(
    ground_truth: GroundTruth, parent_dir: Path, *, dir_name: str, remote_url: str | None = None
) -> Path:
    """A fresh repository holding `ground_truth`'s repo up to its pre-fix commit, and nothing later.

    Every Attempt gets its own workspace under `parent_dir`, per CONTEXT.md's
    rule that Attempts never share one. GroundTruth is used here only to
    locate the commit — nothing from it is written anywhere a Student Agent
    reads from.

    This is deliberately not `git clone` + `git checkout`. A clone carries the
    whole history and an `origin` remote, so the merged fix sat in the
    Attempt's own repository (measured 2026-10-11: 32 commits past the
    checked-out one reachable from `origin/master`) and an agent with a shell
    could `git log origin/master` its way to the Ground Truth. Fetching
    just the pre-fix commit brings its ancestors and nothing newer, and a
    fetch from a bare URL leaves no remote behind to fetch more from. The
    Judge brings the fix in itself, after the Attempt ends (`judge.fetch_ground_truth`).

    What this bounds is the repository, not the machine: an agent with an
    unrestricted shell can still reach github.com directly. Closing that is a
    matter of what the agent is permitted to run (opencode.json), not of how
    the workspace is built.
    """
    workspace = parent_dir / dir_name
    workspace.mkdir(parents=True)
    url = remote_url or remote_url_for(ground_truth)
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "fetch", "--no-tags", url, ground_truth.pre_fix_commit_sha],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "--detach", ground_truth.pre_fix_commit_sha],
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
    "enable Entire" is explicit in issue #6's "what to build". OpenCode is one
    of Entire's own built-in agent integrations, confirmed live: this one call
    both registers and enables it, with no custom integration (contrast
    ADR-0004's Gemini CLI plugin, which `--agent gemini` needed and this
    doesn't). `--checkpoint-backend branch` is explicit because Entire's
    current default is per-commit git refs, not the `entire/checkpoints/v1`
    branch CONTEXT.md's Checkpoint entry names.
    """
    # shell=True: confirmed live that `entire` (an npm-adjacent-style install,
    # not a plain PATH exe Windows' CreateProcess can launch directly) raises
    # WinError 2 without it, while `git` — a real registered .exe — does not
    # need it. See run_opencode_attempt for the same issue with `opencode`.
    subprocess.run(
        ["entire", "enable", "--agent", "opencode", "--checkpoint-backend", "branch"],
        cwd=workspace,
        check=True,
        capture_output=True,
        shell=True,
    )


def render_repo_map(snapshot: str, *, focus: str, max_chars: int = REPO_MAP_CHARS) -> str:
    """A prompt-sized Repo Map out of `entire graph snapshot`'s JSON-lines output.

    The raw snapshot is every symbol and relation in the repo — 4.5 MB for
    OpenZeppelin, measured 2026-10-11 — so it can neither ride a command line
    nor fit a model's context. This keeps one line per file (`path: symbols`),
    most useful first: files whose path or symbols share a word with `focus`
    (the task's own title), then by how often other code calls or imports
    something in them. Whole files are dropped when the budget runs out, never
    cut mid-line, and a snapshot with nothing in it says so rather than
    rendering "" — an empty map would read as a repo with no structure.
    """
    symbols_by_file: dict[str, list[str]] = {}
    references: Counter[str] = Counter()
    for line in snapshot.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("record_type") == "symbol" and record.get("kind") != "section":
            path, name = record.get("file_path"), record.get("name")
            if path and name:
                symbols_by_file.setdefault(path, []).append(name)
        elif record.get("record_type") == "relation" and record.get("type") in ("CALLS", "IMPORTS"):
            references[str(record.get("to_id", ""))] += 1

    if not symbols_by_file:
        return "(no symbols were indexed for this repo)"

    focus_words = {word for word in re.findall(r"[a-z0-9]{4,}", focus.lower())}

    def relevance(path: str) -> int:
        # A word in the path says what the file is about; one in a symbol name
        # is often generic ("address", "bytes"), so a path match counts triple.
        in_path = path.lower()
        in_symbols = " ".join(symbols_by_file[path]).lower()
        return sum(3 * (word in in_path) + (word in in_symbols) for word in focus_words)

    def referenced(path: str) -> int:
        return sum(count for target, count in references.items() if f":{path}:" in target or target.endswith(f":{path}"))

    ordered = sorted(symbols_by_file, key=lambda path: (-relevance(path), -referenced(path), path))
    lines: list[str] = []
    used = 0
    for path in ordered:
        line = f"{path}: {', '.join(symbols_by_file[path][:REPO_MAP_SYMBOLS_PER_FILE])}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def snapshot_repo(workspace: Path) -> str:
    """Entire's `entire-graph` snapshot of `workspace`: every symbol and relation, as JSON lines.

    Requires `entire` on $PATH with the graph plugin installed
    (`entire plugin install graph`). Raises CalledProcessError if either command
    fails, and TimeoutExpired if one hangs — a missing Repo Map should stop the
    Attempt, not quietly start it with an empty one that reads as "this repo
    has no structure". The timeout is not decoration: with the plugin missing,
    `graph index` once sat for ten minutes inside this harness (CPU idle,
    nothing written) before being killed by hand, where run directly it
    failed in under a second. `graph snapshot` takes about 30 seconds on
    OpenZeppelin, so the cap is generous without being unbounded.

    Raw, because it is computed once per pre-fix commit (`prepare_warm_base`)
    and rendered per Attempt (`render_repo_map`), which is what makes the
    Repo Map free for every Attempt after the first.
    """
    subprocess.run(
        ["entire", "graph", "index"],
        cwd=workspace,
        check=True,
        capture_output=True,
        shell=True,
        timeout=ENTIRE_GRAPH_TIMEOUT_SECONDS,
    )
    snapshot = subprocess.run(
        ["entire", "graph", "snapshot"],
        cwd=workspace,
        check=True,
        capture_output=True,
        shell=True,
        timeout=ENTIRE_GRAPH_TIMEOUT_SECONDS,
    )
    return snapshot.stdout.decode("utf-8", errors="replace")


def ensure_opencode_plugin_dependencies(workspace: Path) -> None:
    """Install OpenCode's own plugin package into `workspace/.opencode`, once.

    A workspace with any plugin in `.opencode/plugins` (Entire's is one) makes OpenCode
    install `@opencode-ai/plugin` there and **wait for it before it starts**. Measured
    2026-10-12 on the OpenZeppelin workspace: with Entire enabled OpenCode was still
    starting after 150 s (4 min 40 s in an earlier run) and without a plugin it took
    ~100 s; with the package pre-installed it started and replied in 107 s. A clean
    `npm install` of the package takes 31 s, so the in-workspace install is where the
    minutes went. Doing it once in the warm base means no Attempt pays for it, and
    Entire's `turn-end` hook (which runs `opencode export`, a second OpenCode start-up)
    stops stalling for the same reason.

    The install is pinned to the installed OpenCode's own version. A `package.json` is
    written first so npm resolves against `.opencode` and not the project's own
    (much larger) `package.json` above it, and the `.gitignore` mirrors the one
    OpenCode writes, so none of this shows up as a change in the workspace.
    """
    opencode_dir = workspace / ".opencode"
    if (opencode_dir / "package-lock.json").is_file() and (opencode_dir / "node_modules").is_dir():
        return
    executable = resolve_opencode_executable(shutil.which("opencode"))
    version = subprocess.run([str(executable), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    opencode_dir.mkdir(exist_ok=True)
    package_json = opencode_dir / "package.json"
    if not package_json.exists():
        package_json.write_text("{}\n", encoding="utf-8")
    gitignore = opencode_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("\n".join(["node_modules", "package.json", "package-lock.json", "bun.lock", ".gitignore"]), encoding="utf-8")
    subprocess.run(
        ["npm", "install", "--save-exact", "--ignore-scripts", "--no-audit", "--no-fund", f"@opencode-ai/plugin@{version}"],
        cwd=opencode_dir,
        check=True,
        capture_output=True,
        shell=True,
        timeout=WARM_BASE_STEP_TIMEOUT_SECONDS,
    )


def prepare_warm_base(ground_truth: GroundTruth, cache_dir: Path) -> WarmBase:
    """The warm base for this Ground Truth's pre-fix commit, built once and reused.

    Built means: fetched without the fix (`clone_isolated_workspace`),
    dependencies installed, the project compiled so every Attempt starts from
    build output rather than a cold compile, and the Repo Map snapshotted. A
    `ready` marker is written last, so a build that died halfway is rebuilt
    rather than trusted. The npm steps are OpenZeppelin's (this project's one
    target repo, see proof_of_skill.DEFAULT_TARGET_REPOS); a second target repo
    would need its own.
    """
    directory = warm_base_dir(cache_dir, ground_truth)
    workspace, snapshot_file, ready = directory / "repo", directory / "snapshot.jsonl", directory / "ready"
    if ready.exists():
        ensure_opencode_plugin_dependencies(workspace)  # a base built before this step existed gets it now
        return WarmBase(path=workspace, snapshot=snapshot_file.read_text(encoding="utf-8"))

    if directory.exists():
        remove_attempt_workspace(directory)  # a half-built base from an interrupted prepare
    clone_isolated_workspace(ground_truth, directory, dir_name="repo")
    for command in (["npm", "ci"], ["npm", "run", "compile"]):
        subprocess.run(
            command,
            cwd=workspace,
            check=True,
            capture_output=True,
            shell=True,
            timeout=WARM_BASE_STEP_TIMEOUT_SECONDS,
            env={**os.environ, "HUSKY": "0"},  # husky's own switch: do not install commit hooks
        )
    disable_commit_hooks(workspace)  # belt and braces: a hook that slipped in would block every commit
    snapshot = snapshot_repo(workspace)
    snapshot_file.write_text(snapshot, encoding="utf-8")
    ensure_opencode_plugin_dependencies(workspace)  # after the snapshot, so the Repo Map never indexes it
    ready.write_text(ground_truth.pre_fix_commit_sha, encoding="utf-8")
    return WarmBase(path=workspace, snapshot=snapshot)


def resolve_opencode_executable(shim: str | None) -> Path:
    """OpenCode's real executable, found from the npm shim on $PATH.

    The prompt is a multi-line argument, and `cmd.exe` — which `shell=True`
    routes it through to reach the `.cmd` shim — cuts a command line at its
    first newline. Measured 2026-10-11: a Student Agent launched that way
    received only `=== Learner Profile: Total Beginner ===` and nothing else,
    so it never saw its Repo Map, its Curriculum notes or the issue, and went
    looking for beginner documentation instead. The npm package ships the real
    `opencode.exe` beside the shim, and launching it directly delivers the
    prompt intact. Raises rather than falling back to `shell=True`: a silent
    fallback would hand back Attempts that look run and were given nothing
    (ADR-0002).
    """
    if shim is None:
        raise FileNotFoundError("opencode is not on PATH")
    executable = Path(shim).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"expected OpenCode's real executable at {executable}")
    return executable


def run_opencode_attempt(workspace: Path, prompt: str) -> int:
    """Run the Attempt as a real, non-interactive OpenCode session.

    Confirmed live, not assumed (see ADR-0005):

    - `--auto` auto-approves tool calls (edits, shell commands) that aren't
      explicitly denied. OpenCode has no equivalent of Gemini CLI's Policy
      Engine shell-command gate that `--auto`/`--yolo` fails to satisfy.
    - `stdin=subprocess.DEVNULL` matters, not just convenience: an inherited
      stdin stops OpenCode from loading Entire's plugin at all, so no
      Checkpoint would ever be captured.
    - `PWD` is set explicitly because OpenCode resolves its project (and
      which plugins, including Entire's, it loads) from `PWD`, not from the
      subprocess's working directory alone.
    - `OPENCODE_CONFIG` points at this repo's `opencode.json`, which declares
      the `litellm` provider (neatHack's gateway) and, critically, a `limit`
      block per model — omitting it makes OpenCode request an output-token
      cap the gateway's models reject outright, confirmed live (Nova Lite's
      hard cap is 10,000; OpenCode defaults to requesting far more without an
      explicit limit).

    Checkpoint capture happens as a side effect of this call, through the
    hooks `entire enable --agent opencode` installed — nothing here talks to
    Entire directly.
    """
    # No shell: see resolve_opencode_executable — `cmd.exe` truncates the
    # multi-line prompt at its first newline.
    exit_code = run_killing_tree(
        [
            str(resolve_opencode_executable(shutil.which("opencode"))),
            "run",
            "--format",
            "json",
            "--dir",
            str(workspace),
            "--auto",
            "-m",
            OPENCODE_MODEL,
            prompt,
        ],
        cwd=workspace,
        timeout_seconds=OPENCODE_TIMEOUT_SECONDS,
        env={**os.environ, "OPENCODE_CONFIG": str(OPENCODE_CONFIG_PATH), "PWD": str(workspace)},
        stdin=subprocess.DEVNULL,
    )
    if exit_code is None:
        # The session and everything it started are already stopped. An Attempt that ran
        # out of time is an Attempt that did not finish, which the Judge grades from
        # whatever it committed (usually nothing) — not a reason to lose the whole Round,
        # and every grade with it.
        return OPENCODE_TIMEOUT_EXIT_CODE
    return exit_code


def committed_sha(workspace: Path, before_sha: str) -> str | None:
    """The workspace's current HEAD if the Attempt committed, else None."""
    after = current_head(workspace)
    return after if after != before_sha else None


def verify_checkpoint(workspace: Path, commit_sha: str) -> bool:
    """Whether Entire has a real Checkpoint linked to `commit_sha`.

    This is the acceptance criterion in issue #6 that makes a Checkpoint "real"
    rather than merely attempted. Goes through `entire checkpoint explain`
    rather than reading `entire/checkpoints/v1` directly: confirmed live that a
    Checkpoint's commit on that branch names the Checkpoint's own ID
    ("Checkpoint: a637ea51c4cd"), not the original commit's SHA, so grepping
    the branch for `commit_sha` would never match — `entire checkpoint
    explain` is Entire's own authoritative answer to "does this commit have
    one", not a guess at its storage format. False (not an exception) on any
    failure, including no Checkpoint existing yet: that is itself a legitimate
    "not verified" answer the caller needs to see, not a crash.
    """
    result = subprocess.run(
        ["entire", "checkpoint", "explain", commit_sha],
        cwd=workspace,
        capture_output=True,
        shell=True,
    )
    return result.returncode == 0


@neatlogs.span(kind="AGENT", role="Student")
def run_attempt(
    module: Module,
    profile: LearnerProfile,
    chunks: Sequence[Chunk],
    base: WarmBase,
    *,
    workspace_root: Path,
) -> AttemptResult:
    """One full Attempt: its own copy of the warm base, bounded prompt, real session.

    Takes no GroundTruth. The workspace comes from `base`, which was built
    without the fix, so nothing on this path can reach one.
    """
    started = time.monotonic()
    dir_name = attempt_workspace_name(module.topic, profile, token=uuid.uuid4().hex)
    workspace = build_attempt_workspace(base, workspace_root, dir_name=dir_name)
    copied = time.monotonic()
    before_sha = current_head(workspace)
    enable_entire(workspace)
    enabled = time.monotonic()

    repo_map = render_repo_map(base.snapshot, focus=module.proof_of_skill_task.title)
    prompt = build_attempt_prompt(module, profile, chunks, repo_map)
    prepared = time.monotonic()
    # Printed because the deadline below is what an Attempt is held to and everything
    # before it is overhead: one run spent ~4 minutes here for a reason nobody could see.
    print(
        f"setup: copy {copied - started:.0f}s, entire enable {enabled - copied:.0f}s, "
        f"repo map + prompt {prepared - enabled:.0f}s",
        flush=True,
    )
    exit_code = run_opencode_attempt(workspace, prompt)
    print(f"opencode session: {time.monotonic() - prepared:.0f}s", flush=True)

    commit_sha = committed_sha(workspace, before_sha)
    return AttemptResult(
        profile=profile,
        module_topic=module.topic,
        workspace=workspace,
        opencode_exit_code=exit_code,
        commit_sha=commit_sha,
        checkpoint_verified=commit_sha is not None and verify_checkpoint(workspace, commit_sha),
    )


# This function is the WORKFLOW root for every `retrieve` call and `run_attempt`
# (an AGENT span) below it. Without it those spans would arrive in Neatlogs
# parented to nothing, once run_curriculum's own WORKFLOW span has closed — see
# retrieval.py's identical note on why its own smoke test needs one.
@neatlogs.span(kind="WORKFLOW", name="student_agent_attempts")
def _run_attempts_for_every_profile(
    module: Module, curriculum: Curriculum, base: WarmBase, workspace_root: Path
) -> list[AttemptResult]:
    results = []
    for profile in MVP_LEARNER_PROFILES:
        chunks = retrieve(curriculum, module.proof_of_skill_task.title, RETRIEVAL_K)
        print(f"\n--- Attempt: {module.topic} / {profile.name} ---")
        result = run_attempt(module, profile, chunks, base, workspace_root=workspace_root)
        print(f"opencode exit code: {result.opencode_exit_code}")
        print(f"committed: {result.commit_sha or '(no commit)'}")
        print(f"checkpoint verified on entire/checkpoints/v1: {result.checkpoint_verified}")
        discard_attempt_workspace(result.workspace)
        results.append(result)
    return results


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        frozen = load_or_build_frozen_run(FROZEN_RUN_PATH)
        curriculum = frozen.curriculum
        module = next((m for m in curriculum.modules if m.proof_of_skill_task is not None), None)
        if module is None:
            print("No module carries a Proof-of-Skill Task; nothing to attempt.")
            return
        ground_truth = frozen.assignment.ground_truths[module.proof_of_skill_task.key]

        base = prepare_warm_base(ground_truth, WARM_CACHE_DIR)
        with tempfile.TemporaryDirectory(prefix="pareto-attempts-") as workspace_root:
            _run_attempts_for_every_profile(module, curriculum, base, Path(workspace_root))
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
