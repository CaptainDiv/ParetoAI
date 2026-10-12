import pytest

from pareto_ai.curriculum import Gap
from pareto_ai.student_agent import clone_isolated_workspace
from pareto_ai.judge import (
    JudgeGradingError,
    _graded,
    fetch_ground_truth,
    grade_gaps,
    is_test_file,
    needs_install,
    parse_gaps,
    run_test_suite,
    select_test_files,
    regression_test_command,
)

KNOWN_TOPICS = ("reentrancy guards", "gas optimization")


# --- is_test_file / select_test_files ----------------------------------------


def test_a_path_under_test_dir_is_a_test_file():
    assert is_test_file("test/Vault.test.js")


def test_a_path_under_tests_dir_is_a_test_file():
    assert is_test_file("tests/vault_test.sol")


def test_a_foundry_style_suffix_is_a_test_file():
    assert is_test_file("contracts/Vault.t.sol")


def test_a_contract_source_file_is_not_a_test_file():
    assert not is_test_file("contracts/Vault.sol")


def test_filters_a_mixed_changed_file_list_down_to_test_files():
    changed = ("contracts/Vault.sol", "test/Vault.test.js", "docs/README.md")

    assert select_test_files(changed) == ("test/Vault.test.js",)


def test_an_all_source_change_yields_no_test_files():
    assert select_test_files(("contracts/Vault.sol", "contracts/Token.sol")) == ()


# --- parse_gaps --------------------------------------------------------------


def test_parses_a_gap_mapped_to_a_known_module():
    content = '{"gaps": [{"description": "missed the reentrancy check", "module_topic": "reentrancy guards"}]}'

    gaps = parse_gaps(content, "stop", KNOWN_TOPICS)

    assert gaps == (Gap(description="missed the reentrancy check", module_topic="reentrancy guards"),)


def test_an_unknown_module_topic_is_flagged_rather_than_hallucinated():
    content = '{"gaps": [{"description": "off-topic gap", "module_topic": "a module not in this curriculum"}]}'

    gaps = parse_gaps(content, "stop", KNOWN_TOPICS)

    assert gaps == (Gap(description="off-topic gap", module_topic=None),)


def test_a_null_module_topic_stays_unmapped():
    content = '{"gaps": [{"description": "not covered by any module", "module_topic": null}]}'

    gaps = parse_gaps(content, "stop", KNOWN_TOPICS)

    assert gaps == (Gap(description="not covered by any module", module_topic=None),)


def test_an_empty_gaps_list_parses_as_no_gaps():
    assert parse_gaps('{"gaps": []}', "stop", KNOWN_TOPICS) == ()


def test_drops_gap_items_with_no_description():
    content = '{"gaps": [{"module_topic": "gas optimization"}]}'

    assert parse_gaps(content, "stop", KNOWN_TOPICS) == ()


def test_a_truncated_response_raises_rather_than_reading_as_no_gaps():
    with pytest.raises(JudgeGradingError):
        parse_gaps("", "length", KNOWN_TOPICS)


def test_a_response_with_no_gaps_key_raises():
    with pytest.raises(JudgeGradingError):
        parse_gaps('{"something_else": []}', "stop", KNOWN_TOPICS)


def test_unparseable_content_raises():
    with pytest.raises(JudgeGradingError):
        parse_gaps("not json at all", "stop", KNOWN_TOPICS)


# --- _graded: one retry, then give up loudly --------------------------------


def test_a_rubric_call_retries_once_when_the_model_answers_unusably():
    responses = iter([("thinking out loud, no JSON", "stop"), ('{"gaps": []}', "stop")])

    gaps = _graded("prompt", KNOWN_TOPICS, ask=lambda prompt: next(responses))

    assert gaps == ()


def test_a_rubric_call_that_fails_twice_raises_rather_than_grading_nothing():
    with pytest.raises(JudgeGradingError):
        _graded("prompt", KNOWN_TOPICS, ask=lambda prompt: ("", "length"))


# --- grade_gaps: skips the model entirely when there is nothing to compare --


def test_an_attempt_with_no_diff_gets_a_gap_with_no_model_call():
    # No `ask` is injectable here: grade_gaps must never reach complete() at
    # all for an empty diff, not merely stub it out, so a real network call
    # happening by accident would fail this test rather than pass it quietly.
    gaps = grade_gaps("reentrancy guards", KNOWN_TOPICS, "   ", "some ground truth diff")

    assert len(gaps) == 1
    assert gaps[0].module_topic == "reentrancy guards"
    assert "no changes" in gaps[0].description.lower()


# --- regression_test_command -------------------------------------------------------------


def test_the_judge_runs_only_the_ground_truths_regression_tests():
    assert regression_test_command(("test/token/ERC20.test.js",)) == ["npx", "hardhat", "test", "test/token/ERC20.test.js"]


def test_several_regression_tests_are_all_passed_to_hardhat():
    command = regression_test_command(("test/a.test.js", "test/b.test.ts"))

    assert command[-2:] == ["test/a.test.js", "test/b.test.ts"]


def test_foundry_tests_are_not_handed_to_hardhat():
    assert regression_test_command(("contracts/Vault.t.sol", "test/a.test.js")) == ["npx", "hardhat", "test", "test/a.test.js"]


def test_a_fix_with_no_hardhat_regression_test_falls_back_to_the_whole_suite():
    assert regression_test_command(()) == ["npm", "test"]
    assert regression_test_command(("contracts/Vault.t.sol",)) == ["npm", "test"]


# --- fetch_ground_truth ---------------------------------------------------------


def test_the_judge_can_bring_the_fix_into_an_attempts_workspace_once_it_has_ended(tmp_path, upstream, ground_truth, git):
    repo, shas = upstream
    workspace = clone_isolated_workspace(ground_truth, tmp_path / "attempts", dir_name="a1", remote_url=str(repo))

    fetch_ground_truth(workspace, ground_truth, remote_url=str(repo))

    assert git(workspace, "rev-parse", f"{shas['fix']}^{{commit}}") == shas["fix"]
    assert git(workspace, "diff", "--name-only", shas["pre_fix"], shas["fix"]) == "Vault.sol"


# --- run_test_suite -------------------------------------------------------------


def _record_commands(monkeypatch, exit_codes):
    commands = []
    codes = iter(exit_codes)

    def fake(args, **kwargs):
        commands.append(list(args))
        return next(codes)

    monkeypatch.setattr("pareto_ai.judge.run_killing_tree", fake)
    return commands


def test_dependencies_that_are_already_linked_in_do_not_need_installing(tmp_path):
    (tmp_path / "node_modules").mkdir()

    assert not needs_install(tmp_path)


def test_a_workspace_with_no_dependencies_needs_them_installed(tmp_path):
    assert needs_install(tmp_path)


def test_a_warm_workspace_is_never_reinstalled_because_npm_ci_would_wipe_the_shared_base(tmp_path, monkeypatch):
    (tmp_path / "node_modules").mkdir()
    commands = _record_commands(monkeypatch, [0])

    assert run_test_suite(tmp_path, ("test/a.test.js",)) is True
    assert commands == [["npx", "hardhat", "test", "test/a.test.js"]]


def test_a_cold_workspace_installs_before_it_tests(tmp_path, monkeypatch):
    commands = _record_commands(monkeypatch, [0, 0])

    run_test_suite(tmp_path, ("test/a.test.js",))

    assert commands[0] == ["npm", "ci"]
    assert commands[1][:3] == ["npx", "hardhat", "test"]


def test_a_failed_install_means_the_tests_did_not_pass_and_are_not_run(tmp_path, monkeypatch):
    commands = _record_commands(monkeypatch, [1])

    assert run_test_suite(tmp_path, ("test/a.test.js",)) is False
    assert len(commands) == 1


def test_failing_tests_did_not_pass(tmp_path, monkeypatch):
    (tmp_path / "node_modules").mkdir()
    _record_commands(monkeypatch, [1])

    assert run_test_suite(tmp_path, ("test/a.test.js",)) is False


def test_tests_that_outrun_their_deadline_did_not_pass_rather_than_crashing_the_judge(tmp_path, monkeypatch):
    (tmp_path / "node_modules").mkdir()
    _record_commands(monkeypatch, [None])

    assert run_test_suite(tmp_path, ("test/a.test.js",)) is False
