from pareto_ai.analyst import (
    ParetoEntry,
    Source,
    SourceExtraction,
    aggregate_pareto_table,
    apply_aliases,
    canonical_skill,
)

DOCS = Source("Solidity Docs", "https://docs.soliditylang.org/")
ROADMAP = Source("roadmap.sh", "https://roadmap.sh/blockchain")
ETHEREUM = Source("Ethereum.org", "https://ethereum.org/en/developers/docs/")


def test_ranks_skills_by_number_of_distinct_sources_citing_them():
    extractions = [
        SourceExtraction(source=DOCS, skills=("reentrancy guards", "gas optimization")),
        SourceExtraction(source=ROADMAP, skills=("gas optimization", "ERC-20 standard")),
        SourceExtraction(source=ETHEREUM, skills=("gas optimization",)),
    ]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert table.target_skill == "smart contract development"
    assert [entry.skill for entry in table.entries] == [
        "gas optimization",
        "ERC-20 standard",
        "reentrancy guards",
    ]
    assert table.entries[0].frequency == 3
    assert set(table.entries[0].sources) == {"Solidity Docs", "roadmap.sh", "Ethereum.org"}


def test_a_source_citing_the_same_skill_twice_counts_once():
    extractions = [SourceExtraction(source=DOCS, skills=("gas optimization", "gas optimization"))]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert table.entries == (ParetoEntry(skill="gas optimization", frequency=1, sources=("Solidity Docs",)),)


def test_blank_and_whitespace_only_skills_are_ignored():
    extractions = [SourceExtraction(source=DOCS, skills=("", "   ", "gas optimization"))]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert [entry.skill for entry in table.entries] == ["gas optimization"]


def test_no_extractions_produces_an_empty_table():
    table = aggregate_pareto_table("smart contract development", [])

    assert table.entries == ()


# --- merging the spellings different sources use for the same skill ---
#
# Each source's extraction names skills in its own words, so without merging
# almost every entry sits at frequency=1 and the "top 20% by frequency" cut
# degenerates into alphabetical order.


def test_skills_differing_only_in_case_are_one_entry():
    extractions = [
        SourceExtraction(source=DOCS, skills=("Gas Optimization",)),
        SourceExtraction(source=ROADMAP, skills=("gas optimization",)),
    ]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert len(table.entries) == 1
    assert table.entries[0].frequency == 2


def test_skills_differing_only_in_punctuation_are_one_entry():
    # "ERC‑20" uses a non-breaking hyphen; "ERC-20" an ASCII one.
    extractions = [
        SourceExtraction(source=DOCS, skills=("ERC‑20 standard",)),
        SourceExtraction(source=ROADMAP, skills=("ERC-20 standard",)),
        SourceExtraction(source=ETHEREUM, skills=("erc 20 standard",)),
    ]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert len(table.entries) == 1
    assert table.entries[0].frequency == 3


def test_a_parenthetical_qualifier_does_not_split_an_entry():
    extractions = [
        SourceExtraction(source=DOCS, skills=("oracle integration",)),
        SourceExtraction(source=ROADMAP, skills=("Oracle integration (Chainlink)",)),
    ]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert len(table.entries) == 1
    assert table.entries[0].frequency == 2


def test_a_merged_entry_still_names_every_source_that_cited_it():
    extractions = [
        SourceExtraction(source=DOCS, skills=("Gas Optimization",)),
        SourceExtraction(source=ROADMAP, skills=("gas optimization (opcode costs)",)),
        SourceExtraction(source=ETHEREUM, skills=("gas-optimization",)),
    ]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert set(table.entries[0].sources) == {"Solidity Docs", "roadmap.sh", "Ethereum.org"}


def test_a_merged_entry_is_shown_under_its_most_cited_spelling():
    extractions = [
        SourceExtraction(source=DOCS, skills=("gas optimization",)),
        SourceExtraction(source=ROADMAP, skills=("gas optimization",)),
        SourceExtraction(source=ETHEREUM, skills=("Gas Optimization (advanced)",)),
    ]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert table.entries[0].skill == "gas optimization"


def test_a_skill_that_is_only_punctuation_is_ignored():
    extractions = [SourceExtraction(source=DOCS, skills=("(...)", "gas optimization"))]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert [entry.skill for entry in table.entries] == ["gas optimization"]


def test_distinct_skills_are_not_merged():
    extractions = [
        SourceExtraction(source=DOCS, skills=("ERC-20 standard", "ERC-721 standard")),
    ]

    table = aggregate_pareto_table("smart contract development", extractions)

    assert len(table.entries) == 2


def test_canonical_skill_folds_case_punctuation_and_parentheticals():
    assert canonical_skill("ERC‑20 Standard (fungible tokens)") == canonical_skill("erc-20 standard")


# --- applying the alias map that merges differently-worded skills ---


def test_aliases_rewrite_skills_to_their_canonical_name():
    extractions = [SourceExtraction(source=DOCS, skills=("role-based permissioning",))]

    merged = apply_aliases(extractions, {"role-based permissioning": "access control"})

    assert merged == [SourceExtraction(source=DOCS, skills=("access control",))]


def test_a_skill_with_no_alias_is_left_as_it_was():
    extractions = [SourceExtraction(source=DOCS, skills=("reentrancy guards",))]

    merged = apply_aliases(extractions, {"something else": "other"})

    assert merged == [SourceExtraction(source=DOCS, skills=("reentrancy guards",))]


def test_aliasing_lets_two_sources_corroborate_one_skill():
    # The whole point: without this both land at frequency=1 and the top-20% cut
    # has nothing to rank by.
    extractions = [
        SourceExtraction(source=DOCS, skills=("role-based access control",)),
        SourceExtraction(source=ROADMAP, skills=("Role-based permissioning",)),
    ]
    aliases = {"role-based access control": "access control", "Role-based permissioning": "access control"}

    table = aggregate_pareto_table("smart contract development", apply_aliases(extractions, aliases))

    assert len(table.entries) == 1
    assert table.entries[0].frequency == 2


def test_an_empty_alias_map_changes_nothing():
    extractions = [SourceExtraction(source=DOCS, skills=("gas optimization",))]

    assert apply_aliases(extractions, {}) == extractions
