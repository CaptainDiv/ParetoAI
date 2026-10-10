# The Curriculum agent refuses an unusable LLM response; the Analyst degrades silently

`analyst.py` reads a malformed or empty LLM response as "this Source yielded no skills" (`_parse_skill_list` returns `[]`), and the Curriculum agent deliberately does **not** follow that precedent: `curriculum.py` raises `CurriculumPlanningError`. The two diverge because the *consequence* of degrading differs. A Source yielding nothing is visible in the Pareto Table — the entry count drops and that Source stops appearing in any entry's `sources` — so the table stays honest about what was actually found. An empty prerequisite plan is invisible: it reads as "nothing has hard prerequisites", which silently switches off the prerequisite-closure rule `CONTEXT.md` calls non-negotiable, while still printing a confident, well-formed Curriculum. That is not hypothetical — `openai/gpt-oss-20b` charges reasoning tokens against `max_tokens`, so planning calls at a 2048-token budget spent all of it thinking and returned `''`; the pipeline emitted a plausible-looking Curriculum whose "top ~20% by frequency" cut was really alphabetical and whose Modules each claimed to have no prerequisites. The rule for the agents still to be built: **degrade only where the degradation shows up in the output; refuse where it would be invisible.**

## Considered Options

- **Make the Analyst refuse too, for consistency** — rejected: one unreachable or uncooperative Source shouldn't abort a Pareto Table aggregated from several, and its absence is legible in the result, so nothing is hidden by carrying on.
- **Make the Curriculum agent degrade, for consistency with the Analyst** — rejected: that *is* the bug described above. The failure mode is a Curriculum that looks finished and is silently missing its foundation.
- **Log a warning and continue** — rejected: nothing reads stderr in a demo pipeline whose whole output is a printed Curriculum, so this is silent degradation with extra steps.

## Consequences

- Every planning call gets one retry before a refusal stands, so a transient malformed response doesn't abort a Round. The error only surfaces when the model fails twice.
- The Judge agent inherits the refusing side of this rule: a rubric response read as "no Gaps" would inflate a pass rate, and the pass rate is the project's central claim. Applying the Analyst's leniency there would be the most damaging possible place to do it.
- Deciding which side a new agent falls on requires asking what its silence would look like downstream — the question is not "is this call important?" but "would a reader of the output be able to tell this went wrong?"
