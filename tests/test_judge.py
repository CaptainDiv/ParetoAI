import pytest

from pareto_ai.curriculum import Gap
from pareto_ai.judge import (
    JudgeGradingError,
    _graded,
    grade_gaps,
    is_test_file,
    parse_gaps,
    select_test_files,
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
