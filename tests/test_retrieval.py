import importlib.util
import re
from collections.abc import Sequence

import pytest

from pareto_ai.analyst import ParetoEntry, ParetoTable
from pareto_ai.curriculum import (
    Curriculum,
    Resource,
    assemble_curriculum,
    with_proof_of_skill_tasks,
)
from pareto_ai.proof_of_skill import (
    GroundTruth,
    ProofOfSkillTask,
    TaskCandidate,
    assign_proof_of_skill_tasks,
)
from pareto_ai.retrieval import Chunk, RetrievalError, chunk_curriculum, retrieve

TOPICS = ("solidity syntax", "reentrancy guards", "ERC-20 standard")
PREREQUISITES = {"reentrancy guards": ("solidity syntax",)}
RESOURCES = {
    "solidity syntax": (Resource("Solidity docs: language", "https://docs.soliditylang.org/"),),
    "reentrancy guards": (Resource("OpenZeppelin ReentrancyGuard", "https://docs.openzeppelin.com/"),),
    "ERC-20 standard": (Resource("EIP-20", "https://eips.ethereum.org/EIPS/eip-20"),),
}


def fixture_curriculum() -> Curriculum:
    """A small Curriculum, built the way the Curriculum agent builds one.

    Going through assemble_curriculum rather than constructing Modules by hand
    keeps the fixture a real Curriculum: in particular its Modules carry the cut
    entries as Deferred Resources, which is what makes the bounding testable.
    """
    table = ParetoTable(
        target_skill="smart contract development",
        entries=(
            ParetoEntry("solidity syntax", 3, ("Solidity documentation",)),
            ParetoEntry("reentrancy guards", 3, ("Solidity documentation",)),
            ParetoEntry("ERC-20 standard", 2, ("Ethereum.org smart contracts",)),
            ParetoEntry("formal verification", 1, ("Solidity documentation",)),
            ParetoEntry("zk circuits", 1, ("Ethereum.org developer docs index",)),
        ),
    )
    return assemble_curriculum(table, TOPICS, PREREQUISITES, RESOURCES, {})


def chunk_for(topic: str) -> Chunk:
    return next(chunk for chunk in chunk_curriculum(fixture_curriculum()) if chunk.module_topic == topic)


def retrievable_text(curriculum: Curriculum) -> str:
    """Every word a Student Agent could reach in this Curriculum, as one string."""
    return "\n".join(chunk.text for chunk in chunk_curriculum(curriculum))


# --- chunk_curriculum: what a Student Agent can ever retrieve ---


def test_each_module_becomes_one_retrievable_chunk():
    chunks = chunk_curriculum(fixture_curriculum())

    assert [chunk.module_topic for chunk in chunks] == list(TOPICS)


def test_a_chunk_carries_the_resources_its_module_teaches_from():
    reentrancy = chunk_for("reentrancy guards")

    assert "OpenZeppelin ReentrancyGuard" in reentrancy.text
    assert "https://docs.openzeppelin.com/" in reentrancy.text


def test_a_deferred_resource_is_never_retrievable():
    # The Modules of the fixture Curriculum carry "formal verification" and "zk
    # circuits" as Deferred Resources. They are linked for a learner who wants
    # depth beyond job-readiness, and are exactly the 80% the cut set aside — so
    # a Student Agent retrieving them would be bounded to the whole Pareto Table
    # again, and "the path works using only what it teaches" would be unprovable.
    curriculum = fixture_curriculum()
    deferred = [
        entry.skill for module in curriculum.modules for entry in module.deferred_resources
    ]
    assert deferred, "fixture must actually carry Deferred Resources for this to prove anything"

    chunked = retrievable_text(curriculum)

    for skill in deferred:
        assert skill not in chunked


def test_a_chunk_says_what_its_module_assumes_the_learner_knows():
    assert "solidity syntax" in chunk_for("reentrancy guards").text


def test_a_prerequisite_the_curriculum_teaches_late_is_still_in_the_chunk():
    # A prerequisite ordered after the Module that needs it is the one case where
    # a learner really is missing something. Leaving it out of the retrievable
    # text would make the Module read as assuming nothing.
    table = ParetoTable("smart contract development", (ParetoEntry("reentrancy guards", 1, ()),))
    curriculum = assemble_curriculum(
        table, ("reentrancy guards", "solidity syntax"), PREREQUISITES, RESOURCES, {}
    )

    chunks = chunk_curriculum(curriculum)

    assert curriculum.modules[0].unmet_prerequisites == ("solidity syntax",)
    assert "solidity syntax" in chunks[0].text


def test_a_curriculum_with_no_modules_has_nothing_to_retrieve():
    assert chunk_curriculum(Curriculum("smart contract development", ())) == ()


# --- retrieve: Seam 2, with the embedder injected ---
#
# retrieve's mechanics — the k bound, the ordering, what is close enough to be
# returned at all — are tested with a stand-in embedder so they don't depend on a
# downloaded model. That the embeddings are real and semantic, rather than the
# keyword matching this stand-in does, is what the paraphrase test at the bottom
# of the file checks.

VOCABULARY = ("reentrancy", "erc", "solidity", "proxy")


def keyword_embed(texts: Sequence[str]) -> list[list[float]]:
    """One dimension per vocabulary word, so similarity is keyword overlap."""
    return [[float(word in text.casefold()) for word in VOCABULARY] for text in texts]


def test_a_query_returns_no_more_than_k_chunks():
    retrieved = retrieve(fixture_curriculum(), "reentrancy erc solidity", 2, embed=keyword_embed)

    assert len(retrieved) == 2


def test_the_module_closest_to_the_query_comes_first():
    retrieved = retrieve(fixture_curriculum(), "reentrancy solidity", 2, embed=keyword_embed)

    # The reentrancy Module's chunk carries both of the query's words — its own
    # topic, and "solidity syntax" as the prerequisite it assumes. The solidity
    # Module's carries one, and the ERC-20 Module's neither.
    assert [chunk.module_topic for chunk in retrieved] == ["reentrancy guards", "solidity syntax"]


def test_an_embedder_that_cannot_represent_the_query_is_refused():
    # A zero vector scores 0 against every chunk, so retrieval would answer with
    # whatever order the chunks happened to be in — or, before k was the only
    # bound, with nothing at all. Either reads as a statement about the
    # Curriculum rather than about the embedder, which ADR-0002 refuses.
    with pytest.raises(RetrievalError, match="all-zero"):
        retrieve(fixture_curriculum(), "nothing in the vocabulary", 3, embed=keyword_embed)


def test_k_larger_than_the_curriculum_returns_only_what_it_has():
    retrieved = retrieve(fixture_curriculum(), "reentrancy erc solidity", 99, embed=keyword_embed)

    assert len(retrieved) == len(TOPICS)


def test_a_query_returns_nothing_a_module_did_not_put_there():
    curriculum = fixture_curriculum()

    retrieved = retrieve(curriculum, "reentrancy erc solidity", 99, embed=keyword_embed)

    assert set(retrieved) <= set(chunk_curriculum(curriculum))


def test_retrieving_from_a_curriculum_with_no_modules_returns_nothing():
    def embed_nothing(texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("an empty Curriculum has nothing to embed against")

    assert retrieve(Curriculum("smart contract development", ()), "anything", 3, embed=embed_nothing) == ()


def test_a_ground_truth_is_not_reachable_through_retrieval():
    # The Ground Truth never being exposed to a Student Agent is one of
    # CONTEXT.md's non-negotiable rules, and retrieval is the Student Agent's
    # only route into the Curriculum. Modules carry the issue half of a
    # Proof-of-Skill Task and cannot reach the fix half at all; on top of that,
    # chunking draws on what a Module *teaches*, so a Module's task does not
    # become another Module's retrievable text either.
    candidate = TaskCandidate(
        task=ProofOfSkillTask(
            repo="OpenZeppelin/openzeppelin-contracts",
            issue_number=4242,
            title="ReentrancyGuard allows reentry through a different function",
            body="Calling withdraw() during a transfer re-enters the contract.",
            url="https://github.com/OpenZeppelin/openzeppelin-contracts/issues/4242",
        ),
        ground_truth=GroundTruth(
            repo="OpenZeppelin/openzeppelin-contracts",
            issue_number=4242,
            pull_number=4243,
            pull_title="Add a per-function reentrancy lock",
            pull_body="Introduces _nonReentrantBefore so every entry point shares one slot.",
            merge_commit_sha="f00dcafe",
            pre_fix_commit_sha="deadbeef",
            url="https://github.com/OpenZeppelin/openzeppelin-contracts/pull/4243",
        ),
    )
    assignment = assign_proof_of_skill_tasks(TOPICS, (candidate,), {})
    curriculum = with_proof_of_skill_tasks(fixture_curriculum(), assignment.tasks)

    retrievable = retrievable_text(curriculum)

    assert assignment.ground_truths, "fixture must carry a Ground Truth for this to prove anything"
    for ground_truth in assignment.ground_truths.values():
        assert ground_truth.pull_title not in retrievable
        assert ground_truth.pull_body not in retrievable
        assert ground_truth.merge_commit_sha not in retrievable
        assert ground_truth.pre_fix_commit_sha not in retrievable
        assert ground_truth.url not in retrievable
    assert candidate.task.body not in retrievable


# --- the local embedding model: retrieval that is semantic, not lexical ---


def local_embedder_installed() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


@pytest.mark.skipif(not local_embedder_installed(), reason="sentence-transformers is not installed")
@pytest.mark.parametrize(
    ("query", "expected_topic"),
    [
        ("stop a contract calling back into itself before the first call finishes", "reentrancy guards"),
        ("make my coin show up in wallets and exchanges", "ERC-20 standard"),
        ("how do I write a for loop and declare a variable", "solidity syntax"),
    ],
)
def test_a_paraphrased_question_finds_the_module_that_answers_it(query, expected_topic):
    """The one test that separates real vector retrieval from a lexical match.

    Each query is a learner's own words for a Module it shares no vocabulary
    with, so the last two assertions check that premise rather than trusting it:
    the chunk the query found has none of the query's words in it, and
    keyword_embed — a purely lexical matcher over this same fixture — cannot
    represent the question at all, which is why it is refused outright.
    """
    retrieved = retrieve(fixture_curriculum(), query, 1)

    assert [chunk.module_topic for chunk in retrieved] == [expected_topic]
    assert not words(query) & words(retrieved[0].text)
    with pytest.raises(RetrievalError):
        retrieve(fixture_curriculum(), query, 1, embed=keyword_embed)
