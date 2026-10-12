from dataclasses import fields
import json
import subprocess

import pytest

from pareto_ai.curriculum import Module, Resource
from pareto_ai.proof_of_skill import GroundTruth, ProofOfSkillTask
from pareto_ai.retrieval import Chunk
from pareto_ai.student_agent import (
    SOME_IT_BACKGROUND,
    TOTAL_BEGINNER,
    LearnerProfile,
    OPENCODE_TIMEOUT_SECONDS,
    attempt_workspace_name,
    build_attempt_prompt,
    clone_isolated_workspace,
    render_repo_map,
    resolve_opencode_executable,
    run_opencode_attempt,
)

TASK = ProofOfSkillTask(
    repo="OpenZeppelin/openzeppelin-contracts",
    issue_number=42,
    title="Reentrancy guard missing on withdraw",
    body="Calling withdraw twice before the first call settles drains the contract.",
    url="https://github.com/OpenZeppelin/openzeppelin-contracts/issues/42",
)

MODULE = Module(
    topic="reentrancy guards",
    resources=(Resource(title="Reentrancy Attacks", url="https://example.com/reentrancy"),),
    prerequisites=(),
    deferred_resources=(),
    proof_of_skill_task=TASK,
)

CHUNKS = (
    Chunk(module_topic="reentrancy guards", text="reentrancy guards\nresource: Reentrancy Attacks"),
    Chunk(module_topic="solidity syntax", text="solidity syntax\nresource: Solidity Docs"),
)

REPO_MAP = "withdraw() in Vault.sol calls token.transfer() at line 42"


# --- LearnerProfile ------------------------------------------------------------


def test_the_two_mvp_profiles_have_distinct_names():
    assert TOTAL_BEGINNER.name != SOME_IT_BACKGROUND.name


# --- build_attempt_prompt -------------------------------------------------------


def test_the_prompt_names_the_proof_of_skill_task():
    prompt = build_attempt_prompt(MODULE, TOTAL_BEGINNER, CHUNKS, REPO_MAP)
    assert TASK.title in prompt
    assert TASK.body in prompt
    assert TASK.url in prompt


def test_the_prompt_carries_the_repo_map():
    prompt = build_attempt_prompt(MODULE, TOTAL_BEGINNER, CHUNKS, REPO_MAP)
    assert REPO_MAP in prompt


def test_the_prompt_carries_every_retrieved_chunk():
    prompt = build_attempt_prompt(MODULE, TOTAL_BEGINNER, CHUNKS, REPO_MAP)
    for chunk in CHUNKS:
        assert chunk.text in prompt


def test_the_prompt_mentions_the_learner_profile_by_name():
    prompt = build_attempt_prompt(MODULE, SOME_IT_BACKGROUND, CHUNKS, REPO_MAP)
    assert SOME_IT_BACKGROUND.name in prompt


def test_different_profiles_produce_different_prompts_for_the_same_module():
    beginner_prompt = build_attempt_prompt(MODULE, TOTAL_BEGINNER, CHUNKS, REPO_MAP)
    some_it_prompt = build_attempt_prompt(MODULE, SOME_IT_BACKGROUND, CHUNKS, REPO_MAP)
    assert beginner_prompt != some_it_prompt


def test_the_prompt_instructs_the_agent_to_commit_its_work():
    prompt = build_attempt_prompt(MODULE, TOTAL_BEGINNER, CHUNKS, REPO_MAP)
    assert "commit" in prompt.lower()


def test_a_module_with_no_task_cannot_build_a_prompt():
    bare_module = MODULE.__class__(
        topic=MODULE.topic,
        resources=MODULE.resources,
        prerequisites=MODULE.prerequisites,
        deferred_resources=MODULE.deferred_resources,
        proof_of_skill_task=None,
    )
    try:
        build_attempt_prompt(bare_module, TOTAL_BEGINNER, CHUNKS, REPO_MAP)
        assert False, "expected a ValueError for a module with no assigned task"
    except ValueError:
        pass


def test_build_attempt_prompt_has_no_parameter_that_could_carry_a_ground_truth():
    # Structural guarantee, mirroring proof_of_skill's own: nothing about this
    # function's signature can reach a GroundTruth, so a caller cannot leak the
    # fix into a Student Agent's context even by accident.
    import inspect

    parameters = inspect.signature(build_attempt_prompt).parameters
    ground_truth_field_names = {field.name for field in fields(GroundTruth)}
    assert not (set(parameters) & ground_truth_field_names)


# --- attempt_workspace_name -----------------------------------------------------


def test_workspace_names_differ_by_module_and_profile():
    a = attempt_workspace_name("reentrancy guards", TOTAL_BEGINNER, token="abc123")
    b = attempt_workspace_name("solidity syntax", TOTAL_BEGINNER, token="abc123")
    c = attempt_workspace_name("reentrancy guards", SOME_IT_BACKGROUND, token="abc123")
    assert len({a, b, c}) == 3


def test_a_workspace_name_is_filesystem_safe():
    name = attempt_workspace_name("re-entrancy / guards!!", TOTAL_BEGINNER, token="abc123")
    assert all(c.isalnum() or c in "-_" for c in name)


def test_the_same_inputs_and_token_give_the_same_name():
    first = attempt_workspace_name("reentrancy guards", TOTAL_BEGINNER, token="xyz")
    second = attempt_workspace_name("reentrancy guards", TOTAL_BEGINNER, token="xyz")
    assert first == second


# --- render_repo_map -------------------------------------------------------------


def _symbol(path: str, name: str, kind: str = "function") -> str:
    return json.dumps(
        {"record_type": "symbol", "id": f"{path}:{kind}:{name}", "kind": kind, "name": name, "file_path": path}
    )


def _calls(target_path: str, target_name: str, times: int = 1) -> list[str]:
    return [
        json.dumps({"record_type": "relation", "type": "CALLS", "from_id": f"caller{i}", "to_id": f"{target_path}:function:{target_name}"})
        for i in range(times)
    ]


def test_a_repo_map_is_never_longer_than_its_budget():
    snapshot = "\n".join(_symbol(f"contracts/File{i}.sol", f"symbol{i}") for i in range(500))

    assert len(render_repo_map(snapshot, focus="", max_chars=400)) <= 400


def test_a_repo_map_lists_a_file_with_its_symbols():
    snapshot = "\n".join([_symbol("contracts/Vault.sol", "withdraw"), _symbol("contracts/Vault.sol", "deposit")])

    rendered = render_repo_map(snapshot, focus="", max_chars=1000)

    assert "contracts/Vault.sol" in rendered
    assert "withdraw" in rendered and "deposit" in rendered


def test_files_relevant_to_the_task_come_before_unrelated_ones():
    snapshot = "\n".join([_symbol("contracts/Alpha.sol", "noop"), _symbol("contracts/ReentrancyGuard.sol", "nonReentrant")])

    rendered = render_repo_map(snapshot, focus="Reentrancy guard missing on withdraw", max_chars=1000)

    assert rendered.index("ReentrancyGuard.sol") < rendered.index("Alpha.sol")


def test_among_equally_relevant_files_the_more_referenced_comes_first():
    snapshot = "\n".join(
        [_symbol("contracts/Rare.sol", "rare"), _symbol("contracts/Popular.sol", "popular")] + _calls("contracts/Popular.sol", "popular", 3)
    )

    rendered = render_repo_map(snapshot, focus="", max_chars=1000)

    assert rendered.index("Popular.sol") < rendered.index("Rare.sol")


def test_a_budget_that_runs_out_drops_whole_files_not_half_a_line():
    snapshot = "\n".join(_symbol(f"contracts/File{i}.sol", f"symbol{i}") for i in range(50))

    rendered = render_repo_map(snapshot, focus="", max_chars=200)

    for line in rendered.splitlines():
        assert line.startswith("contracts/File") and "symbol" in line


def test_config_sections_are_not_listed_as_symbols():
    snapshot = "\n".join([_symbol("package.json", "scripts", kind="section"), _symbol("contracts/Vault.sol", "withdraw")])

    assert "scripts" not in render_repo_map(snapshot, focus="", max_chars=1000)


def test_malformed_lines_are_skipped():
    snapshot = "\n".join(["not json at all", "{broken", _symbol("contracts/Vault.sol", "withdraw")])

    assert "withdraw" in render_repo_map(snapshot, focus="", max_chars=1000)


def test_a_snapshot_with_nothing_to_list_says_so_rather_than_reading_as_an_empty_repo():
    assert "no symbols" in render_repo_map("", focus="", max_chars=1000).lower()


# --- resolve_opencode_executable --------------------------------------------------


def _npm_install(tmp_path, *, with_exe: bool):
    shim = tmp_path / "opencode.CMD"
    shim.write_text("@echo off")
    bin_dir = tmp_path / "node_modules" / "opencode-ai" / "bin"
    bin_dir.mkdir(parents=True)
    if with_exe:
        (bin_dir / "opencode.exe").write_text("")
    return shim


def test_the_real_executable_is_found_beside_the_npm_shim(tmp_path):
    shim = _npm_install(tmp_path, with_exe=True)

    assert resolve_opencode_executable(str(shim)) == tmp_path / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"


def test_an_install_with_no_real_executable_is_refused_not_guessed_at(tmp_path):
    shim = _npm_install(tmp_path, with_exe=False)

    with pytest.raises(FileNotFoundError):
        resolve_opencode_executable(str(shim))


def test_opencode_missing_from_the_path_is_refused():
    with pytest.raises(FileNotFoundError):
        resolve_opencode_executable(None)


# --- run_opencode_attempt ---------------------------------------------------------


def _fake_opencode_install(tmp_path, monkeypatch):
    shim = tmp_path / "opencode.CMD"
    shim.write_text("")
    bin_dir = tmp_path / "node_modules" / "opencode-ai" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "opencode.exe").write_text("")
    monkeypatch.setattr("pareto_ai.student_agent.shutil.which", lambda name: str(shim))


def test_a_session_that_outruns_its_timeout_is_an_attempt_that_failed_not_a_crash(tmp_path, monkeypatch):
    _fake_opencode_install(tmp_path, monkeypatch)
    monkeypatch.setattr("pareto_ai.student_agent.run_killing_tree", lambda *a, **k: None)

    assert run_opencode_attempt(tmp_path, "prompt") != 0


def test_a_session_that_finishes_reports_its_own_exit_code(tmp_path, monkeypatch):
    _fake_opencode_install(tmp_path, monkeypatch)
    monkeypatch.setattr("pareto_ai.student_agent.run_killing_tree", lambda *a, **k: 7)

    assert run_opencode_attempt(tmp_path, "prompt") == 7


def test_the_session_is_given_the_attempt_deadline_and_the_whole_prompt(tmp_path, monkeypatch):
    _fake_opencode_install(tmp_path, monkeypatch)
    seen = {}

    def capture(args, *, cwd, timeout_seconds, **kwargs):
        seen.update(args=list(args), timeout=timeout_seconds)
        return 0

    monkeypatch.setattr("pareto_ai.student_agent.run_killing_tree", capture)
    run_opencode_attempt(tmp_path, "line one" + chr(10) + "line two")

    assert seen["timeout"] == OPENCODE_TIMEOUT_SECONDS
    assert seen["args"][-1] == "line one" + chr(10) + "line two"


def test_an_attempt_is_held_short_enough_for_a_whole_cycle_to_fit_in_about_six_minutes():
    assert OPENCODE_TIMEOUT_SECONDS <= 5 * 60


def test_the_prompt_says_dependencies_are_installed_so_the_agent_does_not_reinstall_them():
    prompt = build_attempt_prompt(MODULE, TOTAL_BEGINNER, CHUNKS, REPO_MAP)

    assert "already installed" in prompt


def test_the_prompt_tells_the_agent_to_use_workspace_relative_paths_for_its_file_tools():
    """Its shell shows Git Bash paths (/tmp/...), its file tools need Windows ones: relative paths work in both."""
    prompt = build_attempt_prompt(MODULE, TOTAL_BEGINNER, CHUNKS, REPO_MAP)

    assert "relative to the workspace root" in prompt
    assert "/tmp/" in prompt  # names the form it must not copy from its shell
