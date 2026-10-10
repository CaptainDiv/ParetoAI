from dataclasses import fields

from pareto_ai.curriculum import Module, Resource
from pareto_ai.proof_of_skill import GroundTruth, ProofOfSkillTask
from pareto_ai.retrieval import Chunk
from pareto_ai.student_agent import (
    SOME_IT_BACKGROUND,
    TOTAL_BEGINNER,
    LearnerProfile,
    attempt_workspace_name,
    build_attempt_prompt,
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
