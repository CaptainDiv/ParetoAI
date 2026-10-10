# ParetoAI

A multi-agent system that builds a minimal, high-coverage ("Pareto") learning curriculum for a target professional skill, then pressure-tests the curriculum by having simulated learners attempt real-world coding tasks and grading them against ground truth.

## Language

**Target Skill**:
The professional skill the Curriculum is built for. The system is designed to generalize to any skill; the MVP targets exactly one — smart contract development (Solidity/EVM) — chosen over the original cybersecurity framing because its proof-of-skill step requires real, gradable GitHub issues with merged fixes, which smart-contract repos have in abundance.
_Avoid_: role, track, subject

**Pareto Table**:
The Analyst agent's output: a table of skills for the Target Skill, ranked by how often they appear across public sources (roadmaps, cert objectives, O*NET/BLS data, GitHub signals, Stack Overflow survey data), fetched live rather than scraped from job boards or hand-seeded.
_Avoid_: skill list, rankings

**Curriculum**:
The ordered set of Modules the Curriculum agent cuts from the top ~20% of the Pareto Table, in prerequisite order. Produced fresh each Round.
_Avoid_: mentoring path, scheme, table of contents, course

**Module**:
One unit of the Curriculum: a topic, 1-2 best Resources, and one Proof-of-Skill Task.
_Avoid_: lesson, unit, topic (alone)

**Proof-of-Skill Task**:
A real, closed GitHub issue (with a known merged fix) assigned to a Module, used to verify a learner can actually apply what the Module teaches.
_Avoid_: exercise, quiz, test (alone — "test" also means the repo's automated test suite, a different thing)

**Learner Profile**:
A named archetype a Student Agent is configured to simulate, used to check the Curriculum works across experience levels. MVP uses two: Total Beginner and Some IT/Tech Background.
_Avoid_: persona, user type

**Student Agent**:
An agent conditioned on one Learner Profile that attempts a Proof-of-Skill Task as a real coding-agent session (clones the target repo, reads/edits files, runs tests, commits) — not a text-only simulation. Its knowledge of the Target Skill is bounded to what it retrieves from the Curriculum (RAG), not a system-prompt dump of the whole thing. It never sees the Ground Truth.
_Avoid_: learner (the Student Agent simulates a learner; "learner" alone is ambiguous between the real end user and this simulation)

**Attempt**:
One Student Agent's real coding session against one Module's Proof-of-Skill Task, run in its own isolated clone of the target repo so attempts never interfere with each other.
_Avoid_: run, try

**Ground Truth**:
The actual merged PR that closed the Proof-of-Skill Task's GitHub issue. Used only by the Judge agent to grade an Attempt; never exposed to the Student Agent.
_Avoid_: solution, answer key

**Checkpoint**:
Entire's auto-captured record of one Attempt: the commit plus the full coding-agent transcript (prompts, files touched, tool calls, reasoning). Entire's checkpoint capture only works for its supported coding-agent CLIs (currently Claude Code and Gemini CLI), which constrains how Student Agents must be implemented.
_Avoid_: trace (that term is reserved for neatlogs' spans, a different kind of record)

**Judge Agent**:
Grades an Attempt against its Ground Truth, producing Gaps mapped to specific Modules.
_Avoid_: grader, evaluator

**Gap**:
A specific weakness the Judge Agent finds in an Attempt, mapped to the Module responsible (or flagging that no existing Module covers it). Fed back to the Curriculum agent to add or reorder Modules.
_Avoid_: issue (ambiguous with "GitHub issue"), weakness

**Round**:
One full iteration of Curriculum → Student Agent Attempts → Judge Agent → Gaps → Curriculum revision. Stops when every Learner Profile independently hits the pass-rate threshold (70%) — not an average across profiles, since the point of multiple profiles is to catch a path that works for one but not the other — or after 3 Rounds, whichever comes first.
_Avoid_: iteration, loop (alone), cycle

**Readiness Report**:
The honest end-of-loop output: each Learner Profile's final pass rate, and — if the loop hit the 3-Round cap without every profile reaching threshold — the specific unresolved Gaps, surfaced explicitly rather than silently presenting an unfinished Curriculum as job-ready.
_Avoid_: result, summary

**Repo Map**:
Entire's `entire-graph` output (symbol-level caller/callee map of the target repo) computed once per Proof-of-Skill Task and fed into each Attempt's starting context, so a Student Agent reasons from a prebuilt map instead of blind grepping. The other half of Entire's value prop alongside Checkpoints — both are load-bearing in this design, not just checkpoint capture.
_Avoid_: codebase map (generic), context (too broad)

**Deferred Resources**:
Pareto Table entries that didn't make a Module's active content but aren't discarded — linked from the relevant Module as "going deeper" reading for a learner who wants depth beyond job-readiness. Keeps the 80% cut from being an information loss, just a deprioritization.
_Avoid_: appendix, extra reading

## Rules

- **Prerequisite-closure is non-negotiable.** If a low-frequency Pareto Table entry is a hard prerequisite of anything in the Curriculum, the Curriculum agent must include it regardless of its frequency rank. Frequency decides emphasis; prerequisites decide inclusion.
- **Ground Truth is never exposed to a Student Agent**, before or during an Attempt.
- **Every Attempt runs in its own isolated clone** of the target repo at the commit before the Ground Truth fix, so Attempts never interfere with each other.
