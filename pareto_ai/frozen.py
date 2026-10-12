"""A frozen upstream: the Curriculum and Proof-of-Skill assignment, saved once and reused.

Running one Attempt and having the Judge grade it needs a Curriculum, a task and
its Ground Truth, but not a *fresh* one: building them is the Analyst, Curriculum
and sourcing agents' job, costs about two minutes of LLM calls, and changes the
Module under test every time (observed over four consecutive runs on 2026-10-11:
"input validation", "Solidity data types", "Business logic design", ...). Saving
the result once makes an Attempt-and-Judge loop repeatable, which is what lets
one change to the Student Agent or the Judge be told apart from the Curriculum
moving underneath it.

The file is plain JSON on purpose, so a person can open it, read which task is
being attempted, and edit it to pin a different one. Deserialisation is driven by
the dataclasses' own type hints rather than written per class, so a field added
to a Curriculum type round-trips without touching this module — and a value of
the wrong shape is refused rather than coerced (ADR-0002), because a frozen run
that quietly loaded as something else would make every later grade meaningless.

This holds the Ground Truth beside the Curriculum, exactly as `TaskAssignment`
does, so the same discipline applies: only `assignment.tasks` ever goes toward a
Student Agent.
"""

from __future__ import annotations

import json
import types
import typing
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from pareto_ai.analyst import OccupationalTask, ParetoEntry
from pareto_ai.curriculum import Curriculum
from pareto_ai.analyst import run_analyst
from pareto_ai.curriculum import run_curriculum, with_proof_of_skill_tasks
from pareto_ai.proof_of_skill import GroundTruth, ProofOfSkillTask, TaskAssignment, run_proof_of_skill_sourcing
from pareto_ai.warm_base import WARM_CACHE_DIR


# Names a type hint can mention that its own module only imports for type
# checking (curriculum.py names ProofOfSkillTask that way, to avoid an import
# cycle), so get_type_hints cannot resolve them from the class alone.
_NAMES = {
    "ProofOfSkillTask": ProofOfSkillTask,
    "GroundTruth": GroundTruth,
    "ParetoEntry": ParetoEntry,
    "OccupationalTask": OccupationalTask,
}


@dataclass(frozen=True)
class FrozenRun:
    curriculum: Curriculum
    assignment: TaskAssignment


def to_jsonable(value: Any) -> Any:
    """A dataclass, or anything made of them, as plain JSON-ready data."""
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


def from_jsonable(cls: type, data: Any) -> Any:
    """Rebuild `cls` from `to_jsonable`'s output, by its type hints. Raises ValueError on a bad shape."""
    return _build(cls, data)


def _build(hint: Any, data: Any) -> Any:
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    if origin in (typing.Union, types.UnionType):
        if data is None and type(None) in args:
            return None
        (inner,) = [arg for arg in args if arg is not type(None)]
        return _build(inner, data)

    if origin is tuple:
        if not isinstance(data, list | tuple):
            raise ValueError(f"expected a list for {hint}, got {type(data).__name__}")
        element = args[0]  # every tuple in these types is `tuple[X, ...]` or a homogeneous pair
        return tuple(_build(element, item) for item in data)

    if origin is not None and issubclass(origin, Mapping):
        if not isinstance(data, dict):
            raise ValueError(f"expected an object for {hint}, got {type(data).__name__}")
        key_type, value_type = args
        return {_build(key_type, key): _build(value_type, value) for key, value in data.items()}

    if isinstance(hint, type) and is_dataclass(hint):
        if not isinstance(data, dict):
            raise ValueError(f"expected an object for {hint.__name__}, got {type(data).__name__}")
        hints = typing.get_type_hints(hint, localns=_NAMES)
        kwargs = {f.name: _build(hints[f.name], data[f.name]) for f in fields(hint) if f.name in data}
        return hint(**kwargs)

    if hint is float and isinstance(data, int) and not isinstance(data, bool):
        return float(data)
    if hint in (str, int, float, bool) and not isinstance(data, hint):
        raise ValueError(f"expected {hint.__name__}, got {type(data).__name__}")
    return data


def save_frozen(path: Path, run: FrozenRun) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(run), indent=2, ensure_ascii=False), encoding="utf-8")


def load_frozen(path: Path) -> FrozenRun:
    return from_jsonable(FrozenRun, json.loads(path.read_text(encoding="utf-8")))


FROZEN_RUN_PATH = WARM_CACHE_DIR / "frozen_run.json"


def load_or_build_frozen_run(path: Path) -> FrozenRun:
    """The saved run if there is one, else build it live once and save it.

    Delete the file to get a fresh Curriculum; edit it to pin a different task.
    Live (Analyst, Curriculum and GitHub sourcing), so smoke-tested rather than unit-tested.
    """
    if path.exists():
        return load_frozen(path)
    curriculum = run_curriculum(run_analyst("smart contract development"))
    assignment = run_proof_of_skill_sourcing(curriculum.target_skill, [m.topic for m in curriculum.modules])
    run = FrozenRun(curriculum=with_proof_of_skill_tasks(curriculum, assignment.tasks), assignment=assignment)
    save_frozen(path, run)
    return run
