import pytest

from pareto_ai.analyst import (
    CitedSkill,
    Rank,
    Rating,
    Source,
    SourceExtraction,
    aggregate_pareto_table,
    apply_aliases,
    canonical_skill,
    parse_cited_skills,
)

TARGET = "smart contract development"

ONET = Source(
    "O*NET Blockchain Engineers",
    "https://www.onetonline.org/link/details/15-1299.07",
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


OBJECTIVES = Source(
    "Cyfrin exam objectives",
    "https://updraft.cyfrin.io/certifications/solidity-certification",
    scale=None,
    measures_whole_target_skill=True,
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


# --- ranking by Demand Weight ---


def test_ranks_skills_by_the_demand_their_source_reports():
    table = aggregate_pareto_table(TARGET, [said(ONET, {"cryptographic protocols": 83, "security testing": 91})])

    assert table.target_skill == TARGET
    assert [entry.skill for entry in table.entries] == ["security testing", "cryptographic protocols"]


def test_a_rating_is_read_as_a_fraction_of_its_scale():
    table = aggregate_pareto_table(TARGET, [said(ONET, {"security testing": 91})])

    assert table.entries[0].demand_weight == pytest.approx(0.91)


def test_a_rank_is_read_with_first_place_as_full_demand():
    table = aggregate_pareto_table(
        TARGET, [said(TOP_TEN, {"reentrancy": 8, "access control": 1, "proxy upgradeability": 10})]
    )

    assert [(entry.skill, entry.demand_weight) for entry in table.entries] == [
        ("access control", pytest.approx(1.0)),
        ("reentrancy", pytest.approx(0.3)),
        ("proxy upgradeability", pytest.approx(0.1)),
    ]


def test_demand_weight_averages_what_the_weighing_sources_report():
    table = aggregate_pareto_table(
        TARGET, [said(ONET, {"gas optimization": 80}), said(SURVEY, {"gas optimization": 60})]
    )

    assert table.entries[0].demand_weight == pytest.approx(0.7)


def test_absence_from_a_source_measuring_the_whole_target_skill_counts_as_no_demand():
    table = aggregate_pareto_table(
        TARGET, [said(ONET, {"security testing": 90}), said(SURVEY, {"gas optimization": 60})]
    )

    weights = {entry.skill: entry.demand_weight for entry in table.entries}
    assert weights == {"security testing": pytest.approx(0.45), "gas optimization": pytest.approx(0.3)}


def test_absence_from_a_source_measuring_one_part_of_the_target_skill_counts_for_nothing():
    # The Top 10 is about security, so its silence on logging says only that it
    # is about something else. O*NET measures the whole occupation, so its
    # silence on access control is a real absence of demand.
    table = aggregate_pareto_table(TARGET, [said(ONET, {"logging": 68}), said(TOP_TEN, {"access control": 1})])

    weights = {entry.skill: entry.demand_weight for entry in table.entries}
    assert weights == {"logging": pytest.approx(0.68), "access control": pytest.approx(0.5)}


def test_a_citation_without_a_demand_its_source_can_read_is_left_out_as_silence():
    # Kept unweighed, "b" would escape O*NET's 0 and outrank O*NET's own 0.9.
    extraction = SourceExtraction(ONET, (CitedSkill("a", 90), CitedSkill("b", None), CitedSkill("c", 500)))

    table = aggregate_pareto_table(TARGET, [extraction, said(TOP_TEN, {"b": 1, "c": 1})])

    assert [(entry.skill, entry.demand_weight, entry.sources) for entry in table.entries] == [
        ("a", pytest.approx(0.9), ("O*NET Blockchain Engineers",)),
        ("b", pytest.approx(0.5), ("OWASP Smart Contract Top 10",)),
        ("c", pytest.approx(0.5), ("OWASP Smart Contract Top 10",)),
    ]


def test_a_citation_left_out_is_recorded_on_the_table_so_the_shortfall_shows():
    extraction = SourceExtraction(ONET, (CitedSkill("a", 90), CitedSkill("b", None)))

    table = aggregate_pareto_table(TARGET, [extraction])

    assert table.unreadable_citations == (("O*NET Blockchain Engineers", "b"),)


def test_a_source_that_reported_nothing_readable_does_not_count_its_silence():
    # An extraction that failed measured nothing, so its silence is not a report
    # of no demand. "Contributed nothing" in the printed table is what shows it.
    failed = SourceExtraction(ONET, (CitedSkill("a", None),))

    table = aggregate_pareto_table(TARGET, [failed, said(TOP_TEN, {"access control": 1})])

    assert [(entry.skill, entry.demand_weight) for entry in table.entries] == [
        ("access control", pytest.approx(1.0))
    ]


def test_a_source_with_no_scale_adds_entries_but_cannot_weigh_them():
    table = aggregate_pareto_table(
        TARGET, [said(ONET, {"security testing": 90}), listed(OBJECTIVES, "account abstraction")]
    )

    assert [(entry.skill, entry.demand_weight, entry.sources) for entry in table.entries] == [
        ("security testing", pytest.approx(0.9), ("O*NET Blockchain Engineers",)),
        ("account abstraction", 0.0, ("Cyfrin exam objectives",)),
    ]


def test_a_source_with_no_scale_does_not_dilute_a_skill_it_shares():
    table = aggregate_pareto_table(
        TARGET, [said(ONET, {"gas optimization": 80}), listed(OBJECTIVES, "gas optimization")]
    )

    assert table.entries[0].demand_weight == pytest.approx(0.8)
    assert table.entries[0].sources == ("O*NET Blockchain Engineers", "Cyfrin exam objectives")


def test_with_no_source_that_weighs_skills_every_entry_has_zero_demand_weight():
    table = aggregate_pareto_table(TARGET, [listed(OBJECTIVES, "opcodes", "account abstraction")])

    assert [(entry.skill, entry.demand_weight) for entry in table.entries] == [
        ("account abstraction", 0.0),
        ("opcodes", 0.0),
    ]


def test_a_source_citing_one_skill_twice_counts_once_at_its_strongest_report():
    table = aggregate_pareto_table(
        TARGET, [said(ONET, {"gas optimization": 80, "Gas Optimization (opcodes)": 60})]
    )

    assert [(entry.demand_weight, entry.sources) for entry in table.entries] == [
        (pytest.approx(0.8), ("O*NET Blockchain Engineers",))
    ]


def test_skills_with_equal_demand_weight_are_ordered_by_name():
    table = aggregate_pareto_table(TARGET, [said(ONET, {"logging": 68, "ERC-20 standard": 68})])

    assert [entry.skill for entry in table.entries] == ["ERC-20 standard", "logging"]


def test_blank_and_whitespace_only_skills_are_ignored():
    table = aggregate_pareto_table(TARGET, [said(ONET, {"": 90, "   ": 90, "security testing": 91})])

    assert [entry.skill for entry in table.entries] == ["security testing"]


def test_a_skill_that_is_only_punctuation_is_ignored():
    table = aggregate_pareto_table(TARGET, [said(ONET, {"(...)": 90, "security testing": 91})])

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


# --- merging the spellings different Sources use for the same skill ---
#
# Each Source's extraction names skills in its own words, so two spellings of
# one skill must fold together before what the Sources report about it can be
# combined at all.


def test_skills_differing_only_in_case_are_one_entry():
    table = aggregate_pareto_table(
        TARGET, [said(ONET, {"Gas Optimization": 80}), said(SURVEY, {"gas optimization": 60})]
    )

    assert len(table.entries) == 1


def test_skills_differing_only_in_punctuation_are_one_entry():
    # "ERC‑20" uses a non-breaking hyphen; "ERC-20" an ASCII one.
    table = aggregate_pareto_table(
        TARGET, [said(ONET, {"ERC‑20 standard": 80}), said(SURVEY, {"erc 20 standard": 60})]
    )

    assert len(table.entries) == 1


def test_a_parenthetical_qualifier_does_not_split_an_entry():
    table = aggregate_pareto_table(
        TARGET,
        [said(ONET, {"oracle integration": 80}), said(SURVEY, {"Oracle integration (Chainlink)": 60})],
    )

    assert len(table.entries) == 1


def test_a_merged_entry_still_names_every_source_that_cited_it():
    table = aggregate_pareto_table(
        TARGET,
        [said(ONET, {"Gas Optimization": 80}), said(SURVEY, {"gas optimization (opcode costs)": 60})],
    )

    assert table.entries[0].sources == ("O*NET Blockchain Engineers", "Developer survey")


def test_a_merged_entry_is_shown_under_its_most_cited_spelling():
    third = Source("Third survey", "https://third.example/", scale=Rating(100), measures_whole_target_skill=True)
    table = aggregate_pareto_table(
        TARGET,
        [
            said(ONET, {"gas optimization": 80}),
            said(SURVEY, {"gas optimization": 60}),
            said(third, {"Gas Optimization (advanced)": 70}),
        ],
    )

    assert table.entries[0].skill == "gas optimization"


def test_distinct_skills_are_not_merged():
    table = aggregate_pareto_table(TARGET, [said(ONET, {"ERC-20 standard": 80, "ERC-721 standard": 70})])

    assert len(table.entries) == 2


def test_canonical_skill_folds_case_punctuation_and_parentheticals():
    assert canonical_skill("ERC‑20 Standard (fungible tokens)") == canonical_skill("erc-20 standard")


# --- applying the alias map that merges differently-worded skills ---


def test_aliases_rewrite_skills_to_their_canonical_name_keeping_their_demand():
    merged = apply_aliases([said(ONET, {"role-based permissioning": 80})], {"role-based permissioning": "access control"})

    assert merged == [said(ONET, {"access control": 80})]


def test_a_skill_with_no_alias_is_left_as_it_was():
    extractions = [said(ONET, {"reentrancy guards": 80})]

    assert apply_aliases(extractions, {"something else": "other"}) == extractions


def test_aliasing_lets_two_sources_report_on_one_skill():
    extractions = [
        said(ONET, {"role-based access control": 80}),
        said(SURVEY, {"Role-based permissioning": 60}),
    ]
    aliases = {"role-based access control": "access control", "Role-based permissioning": "access control"}

    table = aggregate_pareto_table(TARGET, apply_aliases(extractions, aliases))

    assert [entry.skill for entry in table.entries] == ["access control"]
    assert table.entries[0].sources == ("O*NET Blockchain Engineers", "Developer survey")


def test_an_empty_alias_map_changes_nothing():
    extractions = [said(ONET, {"gas optimization": 80})]

    assert apply_aliases(extractions, {}) == extractions
