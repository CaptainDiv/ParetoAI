"""Proof-of-Skill Task sourcing: a real closed issue per Module, plus its hidden fix.

A Proof-of-Skill Task is a real closed GitHub issue; its Ground Truth is the
merged PR that closed it. CONTEXT.md makes one rule non-negotiable here — Ground
Truth is never exposed to a Student Agent — so the two are separate types from
the start:

  ProofOfSkillTask  the issue, and nothing whatsoever about its fix
  GroundTruth       the merged PR, keyed by task, returned in its own mapping

A Student Agent is handed Modules (via retrieval), and a Module can only ever
reach a ProofOfSkillTask, so for a Module the separation is structural: there is
no type-level path from it to a fix, and a test pins ProofOfSkillTask's fields to
keep it that way.

That guarantee stops at the Module. A TaskAssignment deliberately holds both
halves — something has to, to hand the Judge its half — so whatever sets up an
Attempt must pass it `assignment.tasks`, never the assignment itself. That part
is call-site discipline, not structure.

assign_proof_of_skill_tasks is the pure, TDD'd core; GitHub fetching and the
topic-to-issue pairing are live and smoke-tested, per the spec's Testing
Decisions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import neatlogs

from pareto_ai.analyst import run_analyst
from pareto_ai.curriculum import Curriculum, run_curriculum, with_proof_of_skill_tasks
from pareto_ai.llm import complete, json_object

ISSUE_BODY_CHARS = 4000
PAIRING_MAX_TOKENS = 4096
CANDIDATES_PER_REPO = 60

# Target repos for Proof-of-Skill Tasks. The spec left this to implementation
# time, so it was measured rather than guessed: counting merged PRs that
# *formally* link a closing issue (`is:pr is:merged linked:issue`), which is the
# linkage a Ground Truth depends on, gave
#
#   OpenZeppelin/openzeppelin-contracts  483      Uniswap/v3-core   73
#   foundry-rs/forge-std                  56      Vectorized/solady 26
#   transmissions11/solmate                6      ProjectOpenSea/seaport  3
#
# OpenZeppelin wins on every count that matters: by far the deepest pool of
# properly linked fixes, MIT licensed so an Attempt may clone it freely, a
# Hardhat suite the Judge agent can actually run, and subject matter (tokens,
# access control, proxies, cryptographic utilities) that maps onto the kind of
# topic the Curriculum agent produces. Note the pool is only findable by search:
# the most recently *updated* PRs link no issues at all, so ordering by recency
# finds nothing.
DEFAULT_TARGET_REPOS: tuple[str, ...] = ("OpenZeppelin/openzeppelin-contracts",)

_CANDIDATE_QUERY = """
query($q: String!, $first: Int!) {
  search(query: $q, type: ISSUE, first: $first) {
    nodes {
      ... on PullRequest {
        number
        title
        bodyText
        url
        repository { nameWithOwner rebaseMergeAllowed }
        mergeCommit { oid parents(first: 1) { nodes { oid } } }
        closingIssuesReferences(first: 1) { nodes { number state title bodyText url } }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class ProofOfSkillTask:
    """A real closed GitHub issue assigned to a Module.

    This is the whole of what a Student Agent may see. Adding anything about the
    fix here would leak Ground Truth into Student Agent input by construction —
    a test pins these field names for that reason.
    """

    repo: str
    issue_number: int
    title: str
    body: str
    url: str

    @property
    def key(self) -> str:
        """How a Ground Truth is addressed, so the Judge can find it from a task."""
        return f"{self.repo}#{self.issue_number}"


@dataclass(frozen=True)
class GroundTruth:
    """The merged PR that closed a Proof-of-Skill Task. For the Judge agent only."""

    repo: str
    issue_number: int
    pull_number: int
    pull_title: str
    pull_body: str
    merge_commit_sha: str
    # Where an Attempt's isolated clone starts. Only trustworthy because a
    # rebase-merging repo is refused during sourcing — see parse_task_candidates.
    pre_fix_commit_sha: str
    url: str

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.issue_number}"


@dataclass(frozen=True)
class TaskCandidate:
    """A sourced issue-and-fix pair, before any Module claims it."""

    task: ProofOfSkillTask
    ground_truth: GroundTruth


@dataclass(frozen=True)
class TaskAssignment:
    """Which Module gets which task, with the fixes alongside but not inside.

    `tasks` is Student-Agent-visible; `ground_truths` is the Judge's and must
    never reach a Student Agent. They sit side by side rather than nested so that
    `tasks` can be handed on by itself — but this object holds both, so passing
    the whole of it to anything that runs an Attempt would leak the fixes.
    """

    tasks: Mapping[str, ProofOfSkillTask] = field(default_factory=dict)  # Module topic -> task
    ground_truths: Mapping[str, GroundTruth] = field(default_factory=dict)  # task key -> fix
    topics_without_tasks: tuple[str, ...] = ()  # reported, never silently dropped
    # Topics that got a task by falling back rather than by the model's choice.
    # ADR-0002 only permits degrading where the degradation shows in the output,
    # and a fallback task is otherwise indistinguishable from a chosen one: both
    # print as a real issue next to a real topic. This is what makes it show.
    unpaired_topics: tuple[str, ...] = ()


# --- pure core -------------------------------------------------------------


def parse_task_candidates(payload: Mapping) -> tuple[TaskCandidate, ...]:
    """Read candidates out of a GitHub `search(type: ISSUE)` response.

    A node survives only if every one of these holds, because each failure makes
    the task unusable in a way that would otherwise be silent:

    - it links an issue, and that issue is CLOSED. `linked:issue` in the search
      query does not filter on issue state, so a merged PR can name an issue
      that is still open, and CONTEXT.md defines a Proof-of-Skill Task as a
      closed issue with a known merged fix.
    - it has a merge commit with a first parent. That parent is where an Attempt
      clones, so without it the Attempt cannot be set up fairly.
    - its repo does not allow rebase merging. A rebase-merged multi-commit PR
      leaves the merge commit as the tip of the rebased series, so its single
      parent is an earlier commit *of the same fix* and the clone would begin
      with part of the answer already applied. A one-parent merge commit cannot
      be distinguished from a squash after the fact, so the repo setting is the
      only reliable discriminator — the configured OpenZeppelin repo permits
      squash merges only, which is why its merge commits can be trusted.

    One issue yields at most one candidate: a follow-up PR re-linking an issue
    would otherwise let two Modules be proven by the same issue, with the second
    fix's parent silently overwriting the first's clone point.
    """
    by_issue: dict[str, TaskCandidate] = {}
    for node in payload.get("data", {}).get("search", {}).get("nodes", []):
        issues = (node.get("closingIssuesReferences") or {}).get("nodes") or []
        merge_commit = node.get("mergeCommit") or {}
        parents = (merge_commit.get("parents") or {}).get("nodes") or []
        repository = node.get("repository") or {}
        if not issues or not merge_commit.get("oid") or not parents:
            continue
        if repository.get("rebaseMergeAllowed"):
            continue

        issue = issues[0]  # one fix yields one task, however many issues it closed
        if (issue.get("state") or "").upper() != "CLOSED":
            continue

        repo = repository.get("nameWithOwner", "")
        task = ProofOfSkillTask(
            repo=repo,
            issue_number=issue["number"],
            title=issue.get("title") or "",
            body=(issue.get("bodyText") or "")[:ISSUE_BODY_CHARS],
            url=issue.get("url") or "",
        )
        by_issue.setdefault(
            task.key,
            TaskCandidate(
                task=task,
                ground_truth=GroundTruth(
                    repo=repo,
                    issue_number=issue["number"],
                    pull_number=node["number"],
                    pull_title=node.get("title") or "",
                    pull_body=(node.get("bodyText") or "")[:ISSUE_BODY_CHARS],
                    merge_commit_sha=merge_commit["oid"],
                    pre_fix_commit_sha=parents[0]["oid"],
                    url=node.get("url") or "",
                ),
            ),
        )
    return tuple(by_issue.values())


def assign_proof_of_skill_tasks(
    topics: Sequence[str],
    candidates: Sequence[TaskCandidate],
    pairings: Mapping[str, str],
) -> TaskAssignment:
    """Give each Module topic one task, and keep each task's fix out of it.

    `pairings` is the model's topic-to-task suggestion. It is advice: a topic
    whose suggestion is unknown or already taken still gets a task, because every
    Module needs one, and no issue is ever used twice — two Modules proven by the
    same fix would not be two independent Attempts.
    """
    by_key = {candidate.task.key: candidate for candidate in candidates}
    unclaimed = list(by_key)  # keys, so a repeated candidate cannot be claimed twice

    tasks: dict[str, ProofOfSkillTask] = {}
    ground_truths: dict[str, GroundTruth] = {}
    without: list[str] = []
    unpaired: list[str] = []
    for topic in topics:
        suggested = pairings.get(topic)
        key = suggested if suggested in by_key and suggested in unclaimed else None
        fell_back = key is None
        if key is None:
            key = unclaimed[0] if unclaimed else None
        if key is None:
            without.append(topic)
            continue
        if fell_back:
            unpaired.append(topic)
        unclaimed.remove(key)
        claimed = by_key[key]
        tasks[topic] = claimed.task
        ground_truths[claimed.ground_truth.key] = claimed.ground_truth

    return TaskAssignment(
        tasks=tasks,
        ground_truths=ground_truths,
        topics_without_tasks=tuple(without),
        unpaired_topics=tuple(unpaired),
    )


# --- live GitHub sourcing --------------------------------------------------


def _search_pull_requests(search: str, first: int) -> Mapping:
    """Run the candidate query through the gh CLI, which already holds the auth."""
    # GitHub answers in UTF-8 and issue bodies are full of emoji, so the decoding
    # must be stated: text=True alone would use the platform locale (cp1252 on
    # Windows) and die on the first one.
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={_CANDIDATE_QUERY}", "-f", f"q={search}", "-F", f"first={first}"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    payload = json.loads(result.stdout)
    # GraphQL reports failures in a 200 body, so a non-zero exit code is not
    # enough to know the query worked.
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL returned errors: {payload['errors']}")
    return payload


@neatlogs.span(kind="AGENT", role="ProofOfSkill")
def fetch_task_candidates(repos: Sequence[str] = DEFAULT_TARGET_REPOS) -> tuple[TaskCandidate, ...]:
    """Closed issues with a merged fix, across the target repos.

    Selected by search rather than recency: a repo's most recently updated PRs
    may link no issues at all (OpenZeppelin's top 20 link none), so ordering by
    date finds an empty pool while `linked:issue` finds hundreds.
    """
    candidates: list[TaskCandidate] = []
    for repo in repos:
        payload = _search_pull_requests(
            f"repo:{repo} is:pr is:merged linked:issue", CANDIDATES_PER_REPO
        )
        candidates.extend(parse_task_candidates(payload))
    return tuple(candidates)


@neatlogs.span(kind="AGENT", role="ProofOfSkill")
def pair_tasks_to_modules(
    target_skill: str, topics: Sequence[str], candidates: Sequence[TaskCandidate]
) -> dict[str, str]:
    """Ask which sourced issue best proves each Module's topic.

    Retried once and then allowed to give up, per ADR-0002. Giving up is only
    acceptable because every topic that ends up with a fallback task is named in
    TaskAssignment.unpaired_topics and printed as such — without that, a skipped
    pairing would be indistinguishable from a good one.
    """
    if not topics or not candidates:
        return {}
    prompt = (
        f'A curriculum on "{target_skill}" has these modules:\n'
        + "\n".join(f"- {topic}" for topic in topics)
        + "\n\nThese real GitHub issues are available as proof-of-skill tasks:\n"
        + "\n".join(f"- {candidate.task.key} :: {candidate.task.title}" for candidate in candidates)
        + "\n\nFor each module, choose the ONE issue that would best prove a learner "
        "can apply what that module teaches. Use each issue at most once. Use only "
        "the identifiers listed above, exactly as written.\n"
        'Respond with ONLY a JSON object of the form {"module topic": "owner/repo#123"}. '
        "No prose."
    )
    return _pairing_attempt(prompt) or _pairing_attempt(prompt) or {}


def _pairing_attempt(prompt: str) -> dict[str, str] | None:
    """One pairing call. None means unusable, which is why finish_reason matters.

    A truncated answer is the ADR-0002 failure exactly: an answer cut off at the
    budget is unusable JSON, which would otherwise read as "no pairings were
    possible".
    """
    content, finish_reason = complete(prompt, max_tokens=PAIRING_MAX_TOKENS)
    if finish_reason == "length":
        return None
    parsed = json_object(content)
    if parsed is None:
        return None
    return {str(topic): str(key) for topic, key in parsed.items() if isinstance(key, str)}


@neatlogs.span(kind="WORKFLOW")
def run_proof_of_skill_sourcing(
    target_skill: str, topics: Sequence[str], repos: Sequence[str] = DEFAULT_TARGET_REPOS
) -> TaskAssignment:
    candidates = fetch_task_candidates(repos)
    pairings = pair_tasks_to_modules(target_skill, topics, candidates)
    return assign_proof_of_skill_tasks(topics, candidates, pairings)


def _print_assignment(curriculum: Curriculum, assignment: TaskAssignment) -> None:
    print(f"Proof-of-Skill Tasks for: {curriculum.target_skill}\n")
    for position, module in enumerate(curriculum.modules, start=1):
        print(f"{position}. {module.topic}")
        task = module.proof_of_skill_task
        if task is None:
            print("   NO TASK ASSIGNED — no unclaimed candidate was left")
            continue
        chosen = "" if module.topic in assignment.unpaired_topics else " (chosen for this module)"
        print(f"   task: {task.key}  {task.title}{chosen}")
        print(f"         {task.url}")
        if module.topic in assignment.unpaired_topics:
            print("         NOT MATCHED TO THIS MODULE — assigned only to fill the slot")

    for topic in assignment.topics_without_tasks:
        print(f"\nUNASSIGNED MODULE: {topic}")

    if assignment.unpaired_topics:
        unpaired, total = len(assignment.unpaired_topics), len(curriculum.modules)
        note = " — the pairing step gave up entirely" if unpaired == total else ""
        print(f"\n{unpaired} of {total} modules carry an unmatched task{note}.")

    # Printed from the assignment, never from the Curriculum: the Modules above
    # genuinely cannot reach any of this.
    print("\n--- Ground Truth (Judge agent only; never shown to a Student Agent) ---")
    for key, ground_truth in assignment.ground_truths.items():
        print(f"{key} <- PR #{ground_truth.pull_number} {ground_truth.url}")
        print(f"   clone an Attempt at {ground_truth.pre_fix_commit_sha}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        curriculum = run_curriculum(run_analyst("smart contract development"))
        topics = [module.topic for module in curriculum.modules]
        assignment = run_proof_of_skill_sourcing(curriculum.target_skill, topics)
        _print_assignment(with_proof_of_skill_tasks(curriculum, assignment.tasks), assignment)
    finally:
        neatlogs.flush()
        neatlogs.shutdown()


if __name__ == "__main__":
    main()
