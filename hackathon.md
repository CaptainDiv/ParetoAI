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
- **Tracing:** neatlogs SDK (`neatlogs[anthropic]` extra installed, though provider is now Groq/OpenAI-compatible), project `209f4402-89cd-450c-9b1d-267a406e4ff6`, workflow name `pareto-ai-demo`
- **Entire:** not yet integrated — required by hackathon rules, pending
- **cfo.ai:** not yet integrated — required by hackathon rules, pending

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

## Demo
- URL: not yet available
- Instructions: not yet available

## Known limitations and next steps
- No actual curriculum-curation or issue-testing agent logic yet — current code is only a tracing smoke test, not the product.
- Entire integration not started (needed for repo-context/checkpoints on the coding-issue-testing agent).
- cfo.ai integration not started (needed for the business plan: pricing, costs, revenue, runway).
- No multi-agent structure yet (curriculum-curator agent, issue-tester agent, coordination between them).
- No demo video or public build-in-public posts yet.

## Submission
Requirements confirmed from https://neatlogs.com/hackathon and https://luma.com/s5blr882 as of 2026-10-10:
- **Mandatory tools:** neatlogs, Entire, cfo.ai — all three must be used and evidenced; only neatlogs is wired up so far.
- **Judging weights:** agent itself 30%, use of neatlogs/Entire/cfo.ai 25%, demo video clarity 20%, building in public 15%, usefulness/originality 10%.
- **Build period:** October 10–12, 2026. **Submission window:** October 12, 7:00–11:55 PM IST. **Results:** October 16.
- **Submission format:** post on X quoting the official neatlogs post, including team/individual name, project title, public repo link (with hackathon-dated commits), and a demo video. Supporting evidence must include neatlogs traces, Entire checkpoints/outputs, and the cfo.ai business plan, plus public updates tagged #neatHack.
- **Outstanding:** repo is not yet public; no X/Twitter post made; no demo video recorded; no #neatHack build-in-public posts yet. Registration status for the hackathon itself (via Luma) not confirmed with the builder.
