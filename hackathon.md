# ParetoAI

## Project
**Name:** ParetoAI
**Purpose:** An AI agent team that curates a beginner-friendly "20% Pareto" curriculum for a given subject — the minimal slice of material that covers most of the practical value — and then tests learners on it using real coding issues.
**Problem it addresses:** Beginners drown in exhaustive syllabi before they've validated they can actually apply anything. ParetoAI identifies the highest-leverage subset of a curriculum and verifies comprehension through hands-on coding-issue exercises instead of passive reading or quizzes.

## Team
- Captin_Div

## Stack
- **Language/runtime:** Python 3.11, venv-managed
- **LLM provider:** Groq (OpenAI-compatible API, `https://api.groq.com/openai/v1`), accessed via the `openai` Python client
  - Model in use: `openai/gpt-oss-20b` (chosen after `llama-3.3-70b-versatile` returned 404 — not available on this key; verified available models via `client.models.list()`)
- **Tracing:** neatlogs SDK (`neatlogs[anthropic]` extra installed, though provider is now Groq/OpenAI-compatible), project `209f4402-89cd-450c-9b1d-267a406e4ff6`, workflow name `pareto-ai` (renamed from `pareto-ai-demo` once the Groq+Neatlogs client was centralized in `pareto_ai/llm.py` for every agent to share — one workflow for the whole pipeline, not one per script)
- **Entire:** not yet integrated — required by hackathon rules, pending (tracked as ticket #6)
- **cfo.ai:** not yet integrated — required by hackathon rules, pending (tracked as ticket #9)

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
- Ran `python -m pareto_ai.analyst` live for "smart contract development": produced a real 28-entry ranked Pareto Table citing all three sources (Ethereum.org docs, Solidity docs, GitHub solidity topic). All entries currently have frequency 1 — the three sources didn't converge on identical skill names (e.g. "OpenZeppelin library" vs "OpenZeppelin secure libraries" are counted separately); noted as a known limitation below rather than fixed now.
- **Known/deliberate gap vs. the spec's source list** (roadmaps, cert syllabi, O*NET/BLS data, GitHub signals, Stack Overflow survey data): the MVP only live-fetches two canonical-docs sources plus one GitHub page. O*NET/BLS requires registration; Stack Overflow's survey data isn't available as a plain fetchable page; roadmap.sh (the roadmap source) turned out to be unfetchable without a JS-executing client. Left as an MVP descoping, not revisited in this ticket.
- Ran `/code-review` (Standards + Spec axes) before committing: fixed test-fixture duplication and a `.__class__` chain in `tests/test_analyst.py`, and dropped an unused `workflow_name` parameter on `init_tracing()` flagged as speculative generality. Full test suite (4/4) and both smoke tests (`app.py`, `pareto_ai.analyst`) pass after fixes.

## Demo
- URL: not yet available
- Instructions: not yet available

## Known limitations and next steps
- Only the Analyst agent (ticket #2) is built; Curriculum, Student Agent/RAG, Judge, and the Round orchestrator (#3–#8) are not yet implemented.
- The Pareto Table's skill aggregation is exact-string matching, so near-duplicate skill names from different sources (e.g. "OpenZeppelin library" vs. "OpenZeppelin secure libraries") don't merge — likely a Curriculum-agent (#3) concern, not fixed yet.
- Only 3 live sources are fetched (2 docs sites + 1 GitHub page), not the full source diversity named in the spec (cert syllabi, O*NET/BLS, Stack Overflow survey data) — see the 2026-10-10 build log entry for why.
- Entire integration not started (needed for repo-context/checkpoints on the coding-issue-testing agent).
- cfo.ai integration not started (needed for the business plan: pricing, costs, revenue, runway).
- No demo video or public build-in-public posts yet.

## Submission
Requirements confirmed from https://neatlogs.com/hackathon and https://luma.com/s5blr882 as of 2026-10-10:
- **Mandatory tools:** neatlogs, Entire, cfo.ai — all three must be used and evidenced; only neatlogs is wired up so far.
- **Judging weights:** agent itself 30%, use of neatlogs/Entire/cfo.ai 25%, demo video clarity 20%, building in public 15%, usefulness/originality 10%.
- **Build period:** October 10–12, 2026. **Submission window:** October 12, 7:00–11:55 PM IST. **Results:** October 16.
- **Submission format:** post on X quoting the official neatlogs post, including team/individual name, project title, public repo link (with hackathon-dated commits), and a demo video. Supporting evidence must include neatlogs traces, Entire checkpoints/outputs, and the cfo.ai business plan, plus public updates tagged #neatHack.
- **Outstanding:** no X/Twitter post made; no demo video recorded; no #neatHack build-in-public posts yet. Registration status for the hackathon itself (via Luma) not confirmed with the builder. (Repo is public: https://github.com/CaptainDiv/ParetoAI)
