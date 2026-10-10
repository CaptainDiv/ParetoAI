# ParetoAI

## Project
**Name:** ParetoAI
**Purpose:** An AI agent team that curates a beginner-friendly "20% Pareto" curriculum for a given professional skill — the minimal slice of material that covers most of the practical value — and then pressure-tests it by having simulated learners attempt real closed GitHub issues, graded against the merged fix.
**Problem it addresses:** Beginners drown in exhaustive syllabi before they've validated they can actually apply anything. ParetoAI identifies the highest-leverage subset of a curriculum and verifies comprehension through hands-on coding-issue exercises instead of passive reading or quizzes.

## Team
- Captin_Div

## Stack
- **Language/runtime:** Python 3.11, venv-managed
- **LLM provider:** Groq (OpenAI-compatible API, `https://api.groq.com/openai/v1`), accessed via the `openai` Python client
  - Model in use: `openai/gpt-oss-20b` (chosen after `llama-3.3-70b-versatile` returned 404 — not available on this key; verified available models via `client.models.list()`)
- **Tracing:** neatlogs SDK (`neatlogs[anthropic]` extra installed, though provider is now Groq/OpenAI-compatible), project `209f4402-89cd-450c-9b1d-267a406e4ff6`, workflow name `pareto-ai` (renamed from `pareto-ai-demo` once the Groq+Neatlogs client was centralized in `pareto_ai/llm.py` for every agent to share — one workflow for the whole pipeline, not one per script)
- **GitHub API:** via the authenticated `gh` CLI (`gh api graphql`), used by the Proof-of-Skill sourcing to find merged PRs with formally linked closing issues. Target repo: `OpenZeppelin/openzeppelin-contracts` (MIT, Hardhat suite, squash-merge-only — see the #4 build log entry for why each matters)
- **Entire:** not yet integrated — required by hackathon rules, pending (tracked as ticket #6)
- **cfo.ai:** not yet integrated — required by hackathon rules, pending (tracked as ticket #9); an untracked `docs/business-plan.md` exists but has not been confirmed as cfo.ai output
- **Decision records:** `docs/adr/0001` (Gemini CLI for Student Agents), `0002` (refuse LLM degradation that would be invisible), `0003` (a Source is a demand measure, not documentation)

## Build log

### 2026-10-10 — Initial setup
- Scaffolded the project in an empty working directory (previously only an empty venv existed).
- Installed `neatlogs[anthropic]` and `python-dotenv`; initialized Neatlogs SDK (`neatlogs.init(workflow_name="pareto-ai-demo")`) and wrapped the LLM client with `neatlogs.wrap()`.
- Built `app.py` as a minimal single-workflow demo (`@neatlogs.span(kind="WORKFLOW")`) to confirm the tracing pipeline end-to-end before building out the real agent team.
- First attempt used Anthropic's SDK directly, but the available API key has no paid plan, so the agent can't call Claude.
- **Fix:** swapped to Groq's OpenAI-compatible endpoint using the `openai` client (`pip install openai`), pointed `base_url` at `https://api.groq.com/openai/v1`, kept `neatlogs.wrap()` around the same client (it auto-detects provider type).
- **Failure → fix:** first Groq model choice (`llama-3.3-70b-versatile`) returned `404 model_not_found` for this key. Queried `client.models.list()` directly, found the key has access to `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, Whisper/Orpheus audio models, and guard models — switched the demo to `openai/gpt-oss-20b`.
- Ran `.\venv\Scripts\python.exe app.py`: returned a correct answer ("The capital of France is **Paris**.") and the Neatlogs SDK logged a clean shutdown, confirming the trace export path works end-to-end.
- **Outstanding partner integrations:** Entire (repo context/checkpoints for coding agents) and cfo.ai (pricing/cost/revenue/runway plan) are both required by the hackathon rules and have not been started yet.
- Since this entry: the design was grilled into a full spec (GitHub issue #1) and split into 9 tracer-bullet tickets (#2–#10) with native blocking edges; `CONTEXT.md` (domain glossary) and `docs/adr/0001-gemini-cli-for-student-agents.md` were added — Student Agents will run as Gemini CLI sessions, not Claude Code or a custom loop, because Entire's checkpoint capture currently only supports those two CLIs.

### 2026-10-10 — Analyst agent (ticket #2) built and smoke-tested
- Built `pareto_ai/analyst.py`: given a Target Skill, fetches live public sources, asks the Groq LLM to extract skills per source, and ranks them into a Pareto Table by how many distinct sources cite each one. TDD'd the pure ranking core (`aggregate_pareto_table`, 4 tests in `tests/test_analyst.py`) per the spec's testing decision that live fetch/LLM calls are smoke-tested only, not unit-tested.
- **Prefactor:** extracted the Groq+Neatlogs client setup out of `app.py` into a shared `pareto_ai/llm.py`, so Curriculum/Judge/Round-orchestrator (tickets #3, #7, #8) reuse one client instead of each re-wiring it. Consolidated tracing under one workflow name, `pareto-ai`, instead of the demo's `pareto-ai-demo` — one workflow for the whole pipeline.
- **Failure → fix:** first live run returned an empty Pareto Table. Cause: `max_tokens=512` truncated the LLM's JSON skill-list mid-string, so the parser correctly found no closing `]` and returned nothing. Fixed by bounding the ask ("5-10 skills") and raising the budget to 1024.
- **Failure → fix:** live run then crashed with `UnicodeEncodeError` — Windows' default console codec (cp1252) can't print some punctuation the LLM returns. Fixed by reconfiguring stdout to UTF-8 in `main()`.
- **Failure → fix:** `roadmap.sh` (one of the three initial sources) is a JS-rendered SPA — a plain `requests.get` only returns the empty loading shell, no real content. Swapped it for `github.com/topics/solidity` (server-rendered, real "GitHub signals" per the ticket's source list).
- Ran `python -m pareto_ai.analyst` live for "smart contract development": produced a real 28-entry ranked Pareto Table citing all three sources (Ethereum.org docs, Solidity docs, GitHub solidity topic). All entries currently have frequency 1 — the three sources didn't converge on identical skill names (e.g. "OpenZeppelin library" vs "OpenZeppelin secure libraries" are counted separately); noted as a known limitation below rather than fixed now. _(Superseded: filed as ticket #11, partially fixed in `9608682`, and then reframed entirely by ADR-0003 — the root cause was not name matching but that documentation pages cannot corroborate each other. See the two entries below.)_
- **Known/deliberate gap vs. the spec's source list** (roadmaps, cert syllabi, O*NET/BLS data, GitHub signals, Stack Overflow survey data): the MVP only live-fetches two canonical-docs sources plus one GitHub page. O*NET/BLS requires registration; Stack Overflow's survey data isn't available as a plain fetchable page; roadmap.sh (the roadmap source) turned out to be unfetchable without a JS-executing client. Left as an MVP descoping, not revisited in this ticket.
- Ran `/code-review` (Standards + Spec axes) before committing: fixed test-fixture duplication and a `.__class__` chain in `tests/test_analyst.py`, and dropped an unused `workflow_name` parameter on `init_tracing()` flagged as speculative generality. Full test suite (4/4) and both smoke tests (`app.py`, `pareto_ai.analyst`) pass after fixes.

### 2026-10-10 — Curriculum agent (ticket #3) built; ADR-0002 on failing loudly
- Built `pareto_ai/curriculum.py` (commit `0803a1c`, 44 tests). The LLM is asked only for judgement — hard prerequisite edges, the 1-2 best Resources per topic, which Module a cut entry extends — while the rules that must hold regardless of what it says are pure, TDD'd functions: `select_module_topics` (top ~20% cut, prerequisite closure, ordering) and `assemble_curriculum` (Modules, Resources, Deferred Resources). Resources are planned *after* topic selection, because prerequisite closure is what decides the final topic set.
- **Prerequisite-closure verified live, not just in fixtures:** the Analyst's table ranked "Solidity fundamentals" well outside the top 20%, and closure promoted it to Module 1 — the behaviour `CONTEXT.md` calls non-negotiable.
- **Failure → fix:** `openai/gpt-oss-20b` charges *reasoning* tokens against `max_tokens`. Planning calls spent the whole 2048-token budget thinking and returned `''` (confirmed: `finish_reason=length`, 2046 of 2048 tokens were reasoning). Read as "no prerequisites", that silently switched off prerequisite closure and produced a confident-looking Curriculum whose "top 20%" cut was really *alphabetical*. Fixed with `reasoning_effort="low"` plus a larger budget, and by **refusing** a truncated or unparseable response instead of degrading quietly.
- That failure generalised into **`docs/adr/0003`'s predecessor, `docs/adr/0002`**: degrade only where the degradation shows up in the output; refuse where it would be invisible. Recorded because `analyst.py` degrades silently and `curriculum.py` does not, and the divergence would otherwise read as an oversight.
- **Failure → fix:** the model skips topics on longer lists, leaving a Module with only a bare source name to read. Missing topics are now re-asked for on their own; three consecutive live runs then produced zero link-less Resources.
- Where a prerequisite cycle forces an impossible ordering, the affected Module reports `unmet_prerequisites` rather than reporting none — found by `/code-review`'s Spec axis, which pointed out the original code made an untaught dependency look like *no* dependency.
- Ran `/code-review` (Standards + Spec) before committing; applied 8 findings across the two axes.

### 2026-10-10 — What a Source is, reconsidered (ADR-0003, ticket #11)
- Running the Analyst live showed the ranking measured the wrong thing. Canonical documentation *partitions* a subject rather than surveying it, so **45 of 45 entries tied at frequency 1**, and a skill's score was really decided by which pages a human picked — including three security-ish pages made security topics score 2. The "demand signal" was a readback of the source list.
- Filed as ticket **#11**. Partial fix committed (`9608682`): `canonical_skill` folds case/unicode-dash/punctuation/parenthetical variants, and `merge_skill_aliases` asks the model which differently-worded names mean one skill. The same nudge appended to each page-extraction prompt was reliably ignored behind 5000 chars of page text, so it got its own call — which also removed a source-order dependence.
- **Failure → fix:** six sources plus a Curriculum run exceed Groq's free-tier **8000 tokens/minute**. `pareto_ai/llm.py` now has a single `complete()` that waits out a 429 using the response's own `retry-after`, and carries the reasoning-effort lesson the Analyst had never had (its extraction was burning 427 reasoning tokens against a 1024 budget at default effort).
- The deeper problem was definitional, so it went through `/domain-modeling` rather than more code: **`Source`** was load-bearing in the glossary while never being defined, and "how often they appear" was ambiguous between three different measures. `CONTEXT.md` now defines `Source` (a published measure of market demand) and **`Demand Weight`** (what a Pareto Table ranks on), with `frequency` on its Avoid list. Recorded as **`docs/adr/0003`** (`a3dc501`) so the next reader doesn't "fix" this by adding the Solidity docs back.
- **Research, then a correction to our own ADR** (`b390b41`): `/research` checked the ADR's claims against primary sources and refuted two of them. O*NET *does* have a smart-contract occupation — **15-1299.07 Blockchain Engineers**, updated 2026, publishing 18 tasks and 41 work activities with numeric 0–100 importance ratings, CC BY 4.0. And **no** smart-contract certifier publishes exam weightings, so the ADR's central bet did not pay off. The decision survived; the evidence changed. Findings, with measured fetch offsets, in `docs/research/demand-sources-for-smart-contract-development.md`.
- Two traps that research caught and a length check would not: **W3CB** returns 5,627 bytes of valid-looking HTML yielding **52 characters** of text (a pure SPA shell), and **BLS** returns 403 on `robots.txt` itself with an explicit prohibition — the same category as the job-board scraping the spec already rules out.

### 2026-10-10 — Proof-of-Skill Task sourcing (ticket #4)
- Built `pareto_ai/proof_of_skill.py` (commit `b31cdb8`, 17 new tests, 83 passing overall). Each Module gets one real closed GitHub issue; the merged PR that closed it becomes its Ground Truth. Verified live end-to-end: a 10-module Curriculum with all 10 carrying real closed OpenZeppelin issues, 9 matched to their topic and 1 reported as unmatched.
- **Ground Truth separation is structural at the Module**, as `CONTEXT.md` requires: `ProofOfSkillTask` has no field describing its fix, and a test pins its field names so adding one fails the suite. Scoped honestly in the docstring — a `TaskAssignment` holds both halves because something must hand the Judge its half, so an Attempt runner must be passed `assignment.tasks`, never the assignment.
- **Target repo decided by measurement**, as ticket #4 asked (it left the choice to implementation time). Counting merged PRs that *formally* link a closing issue: OpenZeppelin 483, Uniswap/v3-core 73, forge-std 56, solady 26, solmate 6, seaport 3. The pool is only reachable by search — OpenZeppelin's 20 most recently updated PRs link no issues at all, so ordering by recency finds nothing.
- **Four defects the live API exposed, none visible from fixtures.** `linked:issue` does not filter on issue state, so a still-open issue (PR #5687 → issue #5666) was becoming a task. Two PRs closing one issue produced two candidates, letting two Modules be proven by the same issue *and* silently overwriting one's clone point — an Attempt would have started after part of its own fix. A rebase-merged multi-commit PR leaves its merge commit's parent as an earlier commit of the same fix, so candidates from rebase-permitting repos are refused (OpenZeppelin is squash-only, which is why its parents can be trusted). And `subprocess(text=True)` decodes with the platform locale, dying on the first emoji in an issue body — pinned to UTF-8.
- `/code-review` found a hard ADR-0002 breach of our own rule: a failed pairing fell back to search order and printed *identically* to a good one. Now every fallback is named in `unpaired_topics` and printed as `NOT MATCHED`, which is what made the 1-of-10 case above visible instead of silent.
- **Infrastructure note:** a transient DNS outage (`getaddrinfo failed` for both Groq and neatlogs) killed one verification run mid-way. Not a code fault; re-ran clean.

## Demo
- URL: not yet available
- Instructions: not yet available
- Reproducible today (requires `GROQ_API_KEY`, `NEATLOGS_API_KEY` in `.env` and an authenticated `gh`):
  - `python -m pareto_ai.analyst` — live Pareto Table, with per-source contribution and spread reported
  - `python -m pareto_ai.curriculum` — Analyst → ordered Curriculum of Modules
  - `python -m pareto_ai.proof_of_skill` — the above, plus a real closed GitHub issue per Module and its Ground Truth printed separately
  - `python -m pytest` — 83 tests
  - Note: a full run makes ~11 Groq calls and will pause on free-tier rate limits.

## Known limitations and next steps

**Built and closed:** Analyst (#2), Curriculum (#3), Proof-of-Skill Task sourcing (#4). 83 tests passing.
**Not started:** RAG retriever (#5, unblocked and next), Student Agent (#6), Judge (#7), Round orchestrator (#8), rendering (#10).

- **The Pareto Table's ranking is not yet trustworthy — the biggest open gap.** Per ADR-0003 a Source must be a measure of market demand, and the Analyst's configured sources are documentation, so they are *not* Sources under our own definition. They are kept, explicitly relabelled as placeholders, only so the pipeline runs end to end; every Demand Weight computed from them is meaningless and should not be demoed as a ranking. Ticket **#11** holds the replacement, and `docs/research/` now has a fetch-verified source list to build it from.
- `ParetoEntry.frequency` still computes a count of citing sources, so it is misnamed under ADR-0003. Renaming it is part of #11, along with the change that makes the name true.
- Name merging is partial: `canonical_skill` handles spelling variants and `merge_skill_aliases` handles differently-worded ones, but a live run still produced only 7 of 40 entries above the floor — because the sources do not overlap, which is the #11 problem above, not a merging problem.
- **Proof-of-Skill Task quality:** the candidate pool includes issues whose fix is not code (one run drew *"Release `v5.5.0` has wrong `dev` tag on `npm`"*). A Student Agent cannot meaningfully attempt that and the Judge cannot grade it by running the test suite. The fix is one extra GraphQL field — filter to PRs touching contract or test files — and it matters before #6.
- **O*NET's weights are coarse where the Curriculum needs them fine:** its tasks are occupation-level ("Test the security and performance of blockchain infrastructures"), not Solidity-level. The only real numeric demand signal does not decompose to Module granularity.
- `PAGE_TEXT_CHARS = 5000` is now a *content* decision rather than a token budget: it is the sole reason the EEA EthTrust checklist is unusable (its first requirement sits at offset ~5,868). Per-Source caps would serve better.
- **No typechecker is configured** in the venv (no mypy/pyright/ruff), so the engineering workflow's typechecking step has nothing to run; compile and import-cycle checks are used instead.
- Free-tier Groq is **8000 tokens/minute**, which a full pipeline run exceeds. Handled by waiting out 429s, but it makes end-to-end runs slow — worth knowing before recording a demo.
- **Entire integration not started** (ticket #6) — needed for Checkpoints and the `entire-graph` Repo Map, and it is one of the three mandatory tools.
- **cfo.ai integration not started** (ticket #9, no blockers). `docs/business-plan.md` exists untracked in the working tree and may already be the deliverable, but it has not been reviewed or confirmed as cfo.ai output.
- No demo video and no #neatHack build-in-public posts yet.

## Submission
Requirements confirmed from https://neatlogs.com/hackathon and https://luma.com/s5blr882 as of 2026-10-10:
- **Mandatory tools:** neatlogs, Entire, cfo.ai — all three must be used and evidenced. **Only neatlogs is wired up**; Entire (#6) and cfo.ai (#9) are both still outstanding, which is the largest submission risk given they carry 25% of the score jointly with neatlogs.
- **Evidence the submission must support** (per the page, re-checked 2026-10-10): claims about agent improvement, neatlogs usage, Entire usage, the cfo.ai business plan, **failure recovery**, public building activity, and code written during the hackathon. The build log above is the failure-recovery record; neatlogs traces cover the Analyst, Curriculum and Proof-of-Skill agents, each under its own span.
- **Judging weights:** agent itself 30%, use of neatlogs/Entire/cfo.ai 25%, demo video clarity 20%, building in public 15%, usefulness/originality 10%.
- **Build period:** October 10–12, 2026. **Submission window:** October 12, 7:00–11:55 PM IST — re-checked 2026-10-10, and the page now words this as a "planned close" with the **final cutoff announced on Discord**, so the Discord channel is the authority on the deadline, not this figure. (An earlier note here gave **Results: October 16**; that is not stated on the hackathon page and is recorded as unverified rather than removed.)
- **Submission format:** post on X quoting the official neatlogs post, including team/individual name, project title, public repo link (with hackathon-dated commits), and a demo video. Supporting evidence must include neatlogs traces, Entire checkpoints/outputs, and the cfo.ai business plan, plus public updates tagged #neatHack.
- **Outstanding:** no X/Twitter post made; no demo video recorded; no #neatHack build-in-public posts yet. Registration status for the hackathon itself (via Luma) not confirmed with the builder. (Repo is public: https://github.com/CaptainDiv/ParetoAI)
