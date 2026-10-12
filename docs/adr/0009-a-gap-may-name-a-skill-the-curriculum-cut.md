# A Gap may name any Pareto Table entry, so a Round can add a Module

`CONTEXT.md` said Gaps are fed back to the Curriculum agent "to add or reorder Modules", but nothing could add one (issue #8). The Judge kept a Gap's `module_topic` only if it was already a Module of the Curriculum the Attempt ran against, and anything else became `None`. The Curriculum agent re-seeds Gap topics into the cut, but a topic that is already a Module changes nothing, so a Round's Gaps could neither change the next Curriculum nor reach a skill the 20% cut had left out. They could only pile up as `uncovered_gaps`.

**Decision: the Judge may map a Gap to any Pareto Table entry.** The rubric prompt lists the Curriculum's Modules and, separately, the entries it cut, and asks for the one skill whose teaching would have prevented the Gap. `parse_gaps` keeps a topic that is in either list and reads anything else as `None`, which now means "no entry in the table covers it" and is what a Readiness Report reports as unresolvable. `select_module_topics` already seeded any Pareto Table entry a Gap named, so the Curriculum side needed only a bound.

**The bound: a revision adds at most two entries (`MAX_TOPICS_ADDED_BY_GAPS`), the ones Gaps blame most often, ties in table order.** Every added Module is another Attempt per Learner Profile in the next Round (minutes each, against a shared gateway budget), so a rule that added a Module per Gap would let one weak Attempt multiply the next Round's cost. Prerequisite closure can still pull in more, because it is non-negotiable.

## Considered Options

- **Narrow the glossary to "reorder"** (the issue's other option). Rejected: a Curriculum's order is already fixed by prerequisites and Demand Weight, so there is nothing for a Gap to reorder, and "Gaps from one Round visibly change the next Round's Curriculum" could never hold.
- **Let the Curriculum agent invent Modules from a Gap's description.** Rejected: a Module is always a Pareto Table entry, which is what keeps the Curriculum a cut of measured demand rather than the model's opinion.
- **No cap.** Rejected for the cost reason above.

## Consequences

- A Gap now points at a skill rather than at a Module, and the skill may not be taught yet. `Curriculum.uncovered_gaps` still lists the Gaps whose skill the Curriculum does not teach; after a revision that took the skill on, the Gap no longer appears there.
- The Judge's mapping is the model's call, and a Round can add a skill because the model blamed it. Which skills get added can vary from run to run, as the Analyst's classifier already does (ADR-0007).
- Only the previous Round's Gaps feed a revision, and a revision keeps every Module the last Curriculum taught (`keep`). A Module a Gap added is no longer blamed once it is taught, so without `keep` the next revision would drop it and the Gap would come back. A Curriculum therefore only grows across Rounds: at most two added per revision, so at most four over three Rounds.
- A Gap that blames a Module the Curriculum already teaches changes nothing in the next Curriculum, and a topic already in through prerequisite closure uses none of the allowance. Nothing reorders Modules: their order is fixed by prerequisites and Demand Weight.
