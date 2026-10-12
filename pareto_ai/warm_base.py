"""A warm base: one prepared copy of a repo at its pre-fix commit, reused by every Attempt.

An Attempt used to start from nothing: fetch the repo, then `npm install` and a
cold compile, all inside the Student Agent's own time budget — so a run spent
most of its deadline on setup and a weaker run on none of the task. Measured on
2026-10-11, OpenCode's session ran 20+ minutes with the agent still installing
dependencies and downloading a compiler. Preparing once per pre-fix commit and
copying makes setup a one-off cost rather than a per-Attempt one.

Isolation is the thing to keep straight. Each Attempt gets its **own copy** of the
repository, `.git` included, because the Judge later fetches the Ground Truth into
it (`judge.fetch_ground_truth`) and an object store shared between Attempts would
show one Attempt the fix another's Judge brought in. Only `node_modules` is shared,
as a link back to the base: it is hundreds of megabytes of dependencies nothing in
an Attempt is meant to change, and copying it would put the setup cost straight
back. The cost of sharing it is that a command which rewrites `node_modules` (an
`npm ci`) would rewrite the base's, which is why the Attempt's prompt says
dependencies are already installed and the Judge skips its own install when they
are.

This module is the filesystem half: where a base lives, and cloning an Attempt out
of one. Building a base needs git, npm and entire, so it lives with them in
`student_agent.prepare_warm_base`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pareto_ai.proof_of_skill import GroundTruth

NODE_MODULES = "node_modules"
# Read-only dependency trees, linked into every workspace rather than copied: the
# project's own, and OpenCode's plugin package under `.opencode`. Copying the second
# (61 MB of small files) took a workspace from ~30 s to 126 s, measured 2026-10-12.
_LINKED_DEPENDENCIES = (Path(NODE_MODULES), Path(".opencode") / NODE_MODULES)


def _LINKED_PARENTS(root: Path) -> tuple[Path, ...]:
    return tuple(root / relative.parent for relative in _LINKED_DEPENDENCIES)


# Gitignored: bases are hundreds of megabytes and rebuilt on demand.
WARM_CACHE_DIR = Path(__file__).resolve().parent.parent / ".warm"


@dataclass(frozen=True)
class WarmBase:
    path: Path  # the prepared repository
    snapshot: str  # `entire graph snapshot` output for it, rendered per Attempt by render_repo_map


def warm_base_dir(cache_dir: Path, ground_truth: GroundTruth) -> Path:
    """Where the base for this Ground Truth's pre-fix commit lives: one directory per commit."""
    return cache_dir / "base" / f"{ground_truth.repo.replace('/', '__')}-{ground_truth.pre_fix_commit_sha[:12]}"


def link_directory(link: Path, target: Path) -> None:
    """Make `link` another way into `target`, without copying it.

    A junction on Windows, because a symlink there needs a privilege most
    accounts lack; a junction needs none. `shutil.rmtree` removes either without
    descending into the target (Python 3.8+ on Windows), which a test pins.
    """
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


def build_attempt_workspace(base: WarmBase, parent_dir: Path, *, dir_name: str) -> Path:
    """A fresh workspace for one Attempt: its own copy of the base, sharing only `node_modules`."""
    workspace = parent_dir / dir_name
    parent_dir.mkdir(parents=True, exist_ok=True)
    # Only the project's own top-level node_modules is linked rather than copied. A name
    # pattern would also skip `.opencode/node_modules`, OpenCode's own plugin
    # dependencies, which are small, must be present, and must be this workspace's own.
    shutil.copytree(
        base.path,
        workspace,
        ignore=lambda directory, names: [NODE_MODULES] if Path(directory) in _LINKED_PARENTS(base.path) and NODE_MODULES in names else [],
    )
    for relative in _LINKED_DEPENDENCIES:
        if (base.path / relative).is_dir():
            link_directory(workspace / relative, base.path / relative)
    retarget_compile_cache(workspace, base.path)
    return workspace


def retarget_compile_cache(workspace: Path, base_path: Path) -> None:
    """Point a copied workspace's Hardhat compile cache at the workspace, not at the base.

    Hardhat keys `cache/solidity-files-cache.json` by each source file's **absolute
    path**, so a copy at a different path misses on every one of its files and
    recompiles the whole project. Measured 2026-10-12: a real Attempt's first
    `npx hardhat test` was still compiling, solc at 94+ CPU-seconds, when its 8-minute
    deadline killed it, and the Judge took 140-160 seconds per grade for the same
    reason. The copy's file contents and modification times are the base's, so the
    cache is right except for those paths. Rewriting them (JSON-escaped, since the
    paths are stored as JSON strings) makes the build output reusable.

    Rewrites the workspace's copy only; the base's own cache is never touched.
    """
    cache = workspace / "cache" / "solidity-files-cache.json"
    if not cache.is_file():
        return
    old, new = json.dumps(str(base_path))[1:-1], json.dumps(str(workspace))[1:-1]
    cache.write_text(cache.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def _is_link(path: Path) -> bool:
    if sys.platform == "win32":
        try:
            return bool(os.stat(path, follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False
    return path.is_symlink()


def _make_writable_and_retry(function, path, _excinfo) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_attempt_workspace(workspace: Path) -> None:
    """Delete an Attempt's workspace without ever touching the base it links into.

    The `node_modules` link is removed on its own first, so nothing below can walk
    through it into the base's dependencies. The rest needs a read-only-aware
    delete: git writes its object files read-only, and Windows refuses to delete
    those, so a plain `rmtree` (or one that ignores errors) leaves the workspace
    half-removed.
    """
    for relative in _LINKED_DEPENDENCIES:
        link = workspace / relative
        if _is_link(link):
            os.rmdir(link) if sys.platform == "win32" else os.unlink(link)
    shutil.rmtree(workspace, onerror=_make_writable_and_retry)


def disable_commit_hooks(repo: Path) -> None:
    """Stop the project's own commit hooks from running in `repo`.

    `npm ci` runs OpenZeppelin's `prepare` script, which installs husky, which
    points `core.hooksPath` at a `pre-commit` that runs `scripts/checks/generation.sh`
    and `lint-staged`. On Windows that hook fails before the commit is made
    ("'scripts' is not recognized as an internal or external command", reproduced
    2026-10-12), so **no Attempt could ever commit**, whatever its model did — and an
    Attempt that cannot commit grades as "never committed", indistinguishable from
    one that never tried. The hooks are the project's contributor checks, not what
    an Attempt is graded on (the Ground Truth's regression test is), so they are off.
    """
    # Exit code 5 is "no such key": a repo that never had hooks is already fine.
    subprocess.run(["git", "config", "--unset", "core.hooksPath"], cwd=repo, capture_output=True)


def discard_attempt_workspace(workspace: Path) -> bool:
    """Best-effort `remove_attempt_workspace`, for housekeeping after a result already exists.

    A build the Attempt left running can still hold files in the workspace, and a
    delete that raises there would throw away a grade that has already been computed
    (a real run lost its Judge result exactly this way, 2026-10-12). Leaving a temp
    directory behind costs disk; losing the grade costs the run. Says so rather than
    hiding it.
    """
    try:
        remove_attempt_workspace(workspace)
        return True
    except OSError as error:
        print(f"warning: could not delete {workspace} ({error.__class__.__name__}); left in place")
        return False
