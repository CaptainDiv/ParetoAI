from dataclasses import fields

from pareto_ai.proof_of_skill import (
    ISSUE_BODY_CHARS,
    GroundTruth,
    ProofOfSkillTask,
    TaskCandidate,
    assign_proof_of_skill_tasks,
    parse_task_candidates,
)

REPO = "OpenZeppelin/openzeppelin-contracts"


def issue_node(number: int, state: str = "CLOSED", body: str = "the issue body") -> dict:
    return {
        "number": number,
        "state": state,
        "title": f"issue {number}",
        "bodyText": body,
        "url": f"https://github.com/{REPO}/issues/{number}",
    }


def graphql_node(
    pull_number: int,
    issue_number: int | None,
    *,
    merge_commit: str | None = "a" * 40,
    parent: str | None = "b" * 40,
    body: str = "the issue body",
    issue_state: str = "CLOSED",
    extra_issues: tuple[int, ...] = (),
    rebase_allowed: bool = False,
) -> dict:
    """One node of a `search(type: ISSUE)` response, shaped as GitHub returns it."""
    issues = [] if issue_number is None else [issue_node(issue_number, issue_state, body)]
    issues += [issue_node(n, "CLOSED", body) for n in extra_issues]
    return {
        "number": pull_number,
        "title": f"pr {pull_number}",
        "bodyText": f"fixes something, pr {pull_number}",
        "url": f"https://github.com/{REPO}/pull/{pull_number}",
        "repository": {"nameWithOwner": REPO, "rebaseMergeAllowed": rebase_allowed},
        "mergeCommit": None
        if merge_commit is None
        else {"oid": merge_commit, "parents": {"nodes": [] if parent is None else [{"oid": parent}]}},
        "closingIssuesReferences": {"nodes": issues},
    }


def payload(*nodes: dict) -> dict:
    return {"data": {"search": {"nodes": list(nodes)}}}


# --- parsing the live GitHub response ---


def test_parses_a_merged_pr_with_a_linked_issue_into_a_candidate():
    candidates = parse_task_candidates(payload(graphql_node(6691, 5025)))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.task == ProofOfSkillTask(
        repo=REPO,
        issue_number=5025,
        title="issue 5025",
        body="the issue body",
        url=f"https://github.com/{REPO}/issues/5025",
    )
    assert candidate.ground_truth == GroundTruth(
        repo=REPO,
        issue_number=5025,
        pull_number=6691,
        pull_title="pr 6691",
        pull_body="fixes something, pr 6691",
        merge_commit_sha="a" * 40,
        pre_fix_commit_sha="b" * 40,
        url=f"https://github.com/{REPO}/pull/6691",
    )


def test_skips_a_pull_request_whose_linked_issue_is_still_open():
    # Real case: OpenZeppelin PR #5687 links issue #5666, which is OPEN. CONTEXT.md
    # defines a Proof-of-Skill Task as a *closed* issue with a known merged fix,
    # and `linked:issue` in the search query does not filter on issue state.
    assert parse_task_candidates(payload(graphql_node(5687, 5666, issue_state="OPEN"))) == ()


def test_skips_a_repo_that_allows_rebase_merging():
    # A rebase-merged multi-commit PR leaves mergeCommit as the tip of the
    # rebased series, so its single parent is an earlier commit *of the same fix*
    # and an Attempt would start with part of its own answer applied. A one-parent
    # merge commit cannot be told apart from a squash, so the repo setting is the
    # only reliable discriminator.
    assert parse_task_candidates(payload(graphql_node(1, 2, rebase_allowed=True))) == ()


def test_the_same_issue_closed_by_two_pull_requests_yields_one_candidate():
    # A follow-up PR re-linking an issue is common. Two candidates for one issue
    # would let two Modules be proven by the same issue, and the second fix's
    # parent commit would overwrite the first's clone point.
    candidates = parse_task_candidates(payload(graphql_node(1, 500), graphql_node(2, 500)))

    assert len(candidates) == 1


def test_a_candidate_carries_the_merged_pull_requests_own_content():
    candidates = parse_task_candidates(payload(graphql_node(6691, 5025)))

    assert candidates[0].ground_truth.pull_title == "pr 6691"
    assert candidates[0].ground_truth.pull_body == "fixes something, pr 6691"


def test_skips_a_pull_request_that_closes_no_issue():
    # Most of OpenZeppelin's recent PRs are like this; without an issue there is
    # nothing for a Student Agent to be given as a task.
    assert parse_task_candidates(payload(graphql_node(6804, None))) == ()


def test_skips_a_pull_request_with_no_merge_commit():
    assert parse_task_candidates(payload(graphql_node(1, 2, merge_commit=None))) == ()


def test_skips_a_pull_request_whose_merge_commit_has_no_parent():
    # Without the parent there is no commit to clone an Attempt at, so the
    # Ground Truth is unusable even though the fix exists.
    assert parse_task_candidates(payload(graphql_node(1, 2, parent=None))) == ()


def test_takes_only_the_first_linked_issue_so_one_fix_yields_one_task():
    candidates = parse_task_candidates(payload(graphql_node(6691, 5025, extra_issues=(5026,))))

    assert [candidate.task.issue_number for candidate in candidates] == [5025]


def test_ignores_nodes_that_are_not_pull_requests():
    # A search of type ISSUE returns {} for hits that aren't PullRequests.
    assert parse_task_candidates(payload({}, graphql_node(1, 2))) == (
        parse_task_candidates(payload(graphql_node(1, 2)))[0],
    )


def test_bounds_a_very_long_issue_body():
    candidates = parse_task_candidates(payload(graphql_node(1, 2, body="x" * (ISSUE_BODY_CHARS + 500))))

    assert len(candidates[0].task.body) == ISSUE_BODY_CHARS


def test_a_missing_body_becomes_empty_rather_than_none():
    node = graphql_node(1, 2)
    node["closingIssuesReferences"]["nodes"][0]["bodyText"] = None

    assert parse_task_candidates(payload(node))[0].task.body == ""


# --- assigning tasks to Modules, and keeping Ground Truth away from them ---


def candidate(issue_number: int, pull_number: int = 999) -> TaskCandidate:
    return parse_task_candidates(payload(graphql_node(pull_number, issue_number)))[0]


CANDIDATES = (candidate(101, 1), candidate(102, 2), candidate(103, 3))
TOPICS = ("access control", "ERC-20 standard", "upgradeable proxies")


def test_every_module_topic_gets_a_task_when_candidates_suffice():
    assignment = assign_proof_of_skill_tasks(TOPICS, CANDIDATES, {})

    assert set(assignment.tasks) == set(TOPICS)
    assert assignment.topics_without_tasks == ()


def test_honours_the_pairing_the_model_proposed():
    pairings = {"ERC-20 standard": f"{REPO}#103"}

    assignment = assign_proof_of_skill_tasks(TOPICS, CANDIDATES, pairings)

    assert assignment.tasks["ERC-20 standard"].issue_number == 103


def test_never_assigns_one_issue_to_two_modules():
    # Each Attempt runs against its own task; reusing an issue would mean two
    # Modules proven by the same fix.
    pairings = {topic: f"{REPO}#101" for topic in TOPICS}

    assignment = assign_proof_of_skill_tasks(TOPICS, CANDIDATES, pairings)

    assigned = [task.issue_number for task in assignment.tasks.values()]
    assert sorted(assigned) == [101, 102, 103]


def test_a_pairing_naming_an_unknown_task_falls_back_to_an_unused_candidate():
    assignment = assign_proof_of_skill_tasks(TOPICS, CANDIDATES, {"access control": "nope/nope#1"})

    assert assignment.tasks["access control"].issue_number in {101, 102, 103}
    assert assignment.topics_without_tasks == ()


def test_a_topic_that_fell_back_is_reported_as_unpaired():
    # ADR-0002: degrading is only allowed where it shows in the output. A fallback
    # task looks exactly like a chosen one, so it has to be said out loud.
    assignment = assign_proof_of_skill_tasks(TOPICS, CANDIDATES, {"access control": f"{REPO}#101"})

    assert "access control" not in assignment.unpaired_topics
    assert set(assignment.unpaired_topics) == {"ERC-20 standard", "upgradeable proxies"}


def test_total_pairing_failure_reports_every_topic_as_unpaired():
    assignment = assign_proof_of_skill_tasks(TOPICS, CANDIDATES, {})

    assert set(assignment.unpaired_topics) == set(TOPICS)


def test_topics_beyond_the_candidate_supply_are_reported_rather_than_dropped():
    assignment = assign_proof_of_skill_tasks(TOPICS, CANDIDATES[:2], {})

    assert len(assignment.tasks) == 2
    assert len(assignment.topics_without_tasks) == 1
    assert assignment.topics_without_tasks[0] in TOPICS


def test_ground_truth_is_returned_only_for_assigned_tasks():
    assignment = assign_proof_of_skill_tasks(TOPICS[:1], CANDIDATES, {})

    assert len(assignment.ground_truths) == 1


def test_a_task_can_be_looked_up_to_its_ground_truth_by_key():
    assignment = assign_proof_of_skill_tasks(TOPICS, CANDIDATES, {})

    task = assignment.tasks["access control"]
    assert assignment.ground_truths[task.key].issue_number == task.issue_number


def test_a_proof_of_skill_task_carries_nothing_about_its_fix():
    # CONTEXT.md: Ground Truth is never exposed to a Student Agent. A Student
    # Agent is handed Modules, so the task type itself must not carry the fix.
    assert {field.name for field in fields(ProofOfSkillTask)} == {
        "repo",
        "issue_number",
        "title",
        "body",
        "url",
    }


def test_ground_truth_names_the_commit_an_attempt_should_clone_at():
    assignment = assign_proof_of_skill_tasks(TOPICS[:1], CANDIDATES, {})

    ground_truth = next(iter(assignment.ground_truths.values()))
    assert ground_truth.pre_fix_commit_sha == "b" * 40
