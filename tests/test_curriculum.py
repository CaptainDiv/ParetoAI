import pytest

from pareto_ai.analyst import ParetoEntry, ParetoTable
from pareto_ai.curriculum import (
    CurriculumPlanningError,
    Gap,
    Resource,
    _planned,
    assemble_curriculum,
    parse_deferrals,
    parse_prerequisites,
    parse_resources,
    select_module_topics,
    with_proof_of_skill_tasks,
)
from pareto_ai.proof_of_skill import ProofOfSkillTask


def table(
    *skills_with_demand_weight: tuple[str, float], sources: tuple[str, ...] = ("O*NET Blockchain Engineers",)
) -> ParetoTable:
    """A Pareto Table in the ranked order aggregate_pareto_table would produce."""
    entries = [
        ParetoEntry(skill=skill, demand_weight=demand_weight, sources=sources)
        for skill, demand_weight in skills_with_demand_weight
    ]
    entries.sort(key=lambda entry: (-entry.demand_weight, entry.skill))
    return ParetoTable(target_skill="smart contract development", entries=tuple(entries))


def ten_entry_table() -> ParetoTable:
    return table(
        ("solidity syntax", 1.0),
        ("gas optimization", 0.9),
        ("reentrancy guards", 0.8),
        ("ERC-20 standard", 0.7),
        ("upgradeable proxies", 0.6),
        ("formal verification", 0.5),
        ("MEV", 0.4),
        ("account abstraction", 0.3),
        ("zk circuits", 0.2),
        ("EVM bytecode", 0.1),
    )


# --- select_module_topics: the 20% cut, prerequisite closure, prerequisite order ---


def test_selects_the_top_fifth_of_the_table_by_demand_weight():
    topics = select_module_topics(ten_entry_table(), prerequisites={})

    assert set(topics) == {"solidity syntax", "gas optimization"}


def test_a_tiny_table_still_yields_one_topic():
    topics = select_module_topics(table(("solidity syntax", 0.5)), prerequisites={})

    assert topics == ("solidity syntax",)


def test_an_empty_table_yields_no_topics():
    assert select_module_topics(ParetoTable("smart contract development", ()), prerequisites={}) == ()


def test_a_low_demand_weight_hard_prerequisite_is_included_regardless_of_rank():
    # "EVM bytecode" is dead last by Demand Weight, so the 20% cut drops it — but
    # gas optimization can't be learned without it.
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={"gas optimization": ("EVM bytecode",)},
    )

    assert "EVM bytecode" in topics


def test_prerequisite_closure_is_transitive():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={"gas optimization": ("EVM bytecode",), "EVM bytecode": ("zk circuits",)},
    )

    assert {"zk circuits", "EVM bytecode"} <= set(topics)


def test_a_prerequisite_comes_before_the_topic_that_needs_it():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={"gas optimization": ("EVM bytecode",), "EVM bytecode": ("zk circuits",)},
    )

    assert topics.index("zk circuits") < topics.index("EVM bytecode")
    assert topics.index("EVM bytecode") < topics.index("gas optimization")


def test_prerequisites_that_are_not_pareto_table_entries_are_ignored():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={"gas optimization": ("how to use a keyboard",)},
    )

    assert "how to use a keyboard" not in topics


def test_a_prerequisite_cycle_keeps_every_topic_instead_of_hanging():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={"solidity syntax": ("gas optimization",), "gas optimization": ("solidity syntax",)},
    )

    assert set(topics) == {"solidity syntax", "gas optimization"}


def test_a_topic_listed_as_its_own_prerequisite_is_ignored():
    topics = select_module_topics(ten_entry_table(), prerequisites={"gas optimization": ("gas optimization",)})

    assert set(topics) == {"solidity syntax", "gas optimization"}


def test_a_prior_gap_forces_its_module_topic_back_into_the_curriculum():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={},
        prior_gaps=(Gap(description="no proxy pattern coverage", module_topic="upgradeable proxies"),),
    )

    assert "upgradeable proxies" in topics


def test_a_prior_gap_that_names_no_pareto_table_entry_adds_no_topic():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={},
        prior_gaps=(Gap(description="never heard of it", module_topic="cobol"),),
    )

    assert set(topics) == {"solidity syntax", "gas optimization"}


def blaming(*topics: str) -> tuple[Gap, ...]:
    return tuple(Gap(description=f"weak on {topic}", module_topic=topic) for topic in topics)


def test_prior_gaps_add_at_most_two_cut_topics_in_one_revision():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={},
        prior_gaps=blaming("upgradeable proxies", "formal verification", "MEV", "account abstraction", "zk circuits"),
    )

    # The two seeds from the 20% cut, plus the two cap allows: a Round that could add a
    # Module per Gap would grow the next Round's Attempts (and bill) with every failure.
    assert len(topics) == 4


def test_the_cut_topics_blamed_most_often_are_the_ones_added():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={},
        prior_gaps=blaming("upgradeable proxies", "MEV", "zk circuits", "MEV", "zk circuits", "MEV"),
    )

    assert set(topics) == {"solidity syntax", "gas optimization", "MEV", "zk circuits"}


def test_equally_blamed_cut_topics_are_added_in_table_order():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={},
        prior_gaps=blaming("zk circuits", "MEV", "upgradeable proxies"),
    )

    assert set(topics) == {"solidity syntax", "gas optimization", "upgradeable proxies", "MEV"}


def test_a_gap_naming_a_topic_already_in_the_cut_uses_none_of_the_allowance():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={},
        prior_gaps=blaming("gas optimization", "gas optimization", "MEV", "zk circuits", "upgradeable proxies"),
    )

    assert set(topics) == {"solidity syntax", "gas optimization", "upgradeable proxies", "MEV"}


def test_a_gap_naming_a_topic_already_in_through_prerequisite_closure_uses_none_of_the_allowance():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={"gas optimization": ("EVM bytecode",)},
        prior_gaps=blaming("EVM bytecode", "EVM bytecode", "EVM bytecode", "MEV", "zk circuits"),
    )

    assert set(topics) == {"solidity syntax", "gas optimization", "EVM bytecode", "MEV", "zk circuits"}


def test_modules_kept_from_the_last_curriculum_stay_in_it():
    # A Module a Gap added last Round is no longer blamed once it is taught, so
    # without this the next revision would drop it and bring the Gap back.
    topics = select_module_topics(ten_entry_table(), prerequisites={}, keep=("MEV",))

    assert set(topics) == {"solidity syntax", "gas optimization", "MEV"}


def test_kept_modules_do_not_use_the_allowance_for_new_ones():
    topics = select_module_topics(
        ten_entry_table(),
        prerequisites={},
        keep=("MEV",),
        prior_gaps=blaming("zk circuits", "upgradeable proxies", "formal verification"),
    )

    assert set(topics) == {"solidity syntax", "gas optimization", "MEV", "upgradeable proxies", "formal verification"}


def test_a_kept_topic_the_table_does_not_have_is_ignored():
    topics = select_module_topics(ten_entry_table(), prerequisites={}, keep=("cobol",))

    assert set(topics) == {"solidity syntax", "gas optimization"}


# --- assemble_curriculum: Modules, Resources, Deferred Resources ---

PREREQUISITES = {"gas optimization": ("solidity syntax",)}
TOPICS = ("solidity syntax", "gas optimization")
RESOURCES = {
    "solidity syntax": (Resource("Solidity docs: language", "https://docs.soliditylang.org/"),),
    "gas optimization": (Resource("Gas optimization guide", "https://example.com/gas"),),
}


def test_builds_one_module_per_topic_in_the_order_given():
    curriculum = assemble_curriculum(ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {})

    assert curriculum.target_skill == "smart contract development"
    assert [module.topic for module in curriculum.modules] == list(TOPICS)


def test_a_module_names_only_earlier_modules_as_prerequisites():
    # "gas optimization" is ordered first here, so its prerequisite hasn't been
    # taught yet and must not be claimed as one.
    curriculum = assemble_curriculum(
        ten_entry_table(), ("gas optimization", "solidity syntax"), PREREQUISITES, RESOURCES, {}
    )

    assert curriculum.modules[0].prerequisites == ()


def test_a_module_records_prerequisites_it_is_taught_after():
    curriculum = assemble_curriculum(ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {})

    assert curriculum.modules[1].prerequisites == ("solidity syntax",)
    assert curriculum.modules[1].unmet_prerequisites == ()


def test_a_module_taught_before_its_prerequisite_reports_it_as_unmet():
    # Only reachable when a prerequisite cycle forced an impossible ordering: the
    # learner really is missing something here, so it must not read as "no
    # prerequisites".
    curriculum = assemble_curriculum(
        ten_entry_table(), ("gas optimization", "solidity syntax"), PREREQUISITES, RESOURCES, {}
    )

    assert curriculum.modules[0].unmet_prerequisites == ("solidity syntax",)


def test_a_module_carries_at_most_two_resources():
    resources = {
        "solidity syntax": (Resource("one"), Resource("two"), Resource("three")),
        "gas optimization": (Resource("four"),),
    }

    curriculum = assemble_curriculum(ten_entry_table(), TOPICS, PREREQUISITES, resources, {})

    assert curriculum.modules[0].resources == (Resource("one"), Resource("two"))


def test_a_module_with_no_planned_resources_falls_back_to_its_citing_source():
    pareto_table = table(("solidity syntax", 0.5), sources=("Cyfrin exam objectives",))

    curriculum = assemble_curriculum(pareto_table, ("solidity syntax",), {}, {}, {})

    assert curriculum.modules[0].resources == (Resource("Cyfrin exam objectives"),)


def test_every_cut_entry_is_linked_from_exactly_one_module_as_a_deferred_resource():
    pareto_table = ten_entry_table()

    curriculum = assemble_curriculum(pareto_table, TOPICS, PREREQUISITES, RESOURCES, {})

    deferred = [entry for module in curriculum.modules for entry in module.deferred_resources]
    assert sorted(entry.skill for entry in deferred) == sorted(
        entry.skill for entry in pareto_table.entries if entry.skill not in TOPICS
    )


def test_a_cut_entry_is_linked_from_the_module_it_was_assigned_to():
    curriculum = assemble_curriculum(
        ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {"MEV": "gas optimization"}
    )

    gas_module = curriculum.modules[1]
    assert "MEV" in [entry.skill for entry in gas_module.deferred_resources]


def test_an_unassigned_cut_entry_lands_on_the_first_module_rather_than_being_discarded():
    curriculum = assemble_curriculum(ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {})

    assert "MEV" in [entry.skill for entry in curriculum.modules[0].deferred_resources]


def test_a_cut_entry_assigned_to_a_topic_that_is_not_a_module_lands_on_the_first_module():
    curriculum = assemble_curriculum(
        ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {"MEV": "formal verification"}
    )

    assert "MEV" in [entry.skill for entry in curriculum.modules[0].deferred_resources]


def test_a_deferred_resource_keeps_its_pareto_table_provenance():
    curriculum = assemble_curriculum(ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {})

    deferred = {entry.skill: entry for module in curriculum.modules for entry in module.deferred_resources}
    assert deferred["EVM bytecode"] == ParetoEntry("EVM bytecode", 0.1, ("O*NET Blockchain Engineers",))


def test_gaps_no_module_covers_are_surfaced_rather_than_dropped():
    gaps = (
        Gap(description="covered", module_topic="gas optimization"),
        Gap(description="nothing teaches this", module_topic=None),
        Gap(description="cut from the curriculum", module_topic="MEV"),
    )

    curriculum = assemble_curriculum(
        ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {}, prior_gaps=gaps
    )

    assert [gap.description for gap in curriculum.uncovered_gaps] == [
        "nothing teaches this",
        "cut from the curriculum",
    ]


# --- parsing the model's planning responses --------------------------------
#
# A response cut off at its token budget can come back empty or half-written.
# Silently reading that as "no prerequisites" disables prerequisite closure
# invisibly, so every parser refuses a truncated response.

TRUNCATED_PARSERS = (parse_prerequisites, parse_resources, parse_deferrals)


@pytest.mark.parametrize("parse", TRUNCATED_PARSERS)
def test_a_response_truncated_at_the_token_budget_is_refused(parse):
    with pytest.raises(CurriculumPlanningError, match="budget"):
        parse("", finish_reason="length")


@pytest.mark.parametrize("parse", TRUNCATED_PARSERS)
def test_a_response_with_no_json_object_is_refused(parse):
    with pytest.raises(CurriculumPlanningError):
        parse("Sure! Here are the prerequisites you asked for.")


@pytest.mark.parametrize("parse", TRUNCATED_PARSERS)
def test_an_explicitly_empty_plan_is_accepted(parse):
    assert parse("{}") == {}


def test_parses_prerequisites_surrounded_by_prose():
    content = 'Here you go:\n{"gas optimization": ["solidity syntax"]}\nHope that helps!'

    assert parse_prerequisites(content) == {"gas optimization": ("solidity syntax",)}


def test_reads_a_lone_prerequisite_given_as_a_bare_string():
    # Dropping it would read downstream as "gas optimization has no prerequisites".
    content = '{"gas optimization": "solidity syntax", "MEV": ["gas optimization"]}'

    assert parse_prerequisites(content) == {
        "gas optimization": ("solidity syntax",),
        "MEV": ("gas optimization",),
    }


def test_refuses_a_prerequisite_entry_that_is_neither_a_list_nor_a_string():
    with pytest.raises(CurriculumPlanningError, match="gas optimization"):
        parse_prerequisites('{"gas optimization": {"first": "solidity syntax"}}')


def test_parses_resources_with_and_without_urls():
    content = """{
      "gas optimization": [
        {"title": "Gas guide", "url": "https://example.com/gas"},
        {"title": "No link here"}
      ]
    }"""

    assert parse_resources(content) == {
        "gas optimization": (
            Resource("Gas guide", "https://example.com/gas"),
            Resource("No link here", None),
        )
    }


def test_drops_resources_with_no_title():
    content = '{"gas optimization": [{"url": "https://example.com"}, {"title": "Gas guide"}]}'

    assert parse_resources(content) == {"gas optimization": (Resource("Gas guide"),)}


def test_parses_deferrals_and_drops_non_string_targets():
    content = '{"MEV": "gas optimization", "zk circuits": ["gas optimization"]}'

    assert parse_deferrals(content) == {"MEV": "gas optimization"}


# --- planning calls: one retry, then give up loudly ------------------------


def test_a_planning_call_retries_once_when_the_model_answers_unusably():
    responses = iter([("thinking out loud, no JSON", "stop"), ('{"MEV": "gas optimization"}', "stop")])

    plan = _planned("prompt", parse_deferrals, ask=lambda prompt: next(responses))

    assert plan == {"MEV": "gas optimization"}


def test_a_planning_call_that_fails_twice_raises_rather_than_planning_nothing():
    with pytest.raises(CurriculumPlanningError):
        _planned("prompt", parse_prerequisites, ask=lambda prompt: ("", "length"))


# --- attaching Proof-of-Skill Tasks to Modules ---


def task(issue_number: int) -> ProofOfSkillTask:
    return ProofOfSkillTask(
        repo="OpenZeppelin/openzeppelin-contracts",
        issue_number=issue_number,
        title=f"issue {issue_number}",
        body="body",
        url=f"https://github.com/OpenZeppelin/openzeppelin-contracts/issues/{issue_number}",
    )


def test_a_module_carries_the_task_assigned_to_its_topic():
    curriculum = assemble_curriculum(ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {})

    tasked = with_proof_of_skill_tasks(curriculum, {"gas optimization": task(101)})

    assert tasked.modules[1].proof_of_skill_task == task(101)


def test_a_module_whose_topic_has_no_task_carries_none():
    curriculum = assemble_curriculum(ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {})

    tasked = with_proof_of_skill_tasks(curriculum, {"gas optimization": task(101)})

    assert tasked.modules[0].proof_of_skill_task is None


def test_attaching_tasks_changes_nothing_else_about_a_module():
    curriculum = assemble_curriculum(ten_entry_table(), TOPICS, PREREQUISITES, RESOURCES, {})

    tasked = with_proof_of_skill_tasks(curriculum, {"gas optimization": task(101)})

    before, after = curriculum.modules[1], tasked.modules[1]
    assert (after.topic, after.resources, after.prerequisites, after.deferred_resources) == (
        before.topic,
        before.resources,
        before.prerequisites,
        before.deferred_resources,
    )


def test_attaching_tasks_preserves_the_curriculum_around_the_modules():
    curriculum = assemble_curriculum(
        ten_entry_table(),
        TOPICS,
        PREREQUISITES,
        RESOURCES,
        {},
        prior_gaps=(Gap(description="uncovered", module_topic=None),),
    )

    tasked = with_proof_of_skill_tasks(curriculum, {})

    assert tasked.target_skill == curriculum.target_skill
    assert tasked.uncovered_gaps == curriculum.uncovered_gaps
    assert len(tasked.modules) == len(curriculum.modules)
