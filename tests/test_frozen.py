import json
from dataclasses import dataclass

from pareto_ai.analyst import OccupationalTask, ParetoEntry
from pareto_ai.curriculum import Curriculum, Gap, Module, Resource
from pareto_ai.frozen import FrozenRun, from_jsonable, load_frozen, save_frozen, to_jsonable
from pareto_ai.proof_of_skill import GroundTruth, ProofOfSkillTask, TaskAssignment

TASK = ProofOfSkillTask(repo="o/r", issue_number=7, title="t", body="b", url="https://x/7")
TRUTH = GroundTruth(
    repo="o/r", issue_number=7, pull_number=8, pull_title="p", pull_body="pb",
    merge_commit_sha="m" * 40, pre_fix_commit_sha="p" * 40, url="https://x/8",
)


def _run() -> FrozenRun:
    module = Module(
        topic="reentrancy",
        resources=(Resource(title="R", url=None), Resource(title="S", url="https://s")),
        prerequisites=("solidity basics",),
        deferred_resources=(
            ParetoEntry(
                skill="gas", demand_weight=0.5, sources=("O*NET",),
                occupational_tasks=(OccupationalTask(description="Test", demand=0.9),),
            ),
        ),
        unmet_prerequisites=(),
        proof_of_skill_task=TASK,
    )
    curriculum = Curriculum(target_skill="smart contracts", modules=(module,), uncovered_gaps=(Gap("g", None),))
    assignment = TaskAssignment(
        tasks={"reentrancy": TASK},
        ground_truths={TASK.key: TRUTH},
        topics_without_tasks=("x",),
        unpaired_topics=(),
    )
    return FrozenRun(curriculum=curriculum, assignment=assignment)


def test_a_frozen_run_survives_a_round_trip_through_a_file(tmp_path):
    path = tmp_path / "frozen.json"
    original = _run()

    save_frozen(path, original)

    assert load_frozen(path) == original


def test_the_file_is_plain_json_a_person_can_read_and_edit(tmp_path):
    path = tmp_path / "frozen.json"

    save_frozen(path, _run())

    assert json.loads(path.read_text(encoding="utf-8"))["curriculum"]["target_skill"] == "smart contracts"


def test_tuples_come_back_as_tuples_not_lists():
    restored = from_jsonable(Module, json.loads(json.dumps(to_jsonable(_run().curriculum.modules[0]))))

    assert isinstance(restored.resources, tuple) and isinstance(restored.prerequisites, tuple)


def test_a_missing_optional_field_takes_its_default():
    data = to_jsonable(_run().curriculum.modules[0])
    del data["unmet_prerequisites"]

    assert from_jsonable(Module, data).unmet_prerequisites == ()


def test_a_value_of_the_wrong_shape_is_refused_not_coerced():
    import pytest

    data = to_jsonable(_run().curriculum.modules[0])
    data["resources"] = "not a list"

    with pytest.raises(ValueError):
        from_jsonable(Module, data)


def test_loading_a_file_that_is_not_there_says_so(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        load_frozen(tmp_path / "nope.json")
