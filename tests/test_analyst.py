import pytest

from pareto_ai.analyst import (
    CitedSkill,
    OccupationalTask,
    Rank,
    Rating,
    Source,
    SourceExtraction,
    aggregate_pareto_table,
    apply_aliases,
    canonical_skill,
    parse_cited_skills,
    parse_served_occupational_tasks,
)

TARGET = "smart contract development"

EMPLOYERS = Source(
    "Employer survey",
    "https://employers.example/",
    scale=Rating(maximum=100),
    measures_whole_target_skill=True,
)
SURVEY = Source(
    "Developer survey",
    "https://survey.example/",
    scale=Rating(maximum=100),
    measures_whole_target_skill=True,
)
TOP_TEN = Source(
    "OWASP Smart Contract Top 10",
    "https://scs.owasp.org/sctop10/",
    scale=Rank(count=10),
    measures_whole_target_skill=False,
)
SECURITY_SURVEY = Source(
    "Security survey",
    "https://security.example/",
    scale=Rating(maximum=100),
    measures_whole_target_skill=False,
)


OBJECTIVES = Source(
    "Cyfrin exam objectives",
    "https://updraft.cyfrin.io/certifications/solidity-certification",
    scale=None,
    measures_whole_target_skill=True,
)


ONET = Source(
    "O*NET Blockchain Engineers",
    "https://www.onetonline.org/link/details/15-1299.07",
    scale=Rating(maximum=100),
    measures_whole_target_skill=True,
    rates_occupational_tasks=True,
)
TEST_SECURITY = "Test the security and performance of blockchain infrastructures."
ASSESS_THREATS = "Assess blockchain threats, such as untested code and unprotected keys."
CRYPTOGRAPHY = "Design and verify cryptographic protocols to protect private information."
DASHBOARDS = "Design and implement dashboard and data visualizations to meet customer reporting needs."


def rated(source: Source, rating_by_occupational_task: dict[str, float | None]) -> SourceExtraction:
    """What a Source that rates Occupational Tasks reported, in its own units."""
    return SourceExtraction(
        source=source,
        skills=(),
        occupational_tasks=tuple(OccupationalTask(task, rating) for task, rating in rating_by_occupational_task.items()),
    )


def said(source: Source, demand_by_skill: dict[str, float]) -> SourceExtraction:
    """What a Source with a demand scale reported, in that Source's own units."""
    return SourceExtraction(
        source=source,
        skills=tuple(CitedSkill(name, demand) for name, demand in demand_by_skill.items()),
    )


def listed(source: Source, *skills: str) -> SourceExtraction:
    """What a Source with no demand scale named."""
    return SourceExtraction(source=source, skills=tuple(CitedSkill(name) for name in skills))


# --- weighing skills through the Occupational Tasks they serve ---


def test_a_skill_takes_the_rating_of_the_occupational_task_it_serves():
    table = aggregate_pareto_table(
        TARGET,
        [rated(ONET, {TEST_SECURITY: 91, CRYPTOGRAPHY: 83}), listed(OBJECTIVES, "signatures", "fuzz testing")],
        served_occupational_tasks={ONET.name: {"fuzz testing": (TEST_SECURITY,), "signatures": (CRYPTOGRAPHY,)}},
    )

    assert [(entry.skill, entry.demand_weight) for entry in table.entries] == [
        ("fuzz testing", pytest.approx(0.91)),
        ("signatures", pytest.approx(0.83)),
    ]


def test_a_source_that_rates_occupational_tasks_adds_no_entries_of_its_own():
    # Neither its Occupational Tasks nor a skill only the classification named.
    table = aggregate_pareto_table(
        TARGET,
        [rated(ONET, {TEST_SECURITY: 91}), listed(OBJECTIVES, "fuzz testing")],
        served_occupational_tasks={ONET.name: {"fuzz testing": (TEST_SECURITY,), "load testing": (TEST_SECURITY,)}},
    )

    assert [(entry.skill, entry.sources) for entry in table.entries] == [
        ("fuzz testing", ("Cyfrin exam objectives", "O*NET Blockchain Engineers"))
    ]


def test_a_skill_cited_by_a_source_that_rates_occupational_tasks_never_becomes_an_entry():
    # An occupation-level item read as a skill is what #13 set out to remove.
    extraction = SourceExtraction(
        ONET, skills=(CitedSkill("risk evaluation", 90),), occupational_tasks=(OccupationalTask(TEST_SECURITY, 91),)
    )

    table = aggregate_pareto_table(
        TARGET,
        [extraction, listed(OBJECTIVES, "fuzz testing")],
        served_occupational_tasks={ONET.name: {"fuzz testing": (TEST_SECURITY,)}},
    )

    assert [entry.skill for entry in table.entries] == ["fuzz testing"]


def test_a_skill_serving_several_occupational_tasks_takes_the_strongest_rating():
    table = aggregate_pareto_table(
        TARGET,
        [rated(ONET, {ASSESS_THREATS: 88, TEST_SECURITY: 91}), listed(OBJECTIVES, "fuzz testing")],
        served_occupational_tasks={ONET.name: {"fuzz testing": (ASSESS_THREATS, TEST_SECURITY)}},
    )

    assert table.entries[0].demand_weight == pytest.approx(0.91)


def test_an_entry_records_the_occupational_tasks_it_was_weighed_through_strongest_first():
    # Which Occupational Task a skill serves is the model's call, so it is shown.
    table = aggregate_pareto_table(
        TARGET,
        [rated(ONET, {ASSESS_THREATS: 88, TEST_SECURITY: 91}), listed(OBJECTIVES, "fuzz testing", "opcodes")],
        served_occupational_tasks={ONET.name: {"fuzz testing": (ASSESS_THREATS, TEST_SECURITY), "opcodes": ()}},
    )

    assert [(entry.skill, entry.occupational_tasks) for entry in table.entries] == [
        ("fuzz testing", (OccupationalTask(TEST_SECURITY, 91), OccupationalTask(ASSESS_THREATS, 88))),
        ("opcodes", ()),
    ]


def test_the_table_lists_the_occupational_tasks_no_skill_serves_strongest_first():
    # The parts of the job this Pareto Table doesn't reach, reported rather than hidden.
    table = aggregate_pareto_table(
        TARGET,
        [rated(ONET, {DASHBOARDS: 64, TEST_SECURITY: 91, CRYPTOGRAPHY: 83}), listed(OBJECTIVES, "fuzz testing")],
        served_occupational_tasks={ONET.name: {"fuzz testing": (TEST_SECURITY,)}},
    )

    assert table.unserved_occupational_tasks == (
        OccupationalTask(CRYPTOGRAPHY, 83),
        OccupationalTask(DASHBOARDS, 64),
    )


def test_an_occupational_task_without_a_readable_rating_is_recorded_and_weighs_nothing():
    unrated = "Develop software tools, such as application programming interfaces (APIs), to integrate applications."
    table = aggregate_pareto_table(
        TARGET,
        [rated(ONET, {TEST_SECURITY: 91, unrated: None}), listed(OBJECTIVES, "fuzz testing", "API design")],
        served_occupational_tasks={ONET.name: {"fuzz testing": (TEST_SECURITY,), "API design": (unrated,)}},
    )

    assert table.unreadable_citations == (("O*NET Blockchain Engineers", unrated),)
    assert table.unserved_occupational_tasks == ()
    assert [(entry.skill, entry.demand_weight) for entry in table.entries] == [
        ("fuzz testing", pytest.approx(0.91)),
        ("API design", 0.0),
    ]


def test_a_skill_serving_none_of_a_whole_skill_sources_occupational_tasks_takes_its_zero():
    # The Occupational Tasks span the whole occupation, so serving none of them
    # is a real absence of demand.
    table = aggregate_pareto_table(
        TARGET,
        [rated(ONET, {TEST_SECURITY: 91}), said(SECURITY_SURVEY, {"fuzz testing": 80, "token art": 80})],
        served_occupational_tasks={ONET.name: {"fuzz testing": (TEST_SECURITY,), "token art": ()}},
    )

    weights = {entry.skill: entry.demand_weight for entry in table.entries}
    assert weights == {"fuzz testing": pytest.approx(0.855), "token art": pytest.approx(0.4)}


def test_a_skill_left_out_of_the_classification_is_refused_rather_than_read_as_serving_none():
    # Read as none, fuzz testing would take O*NET's 0, which O*NET never gave.
    with pytest.raises(ValueError, match="fuzz testing"):
        aggregate_pareto_table(
            TARGET,
            [rated(ONET, {TEST_SECURITY: 91}), listed(OBJECTIVES, "fuzz testing", "opcodes")],
            served_occupational_tasks={ONET.name: {"opcodes": ()}},
        )


def test_a_source_that_rated_occupational_tasks_counts_its_silence_even_when_no_skill_serves_one():
    table = aggregate_pareto_table(
        TARGET,
        [rated(ONET, {DASHBOARDS: 64}), said(SECURITY_SURVEY, {"access control": 100})],
        served_occupational_tasks={ONET.name: {"access control": ()}},
    )

    assert table.entries[0].demand_weight == pytest.approx(0.5)


def test_a_source_that_rated_no_occupational_task_readably_does_not_count_its_silence():
    failed = rated(ONET, {TEST_SECURITY: None})

    table = aggregate_pareto_table(
        TARGET, [failed, said(SECURITY_SURVEY, {"access control": 100})], served_occupational_tasks={ONET.name: {}}
    )

    assert table.entries[0].demand_weight == pytest.approx(1.0)


# --- ranking by Demand Weight ---


def test_ranks_skills_by_the_demand_their_source_reports():
    table = aggregate_pareto_table(TARGET, [said(EMPLOYERS, {"cryptographic protocols": 83, "security testing": 91})])

    assert table.target_skill == TARGET
    assert [entry.skill for entry in table.entries] == ["security testing", "cryptographic protocols"]


def test_a_rating_is_read_as_a_fraction_of_its_scale():
    table = aggregate_pareto_table(TARGET, [said(EMPLOYERS, {"security testing": 91})])

    assert table.entries[0].demand_weight == pytest.approx(0.91)


def test_a_rank_orders_skills_but_does_not_weigh_them():
    # A place says which comes first, not how much less the tenth is worth.
    table = aggregate_pareto_table(
        TARGET, [said(TOP_TEN, {"reentrancy": 8, "access control": 1, "proxy upgradeability": 10})]
    )

    assert [(entry.skill, entry.demand_weight) for entry in table.entries] == [
        ("access control", 0.0),
        ("reentrancy", 0.0),
        ("proxy upgradeability", 0.0),
    ]


def test_a_rank_places_a_skill_ahead_of_an_equally_weighed_one_and_never_lowers_it():
    # Read as a level, tenth place would drag reentrancy down to 0.505, below
    # the same skill left off the Top 10 entirely.
    table = aggregate_pareto_table(
        TARGET, [said(EMPLOYERS, {"fuzz testing": 91, "reentrancy": 91}), said(TOP_TEN, {"reentrancy": 10})]
    )

    assert [(entry.skill, entry.demand_weight) for entry in table.entries] == [
        ("reentrancy", pytest.approx(0.91)),
        ("fuzz testing", pytest.approx(0.91)),
    ]


def test_with_several_ranks_a_skill_is_ordered_by_the_best_place_any_gives_it():
    # Averaged, the audit list's third place would drop access control behind
    # reentrancy: one more Rank placing it would have lowered it.
    audits = Source("Audit firm top 3", "https://audits.example/", scale=Rank(count=3), measures_whole_target_skill=False)
    table = aggregate_pareto_table(
        TARGET, [said(TOP_TEN, {"access control": 1, "reentrancy": 2}), said(audits, {"access control": 3})]
    )

    assert [entry.skill for entry in table.entries] == ["access control", "reentrancy"]


def test_demand_weight_averages_what_the_weighing_sources_report():
    table = aggregate_pareto_table(
        TARGET, [said(EMPLOYERS, {"gas optimization": 80}), said(SURVEY, {"gas optimization": 60})]
    )

    assert table.entries[0].demand_weight == pytest.approx(0.7)


def test_absence_from_a_source_measuring_the_whole_target_skill_counts_as_no_demand():
    table = aggregate_pareto_table(
        TARGET, [said(EMPLOYERS, {"security testing": 90}), said(SURVEY, {"gas optimization": 60})]
    )

    weights = {entry.skill: entry.demand_weight for entry in table.entries}
    assert weights == {"security testing": pytest.approx(0.45), "gas optimization": pytest.approx(0.3)}


def test_absence_from_a_source_measuring_one_part_of_the_target_skill_counts_for_nothing():
    # The security survey is about security, so its silence on logging says only
    # that it is about something else. The employer survey measures the whole
    # occupation, so its silence on access control is a real absence of demand.
    table = aggregate_pareto_table(
        TARGET, [said(EMPLOYERS, {"logging": 68}), said(SECURITY_SURVEY, {"access control": 80})]
    )

    weights = {entry.skill: entry.demand_weight for entry in table.entries}
    assert weights == {"logging": pytest.approx(0.68), "access control": pytest.approx(0.4)}


def test_a_citation_without_a_demand_its_source_can_read_is_left_out_as_silence():
    # Kept unweighed, "b" would escape the employer survey's 0.
    extraction = SourceExtraction(EMPLOYERS, (CitedSkill("a", 90), CitedSkill("b", None), CitedSkill("c", 500)))

    table = aggregate_pareto_table(TARGET, [extraction, said(SECURITY_SURVEY, {"b": 100, "c": 100})])

    assert [(entry.skill, entry.demand_weight, entry.sources) for entry in table.entries] == [
        ("a", pytest.approx(0.9), ("Employer survey",)),
        ("b", pytest.approx(0.5), ("Security survey",)),
        ("c", pytest.approx(0.5), ("Security survey",)),
    ]


def test_a_citation_left_out_is_recorded_on_the_table_so_the_shortfall_shows():
    extraction = SourceExtraction(EMPLOYERS, (CitedSkill("a", 90), CitedSkill("b", None)))

    table = aggregate_pareto_table(TARGET, [extraction])

    assert table.unreadable_citations == (("Employer survey", "b"),)


def test_a_source_that_reported_nothing_readable_does_not_count_its_silence():
    # An extraction that failed measured nothing, so its silence is not a report
    # of no demand. "Contributed nothing" in the printed table is what shows it.
    failed = SourceExtraction(EMPLOYERS, (CitedSkill("a", None),))

    table = aggregate_pareto_table(TARGET, [failed, said(SECURITY_SURVEY, {"access control": 100})])

    assert [(entry.skill, entry.demand_weight) for entry in table.entries] == [
        ("access control", pytest.approx(1.0))
    ]


def test_a_source_with_no_scale_adds_entries_but_cannot_weigh_them():
    table = aggregate_pareto_table(
        TARGET, [said(EMPLOYERS, {"security testing": 90}), listed(OBJECTIVES, "account abstraction")]
    )

    assert [(entry.skill, entry.demand_weight, entry.sources) for entry in table.entries] == [
        ("security testing", pytest.approx(0.9), ("Employer survey",)),
        ("account abstraction", 0.0, ("Cyfrin exam objectives",)),
    ]


def test_a_source_with_no_scale_does_not_dilute_a_skill_it_shares():
    table = aggregate_pareto_table(
        TARGET, [said(EMPLOYERS, {"gas optimization": 80}), listed(OBJECTIVES, "gas optimization")]
    )

    assert table.entries[0].demand_weight == pytest.approx(0.8)
    assert table.entries[0].sources == ("Employer survey", "Cyfrin exam objectives")


def test_with_no_source_that_weighs_skills_every_entry_has_zero_demand_weight():
    table = aggregate_pareto_table(TARGET, [listed(OBJECTIVES, "opcodes", "account abstraction")])

    assert [(entry.skill, entry.demand_weight) for entry in table.entries] == [
        ("account abstraction", 0.0),
        ("opcodes", 0.0),
    ]


def test_a_source_citing_one_skill_twice_counts_once_at_its_strongest_report():
    table = aggregate_pareto_table(
        TARGET, [said(EMPLOYERS, {"gas optimization": 80, "Gas Optimization (opcodes)": 60})]
    )

    assert [(entry.demand_weight, entry.sources) for entry in table.entries] == [
        (pytest.approx(0.8), ("Employer survey",))
    ]


def test_skills_with_equal_demand_weight_are_ordered_by_name():
    table = aggregate_pareto_table(TARGET, [said(EMPLOYERS, {"logging": 68, "ERC-20 standard": 68})])

    assert [entry.skill for entry in table.entries] == ["ERC-20 standard", "logging"]


def test_blank_and_whitespace_only_skills_are_ignored():
    table = aggregate_pareto_table(TARGET, [said(EMPLOYERS, {"": 90, "   ": 90, "security testing": 91})])

    assert [entry.skill for entry in table.entries] == ["security testing"]


def test_a_skill_that_is_only_punctuation_is_ignored():
    table = aggregate_pareto_table(TARGET, [said(EMPLOYERS, {"(...)": 90, "security testing": 91})])

    assert [entry.skill for entry in table.entries] == ["security testing"]


def test_no_extractions_produces_an_empty_table():
    assert aggregate_pareto_table(TARGET, []).entries == ()


# --- reading what the extraction model says a Source reported ---


def test_parses_skills_with_the_demand_a_source_reports():
    content = 'Sure: [{"skill": "security testing", "demand": 91}, {"skill": "logging", "demand": 68}]'

    assert parse_cited_skills(content) == (CitedSkill("security testing", 91), CitedSkill("logging", 68))


def test_a_demand_that_is_not_a_number_is_read_as_none():
    # Whether a citation with no demand still counts is the Source's scale's
    # call, made in aggregate_pareto_table; the parser only reads what was said.
    content = (
        '[{"skill": "no demand"}, {"skill": "a word", "demand": "high"}, '
        '{"skill": "a bool", "demand": true}, "bare name"]'
    )

    assert parse_cited_skills(content) == (
        CitedSkill("no demand"),
        CitedSkill("a word"),
        CitedSkill("a bool"),
        CitedSkill("bare name"),
    )


def test_an_item_with_no_skill_name_is_skipped():
    assert parse_cited_skills('[{"demand": 90}, {"skill": 7}, 42, {"skill": "logging"}]') == (
        CitedSkill("logging"),
    )


def test_an_unusable_response_yields_no_skills():
    assert parse_cited_skills("I could not find any skills.") == ()
    assert parse_cited_skills('[{"skill": ') == ()
    assert parse_cited_skills('{"skill": "logging"}') == ()


# --- reading which Occupational Tasks the model says each skill serves ---
#
# The model is shown the Occupational Tasks numbered from 1 and answers with
# numbers, so it cannot misquote one.

OCCUPATIONAL_TASKS = (TEST_SECURITY, ASSESS_THREATS, CRYPTOGRAPHY)


def test_reads_numbers_as_the_occupational_tasks_each_skill_serves():
    content = 'Here: {"fuzz testing": [1, 2], "signatures": [3]}'

    assert parse_served_occupational_tasks(content, ["fuzz testing", "signatures"], OCCUPATIONAL_TASKS) == {
        "fuzz testing": (TEST_SECURITY, ASSESS_THREATS),
        "signatures": (CRYPTOGRAPHY,),
    }


def test_serving_none_is_read_only_from_an_explicit_empty_answer():
    # A skill left out is unanswered, not "serves none": read as none it would
    # take a whole-skill Source's 0 that the Source never reported.
    served = parse_served_occupational_tasks('{"fuzz testing": []}', ["fuzz testing", "opcodes"], OCCUPATIONAL_TASKS)

    assert served == {"fuzz testing": ()}


def test_an_answer_naming_a_number_not_on_the_list_leaves_the_skill_unanswered():
    # Keeping the valid half of [1, 4] could drop the strongest Occupational Task, so the
    # whole answer is unusable.
    content = '{"a": [4], "b": [0], "c": [1, 4], "d": ["2"], "e": [true], "f": [2]}'

    assert parse_served_occupational_tasks(content, ["a", "b", "c", "d", "e", "f"], OCCUPATIONAL_TASKS) == {"f": (ASSESS_THREATS,)}


def test_a_lone_number_is_read_as_the_one_occupational_task_it_plainly_means():
    assert parse_served_occupational_tasks('{"signatures": 3}', ["signatures"], OCCUPATIONAL_TASKS) == {"signatures": (CRYPTOGRAPHY,)}


def test_an_unusable_response_answers_no_skill():
    assert parse_served_occupational_tasks("I can't classify these.", ["fuzz testing"], OCCUPATIONAL_TASKS) == {}
    assert parse_served_occupational_tasks('{"fuzz testing": [1', ["fuzz testing"], OCCUPATIONAL_TASKS) == {}


# --- merging the spellings different Sources use for the same skill ---
#
# Each Source's extraction names skills in its own words, so two spellings of
# one skill must fold together before what the Sources report about it can be
# combined at all.


def test_skills_differing_only_in_case_are_one_entry():
    table = aggregate_pareto_table(
        TARGET, [said(EMPLOYERS, {"Gas Optimization": 80}), said(SURVEY, {"gas optimization": 60})]
    )

    assert len(table.entries) == 1


def test_skills_differing_only_in_punctuation_are_one_entry():
    # "ERC‑20" uses a non-breaking hyphen; "ERC-20" an ASCII one.
    table = aggregate_pareto_table(
        TARGET, [said(EMPLOYERS, {"ERC‑20 standard": 80}), said(SURVEY, {"erc 20 standard": 60})]
    )

    assert len(table.entries) == 1


def test_a_parenthetical_qualifier_does_not_split_an_entry():
    table = aggregate_pareto_table(
        TARGET,
        [said(EMPLOYERS, {"oracle integration": 80}), said(SURVEY, {"Oracle integration (Chainlink)": 60})],
    )

    assert len(table.entries) == 1


def test_a_merged_entry_still_names_every_source_that_cited_it():
    table = aggregate_pareto_table(
        TARGET,
        [said(EMPLOYERS, {"Gas Optimization": 80}), said(SURVEY, {"gas optimization (opcode costs)": 60})],
    )

    assert table.entries[0].sources == ("Employer survey", "Developer survey")


def test_a_merged_entry_is_shown_under_its_most_cited_spelling():
    third = Source("Third survey", "https://third.example/", scale=Rating(100), measures_whole_target_skill=True)
    table = aggregate_pareto_table(
        TARGET,
        [
            said(EMPLOYERS, {"gas optimization": 80}),
            said(SURVEY, {"gas optimization": 60}),
            said(third, {"Gas Optimization (advanced)": 70}),
        ],
    )

    assert table.entries[0].skill == "gas optimization"


def test_distinct_skills_are_not_merged():
    table = aggregate_pareto_table(TARGET, [said(EMPLOYERS, {"ERC-20 standard": 80, "ERC-721 standard": 70})])

    assert len(table.entries) == 2


def test_canonical_skill_folds_case_punctuation_and_parentheticals():
    assert canonical_skill("ERC‑20 Standard (fungible tokens)") == canonical_skill("erc-20 standard")


# --- applying the alias map that merges differently-worded skills ---


def test_aliases_rewrite_skills_to_their_canonical_name_keeping_their_demand():
    merged = apply_aliases([said(EMPLOYERS, {"role-based permissioning": 80})], {"role-based permissioning": "access control"})

    assert merged == [said(EMPLOYERS, {"access control": 80})]


def test_a_skill_with_no_alias_is_left_as_it_was():
    extractions = [said(EMPLOYERS, {"reentrancy guards": 80})]

    assert apply_aliases(extractions, {"something else": "other"}) == extractions


def test_aliasing_lets_two_sources_report_on_one_skill():
    extractions = [
        said(EMPLOYERS, {"role-based access control": 80}),
        said(SURVEY, {"Role-based permissioning": 60}),
    ]
    aliases = {"role-based access control": "access control", "Role-based permissioning": "access control"}

    table = aggregate_pareto_table(TARGET, apply_aliases(extractions, aliases))

    assert [entry.skill for entry in table.entries] == ["access control"]
    assert table.entries[0].sources == ("Employer survey", "Developer survey")


def test_an_empty_alias_map_changes_nothing():
    extractions = [said(EMPLOYERS, {"gas optimization": 80})]

    assert apply_aliases(extractions, {}) == extractions
