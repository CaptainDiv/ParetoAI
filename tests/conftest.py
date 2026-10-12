import subprocess

import pytest

from pareto_ai.proof_of_skill import GroundTruth


def run_git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def git():
    return run_git


@pytest.fixture
def upstream(tmp_path):
    """A real repo: a pre-fix commit, the fix on top of it, and a later commit."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    run_git(repo, "init", "-b", "main")
    run_git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    shas = {}
    for name in ("pre_fix", "fix", "later"):
        (repo / "Vault.sol").write_text(name)
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-m", name)
        shas[name] = run_git(repo, "rev-parse", "HEAD")
    return repo, shas


@pytest.fixture
def ground_truth(upstream):
    _, shas = upstream
    return GroundTruth(
        repo="owner/repo",
        issue_number=1,
        pull_number=2,
        pull_title="fix",
        pull_body="the fix",
        merge_commit_sha=shas["fix"],
        pre_fix_commit_sha=shas["pre_fix"],
        url="https://example.com/pull/2",
    )
