"""RAG retriever: bounds a Student Agent's knowledge to the Curriculum.

Seam 2 of the spec. `retrieve(curriculum, query, k)` is what a Student Agent
reaches the Curriculum through, and it exists so that "the path works using only
what it teaches" is a checkable property rather than a claim: an Attempt is given
k retrieved Chunks, never the Curriculum itself.

What a Chunk may be made of is therefore the whole design, and two things a
Module can reach are deliberately left out of it:

  Deferred Resources  the 80% the Curriculum agent cut. They are linked from a
                      Module for a learner who wants depth past job-readiness,
                      so retrieving them would bound a Student Agent to the
                      whole Pareto Table again and there would be nothing left
                      to prove about the cut.
  Proof-of-Skill Task the exam, not the teaching. This one is a judgement the
                      ticket did not ask for either way: a task is inside the
                      Curriculum, but an Attempt is handed its own task as its
                      premise, so retrieval is left to study material and
                      another Module's task cannot arrive as a distraction.
                      Ground Truth cannot be reached from a Module at all — see
                      proof_of_skill.

chunk_curriculum is one Chunk per Module rather than an arbitrary character
split, because a Module already *is* chunk-sized: a topic, what it assumes, and
1-2 Resources. Note what that means for honesty about this pipeline — a
Curriculum holds pointers to teaching material, not the material itself, so what
retrieval returns is "this topic, and here is what to read", not prose to learn
from.

The embedder is injected. The bounding rules above are the part that must never
go untested, and they are pure, so they are TDD'd against fixture Curricula with
a stand-in embedder; that the real embeddings are semantic rather than lexical is
checked separately against the local model, per the spec's Testing Decisions.
The spec calls `retrieve` a pure function, and it is one *given* an embedder —
with the default it loads a model and emits a trace, so "pure" describes the
seam's testability rather than the call.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import neatlogs

from pareto_ai.analyst import run_analyst
from pareto_ai.curriculum import Curriculum, Module, run_curriculum

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

# Small enough to run on a CPU in-process, which is what makes retrieval local:
# a Student Agent asks the Curriculum many questions per Attempt, and none of
# them should cost an API call or leave the machine.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

Vector = Sequence[float]
# Embeds a batch of texts into one vector each, in the order given. Injected so
# retrieve is testable without loading a model, and so the pure ranking below
# works with any embedder rather than only the local one.
Embed = Callable[[Sequence[str]], Sequence[Vector]]


# --- pure core -------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """One retrievable piece of the Curriculum, and the Module it came from."""

    module_topic: str
    text: str


def chunk_curriculum(curriculum: Curriculum) -> tuple[Chunk, ...]:
    """Everything a Student Agent could ever retrieve from this Curriculum."""
    return tuple(_module_chunk(module) for module in curriculum.modules)


def _module_chunk(module: Module) -> Chunk:
    lines = [module.topic]
    # Both kinds of prerequisite, because from inside an Attempt they are the same
    # thing — something this Module assumes. Which one it is says where the
    # Curriculum teaches it, not whether the learner needs it.
    assumed = (*module.prerequisites, *module.unmet_prerequisites)
    if assumed:
        lines.append(f"assumes you already know: {', '.join(assumed)}")
    lines += [
        f"resource: {resource.title}" + (f" <{resource.url}>" if resource.url else "")
        for resource in module.resources
    ]
    return Chunk(module_topic=module.topic, text="\n".join(lines))


# --- the local embedding model ---------------------------------------------

_model: SentenceTransformer | None = None


def _embedding_model() -> SentenceTransformer:
    """The local model, loaded once and kept.

    Imported here rather than at module scope so that chunk_curriculum, the
    ranking below, and their tests stay runnable without the torch install
    sentence-transformers pulls in — the bounding rules are the part that must
    never go untested, and they do not need a model to check.
    """
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


@neatlogs.span(kind="EMBEDDING")
def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Embed a batch of texts with the local model."""
    return _embedding_model().encode(list(texts)).tolist()


class RetrievalError(RuntimeError):
    """An embedding couldn't be used, so what retrieval returned can't be trusted.

    ADR-0002 again: a zero vector scores 0 against everything, so letting one
    through would answer with no chunks at all — and an empty answer reads
    exactly like "nothing in this Curriculum covers that", which is a claim about
    the Curriculum rather than about the embedder. Inside an Attempt that
    difference ends up in a pass rate.
    """


def _is_zero(vector: Vector) -> bool:
    return not any(vector)


def _cosine_similarity(left: Vector, right: Vector) -> float:
    """How alike two embeddings are, on a scale where 0 is nothing in common.

    Neither vector may be all zeros; retrieve refuses those before getting here.
    strict, for the same reason retrieve is: vectors of differing length mean the
    embedder misbehaved, and truncating them would just score the overlap and
    call it a similarity.
    """
    magnitude = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return sum(a * b for a, b in zip(left, right, strict=True)) / magnitude


@neatlogs.span(kind="RETRIEVER")
def retrieve(
    curriculum: Curriculum, query: str, k: int, *, embed: Embed = embed_texts
) -> tuple[Chunk, ...]:
    """The at-most-k Chunks of this Curriculum closest to the query.

    This is the whole of a Student Agent's access to the Curriculum. `k` has no
    default: the bound is the point of this function, so the caller states it.
    """
    chunks = chunk_curriculum(curriculum)
    if not chunks:
        return ()

    vectors = embed([query, *(chunk.text for chunk in chunks)])
    if any(_is_zero(vector) for vector in vectors):
        raise RetrievalError(
            f"the embedder returned an all-zero vector for {query!r} or for one of "
            f"{len(chunks)} chunks, so a no-match answer would not mean no match"
        )

    query_vector, *chunk_vectors = vectors
    # strict, because an embedder handing back the wrong number of vectors would
    # otherwise quietly drop chunks from what a Student Agent can reach at all.
    ranked = sorted(
        zip(
            chunks,
            (_cosine_similarity(query_vector, vector) for vector in chunk_vectors),
            strict=True,
        ),
        key=lambda scored: -scored[1],
    )
    # k alone is the bound, which is the bound the spec names. An earlier version
    # also dropped chunks scoring <= 0, on the assumption that real embeddings
    # practically never do; measuring it refuted that — an off-topic question
    # ("sourdough bread") scored this fixture's three chunks at -0.008, -0.014
    # and +0.040, so the sign was noise and the filter was a coin toss that
    # admitted +0.040 as relevant anyway. A cut-off that meant anything would
    # need calibration data this project does not have.
    return tuple(chunk for chunk, _ in ranked[:k])


# --- live smoke test ------------------------------------------------------

# The kind of thing a Student Agent asks mid-Attempt. Fixed rather than
# generated, so a run can be compared against the last one.
SMOKE_QUERIES: tuple[str, ...] = (
    "a contract keeps calling back into itself before my first call has finished",
    "which standard does a fungible token have to follow",
    "my transaction runs out of gas inside a loop",
)
SMOKE_K = 3


# Retrieval is a step inside a Student Agent's Attempt (ticket #6), not a
# workflow of its own, so `retrieve` carries a RETRIEVER span and no root. A
# standalone run still needs one, or its spans arrive in Neatlogs parented to
# nothing — hence the WORKFLOW span here rather than on retrieve itself.
@neatlogs.span(kind="WORKFLOW", name="retrieval_smoke_test")
def _print_retrieval(curriculum: Curriculum, queries: Sequence[str] = SMOKE_QUERIES) -> None:
    chunks = chunk_curriculum(curriculum)
    deferred = [entry for module in curriculum.modules for entry in module.deferred_resources]

    print(f"Retrieval over the Curriculum for: {curriculum.target_skill}\n")
    print(f"{len(chunks)} retrievable chunks, one per Module.")
    # The bound, stated in the output rather than only in the tests: this many
    # Pareto Table entries hang off these Modules and none of them can be reached.
    print(f"{len(deferred)} Deferred Resources are linked from those Modules and NOT retrievable.\n")

    for query in queries:
        retrieved = retrieve(curriculum, query, SMOKE_K)
        print(f'? "{query}"')
        print(f"  retrieved {len(retrieved)} of {len(chunks)} chunks (k={SMOKE_K})")
        for chunk in retrieved:
            # The whole chunk, indented — printing a header and then slicing the
            # first line back off would quietly depend on where the topic sits in
            # the text, and this is meant to show exactly what a Student Agent got.
            print("  - " + chunk.text.replace("\n", "\n      "))
        if not retrieved:
            print("  - nothing in this Curriculum is close to the question")
        print()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        _print_retrieval(run_curriculum(run_analyst("smart contract development")))
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
