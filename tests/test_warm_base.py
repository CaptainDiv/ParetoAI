import json
import os
import subprocess

import pytest

from pareto_ai.warm_base import (
    WarmBase,
    build_attempt_workspace,
    disable_commit_hooks,
    discard_attempt_workspace,
    remove_attempt_workspace,
    warm_base_dir,
)


def _base(tmp_path, git, *, with_node_modules=True):
    base = tmp_path / "base"
    base.mkdir()
    git(base, "init", "-b", "main")
    (base / "Vault.sol").write_text("contract Vault {}")
    (base / "artifacts").mkdir()
    (base / "artifacts" / "built.json").write_text("{}")
    git(base, "add", ".")
    git(base, "commit", "-m", "pre-fix")
    if with_node_modules:
        (base / "node_modules" / "pkg").mkdir(parents=True)
        (base / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1")
    return base


def _workspace(tmp_path, base, name="a1"):
    return build_attempt_workspace(WarmBase(path=base, snapshot=""), tmp_path / "attempts", dir_name=name)


def test_an_attempt_workspace_starts_with_the_bases_files_and_build_output(tmp_path, git):
    workspace = _workspace(tmp_path, _base(tmp_path, git))

    assert (workspace / "Vault.sol").read_text() == "contract Vault {}"
    assert (workspace / "artifacts" / "built.json").exists()


def test_installed_dependencies_are_reachable_without_being_copied(tmp_path, git):
    base = _base(tmp_path, git)
    workspace = _workspace(tmp_path, base)

    assert (workspace / "node_modules" / "pkg" / "index.js").read_text() == "module.exports = 1"
    assert os.path.realpath(workspace / "node_modules") == os.path.realpath(base / "node_modules")


def test_removing_an_attempts_workspace_removes_it_all_and_leaves_the_bases_dependencies_intact(tmp_path, git):
    base = _base(tmp_path, git)
    workspace = _workspace(tmp_path, base)

    remove_attempt_workspace(workspace)

    assert (base / "node_modules" / "pkg" / "index.js").exists()
    assert not workspace.exists()


def test_an_attempts_commits_never_reach_the_base_or_another_attempt(tmp_path, git):
    base = _base(tmp_path, git)
    first, second = _workspace(tmp_path, base, "a1"), _workspace(tmp_path, base, "a2")

    (first / "Vault.sol").write_text("changed")
    git(first, "commit", "-am", "attempt 1")

    assert git(base, "rev-list", "--all", "--count") == "1"
    assert git(second, "rev-list", "--all", "--count") == "1"
    assert (second / "Vault.sol").read_text() == "contract Vault {}"


def test_a_base_with_no_dependencies_gives_a_workspace_with_none(tmp_path, git):
    workspace = _workspace(tmp_path, _base(tmp_path, git, with_node_modules=False))

    assert not (workspace / "node_modules").exists()


def test_two_ground_truths_do_not_share_a_base_directory(tmp_path, ground_truth):
    from dataclasses import replace

    other = replace(ground_truth, pre_fix_commit_sha="f" * 40)

    assert warm_base_dir(tmp_path, ground_truth) != warm_base_dir(tmp_path, other)


# --- commit hooks ---------------------------------------------------------------


def _repo_whose_commit_hook_always_fails(tmp_path, git):
    """The shape of OpenZeppelin's husky setup: core.hooksPath pointing at a pre-commit that fails."""
    repo = tmp_path / "hooked"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / ".husky").mkdir()
    (repo / ".husky" / "pre-commit").write_text("#!/bin/sh" + chr(10) + "exit 1" + chr(10))
    git(repo, "config", "core.hooksPath", ".husky")
    (repo / "a.txt").write_text("x")
    git(repo, "add", ".")
    return repo


def test_a_failing_commit_hook_blocks_every_commit_which_is_why_hooks_must_be_turned_off(tmp_path, git):
    repo = _repo_whose_commit_hook_always_fails(tmp_path, git)

    with pytest.raises(subprocess.CalledProcessError):
        git(repo, "commit", "-m", "an attempt's fix")


def test_with_commit_hooks_disabled_an_attempt_can_commit(tmp_path, git):
    repo = _repo_whose_commit_hook_always_fails(tmp_path, git)

    disable_commit_hooks(repo)
    git(repo, "commit", "-m", "an attempt's fix")

    assert git(repo, "rev-list", "--count", "HEAD") == "1"


def test_disabling_hooks_in_a_repo_that_has_none_is_harmless(tmp_path, git):
    repo = tmp_path / "plain"
    repo.mkdir()
    git(repo, "init", "-b", "main")

    disable_commit_hooks(repo)


# --- the compile cache is keyed by absolute path ------------------------------------


def _base_with_compile_cache(tmp_path, git):
    base = _base(tmp_path, git)
    (base / "cache").mkdir()
    entry = {"lastModificationDate": 1, "contentHash": "abc", "sourceName": "contracts/Vault.sol"}
    (base / "cache" / "solidity-files-cache.json").write_text(
        json.dumps({"_format": "hh-sol-cache-2", "files": {str(base / "contracts" / "Vault.sol"): entry}})
    )
    return base


def test_a_copied_workspaces_compile_cache_points_at_the_workspace_not_the_base(tmp_path, git):
    base = _base_with_compile_cache(tmp_path, git)
    workspace = _workspace(tmp_path, base)

    cached = json.loads((workspace / "cache" / "solidity-files-cache.json").read_text())["files"]

    assert list(cached) == [str(workspace / "contracts" / "Vault.sol")]


def test_building_a_workspace_leaves_the_bases_own_compile_cache_alone(tmp_path, git):
    base = _base_with_compile_cache(tmp_path, git)
    _workspace(tmp_path, base)

    cached = json.loads((base / "cache" / "solidity-files-cache.json").read_text())["files"]

    assert list(cached) == [str(base / "contracts" / "Vault.sol")]


def test_a_base_that_has_never_compiled_has_no_cache_to_retarget(tmp_path, git):
    workspace = _workspace(tmp_path, _base(tmp_path, git))

    assert not (workspace / "cache").exists()


def test_a_workspace_that_cannot_be_deleted_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    def locked(*args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr("pareto_ai.warm_base.remove_attempt_workspace", locked)

    assert discard_attempt_workspace(tmp_path) is False
    assert "could not delete" in capsys.readouterr().out


def test_a_workspace_that_can_be_deleted_is_deleted(tmp_path, git):
    workspace = _workspace(tmp_path, _base(tmp_path, git))

    assert discard_attempt_workspace(workspace) is True
    assert not workspace.exists()


# --- OpenCode's own plugin dependencies live in .opencode/node_modules --------------


def test_opencodes_plugin_dependencies_are_copied_into_every_attempt_workspace(tmp_path, git):
    """Without them OpenCode installs 26 packages on start-up inside the Attempt's own time budget."""
    base = _base(tmp_path, git)
    (base / ".opencode" / "node_modules" / "@opencode-ai" / "plugin").mkdir(parents=True)
    (base / ".opencode" / "node_modules" / "@opencode-ai" / "plugin" / "index.js").write_text("module.exports = 1")
    (base / ".opencode" / "package-lock.json").write_text("{}")

    workspace = _workspace(tmp_path, base)

    assert (workspace / ".opencode" / "node_modules" / "@opencode-ai" / "plugin" / "index.js").exists()
    assert (workspace / ".opencode" / "package-lock.json").exists()


def test_those_dependencies_are_linked_not_copied_because_copying_them_cost_over_a_minute_per_attempt(tmp_path, git):
    """Measured 2026-10-12: adding the 61 MB of small files to each copy took a workspace from ~30 s to 126 s."""
    base = _base(tmp_path, git)
    (base / ".opencode" / "node_modules" / "pkg").mkdir(parents=True)
    (base / ".opencode" / "node_modules" / "pkg" / "index.js").write_text("x")

    workspace = _workspace(tmp_path, base)

    assert os.path.realpath(workspace / ".opencode" / "node_modules") == os.path.realpath(base / ".opencode" / "node_modules")


def test_removing_a_workspace_leaves_the_bases_opencode_dependencies_intact(tmp_path, git):
    base = _base(tmp_path, git)
    (base / ".opencode" / "node_modules" / "pkg").mkdir(parents=True)
    (base / ".opencode" / "node_modules" / "pkg" / "index.js").write_text("x")
    workspace = _workspace(tmp_path, base)

    remove_attempt_workspace(workspace)

    assert (base / ".opencode" / "node_modules" / "pkg" / "index.js").exists()
    assert not workspace.exists()


def test_the_projects_own_node_modules_is_still_linked_not_copied(tmp_path, git):
    base = _base(tmp_path, git)
    (base / ".opencode" / "node_modules" / "pkg").mkdir(parents=True)

    workspace = _workspace(tmp_path, base)

    assert os.path.realpath(workspace / "node_modules") == os.path.realpath(base / "node_modules")
