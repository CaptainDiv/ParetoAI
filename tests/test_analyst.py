from pareto_ai.analyst import ParetoEntry, Source, SourceExtraction, aggregate_pareto_table

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
