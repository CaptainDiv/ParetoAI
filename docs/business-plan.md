# ParetoAI business plan (ticket #9)

**Status: built in cfo.ai on 2026-10-12; see Results. Link: https://app.cfo.ai/s/0tswRgOA4z8tEgYk** The model itself has to be built *in* cfo.ai — the ticket's
acceptance criteria call for a cfo.ai artifact, not a hand-written projection. This file holds the
brief that goes into the cfo.ai session, so that work is ready to paste the moment access opens.
Results and the shared Page link get filled in below once the session runs.

## Blocker (resolved: access opened on 2026-10-12 and the plan was built)

cfo.ai gates the app behind a plan selection (Startup $375/mo, $4,500/yr, after a 14-day free
trial). The trial still requires a card authorisation hold, which declined with insufficient funds
on 2026-10-10. See "Access options" at the bottom.

## Model parameters (agreed)

| Parameter | Value | Why |
| --- | --- | --- |
| Horizon | 24 months, monthly, from Jan 2027 | Long enough to show the B2B ramp overtaking free-tier subsidy cost |
| Starting cash | $0 | Deliberate: makes the model *derive* the raise needed rather than assume cash exists |
| Headcount | Solo founder, no salary | Reflects the actual team (Captin_Div); keeps burn attributable to infra + inference |
| Pricing | Proposed by Ari, not fixed here | Spec #1 explicitly delegates pricing to the cfo.ai session |

## Revenue model

Two tiers, per spec #1:

- **Free individual tier** — learners generate curricula at no charge. Top-of-funnel, and the source
  of the validation data that makes the product credible. Modelled as a real cost centre, not a
  rounding error, because every free curriculum consumes the full generation cost below.
- **Paid B2B tier** — companies and bootcamps generate a vetted curriculum plus a readiness bar for
  a role they're hiring or training for, with the measured pass-rate evidence behind it. Recurring
  subscription revenue.

## Cost engine

The unit cost is the central risk, so the model must build it from drivers rather than hardcode a
number. Derived from the pipeline as built (`CONTEXT.md`, `hackathon.md`, checked 2026-10-12): one
generated curriculum runs up to 3 Rounds. **Measured** means seen in a real run (usually one run);
**assumed** means no measurement yet, so the model should expose it as an editable variable.

| Driver | Value | Status |
| --- | --- | --- |
| Analyst + Curriculum + task-pairing LLM calls, once per curriculum | about 12 calls, 34 s live | Measured. Model `claude-haiku`; the hackathon gateway is free to the team, so a real price per 1M tokens is a model input |
| Curriculum revision, per Round after the first | about 3 planning calls | Assumed from the code (prerequisites, Resources, deferrals), not measured |
| Modules per Round | 5 in the one live Curriculum; Round 2 adds up to 2, Round 3 up to 2 more | 5 measured, the rest is the cap in ADR-0009 |
| Student Agent sessions per Round | Modules x 2 Learner Profiles: 10, then up to 14, then up to 18 | Derived. Worst case **42 per curriculum** |
| Tokens per Student Agent session | about 95k (one Attempt) | Measured once, n=1. A real input, not an average |
| Judge rubric calls | one per Attempt | Measured by construction; diffs are capped at 3,000 characters each |
| Compute wall-clock per Attempt | 8-10 min (copy 40-70 s, OpenCode up to 300 s, Judge about 2 min) | Measured on one pinned task |
| One-off warm-base build per new Proof-of-Skill Task | about 9 min (npm ci, compile, Repo Map) | Measured once. Paid once per task, then reused across Rounds and profiles |
| Wall-clock for a whole curriculum | about 2 h if it converges in Round 1; about 6-8 h over 3 Rounds | **Estimate** from the above; the loop has not been run live |

Worst case (all 3 Rounds): **up to 42 coding-agent sessions plus up to about 7 warm-base builds per
curriculum.** (Not the earlier "48 sessions on 8 Modules": the Curriculum is 5 Modules and grows only
by the Gaps' cap.)

The model should express per-curriculum cost as editable variables: price per 1M input and output
tokens, tokens per session, compute cost per minute, minutes per Attempt, and warm-base minutes per
task. **Pricing inputs are deliberately left blank here**: this project has only ever paid the
hackathon gateway's zero price, so any dollar figure written down now would be invented.

## Questions the model must answer

1. Monthly cash balance and runway across the 24 months.
2. The raise needed to reach breakeven, and the month breakeven happens.
3. Gross margin on the B2B tier once the free-tier subsidy is charged against it.
4. The free-tier cap (curricula per user per month) that keeps the subsidy sustainable.

## Access options (unblocking this ticket)

1. **Ask the hackathon organisers for participant access.** cfo.ai is a *mandatory* neatHack partner
   tool; requiring every participant to put a card against a $4,500/yr plan is unlikely to be the
   intended path. Highest value, zero cost — ask in the hackathon community or via Luma.
2. **Fund the card for the authorisation hold.** A "$0 today" trial still authorises a small amount
   (typically $0–$5) to verify the card.
3. **Use a different card** — including a virtual card.
4. **cfo.ai's "Talk to a human first"** contact route, mentioning hackathon participation.

## Results

Built in cfo.ai by Ari on 2026-10-12 from the prompt below. **Shared plan: https://app.cfo.ai/s/0tswRgOA4z8tEgYk**
(the builder confirmed on 2026-10-12 that the link opens). The model has an Assumptions page, a Cost engine, Monthly operations, a free-cap
simulator and Downside and Upside scenarios; every input is editable.

**Everything below is Ari's output from the stated assumptions, not a forecast and not validated.**
No customer, price or sales figure here is measured. The only measured inputs are the pipeline
drivers in the brief.

| Question | Ari's answer (base case) |
| --- | --- |
| Raise needed | **$2,125 minimum** (cash dips to $0 in Feb 2027, so no cushion). $2,885 if every free user uses the cap; $5,125 with three months of fixed costs in hand |
| Breakeven | Monthly cash breakeven **March 2027**; cumulative startup losses recovered **May 2027**. Month-end cash reaches about $274,725 by Dec 2028 (with the raise) |
| B2B gross margin | **91.3%** direct; **79.0%** after all free-tier generation cost (75.4% in 2027, 79.8% in 2028); 72.0% in Dec 2028 if every free user uses the cap |
| Free-tier cap | **1 curriculum per verified user per month**, plus an aggregate monthly generation budget (30% of subscription revenue, less paid delivery costs) and a waitlist. Sustainable at the 70% margin target from May 2027 at expected use, and from Nov 2027 even at full use; before that it is a funded launch experiment |
| Downside stress | $600,638 to cover 24 months, breakeven not reached (slower sales, every curriculum reaches Round 3, full free use, coding agents at $3/$15 per million tokens) |

**Unit cost (Ari):** a curriculum converging in Round 1 costs about **$2.41** (10 coding attempts);
the base-case mix about **$4.44** (19.2 expected attempts); one that reaches all three Rounds about
**$9.48** (42 attempts).

**Proposed pricing (Ari's hypothesis, unvalidated):** companies $899/month for up to 8 curricula
(expected use 6); bootcamps $1,799/month for up to 16 (expected use 12). Earlier test prices of
$499/$999 could not support the 70% margin target with the proposed free usage.

**Inputs to treat as assumptions, not facts:**
- Sales: 0.5 company and 0.15 bootcamp new accounts in Jan 2027, growing 8% a month, 2% monthly churn;
  200 free users in Jan 2027 growing 10% a month at 60% cap use. No customer exists today.
- Round odds: 40% of curricula reach Round 2 and half of those reach Round 3. Ari's choice, not measured;
  the loop has not run live.
- Token prices: Claude Haiku 4.5 at $1 / $5 per million input / output tokens, from Anthropic's pricing
  page as cited by Ari (not re-checked here). The coding agents and judge are priced as Haiku too. The
  Student Agent actually runs through the hackathon gateway at no cost to us, so this is a price a
  real deployment would pay.
- Compute: $0.004 per container-minute, a generic placeholder. Token input/output split, retries
  (15% allowance) and judge token sizes are Ari's estimates.
- Excluded by Ari: founder living costs, pre-launch development, daily payment timing, taxes.
- The 95k-token session figure is one observation.

## Prompt for Ari (paste as one message)

Build a 24-month monthly business plan model for ParetoAI, starting Jan 2027, with starting cash $0 so the model derives the raise needed, and a solo founder taking no salary. ParetoAI is an AI agent system that builds a minimal Pareto (top ~20%) learning curriculum for a professional skill, then proves it works by having simulated learners (a Total Beginner and a learner with some IT background) attempt real closed GitHub issues, graded against the real merged fix. Two tiers. (1) A free individual tier: learners generate curricula at no charge; model it as a real cost centre, since every free curriculum consumes the full generation cost. (2) A paid B2B tier: companies and bootcamps pay a recurring subscription to generate a vetted curriculum plus a readiness bar for a role they are hiring or training for, with the measured pass-rate evidence behind it. Propose the B2B pricing yourself and show the assumption. COST ENGINE, built from editable drivers, not one fixed number. One curriculum runs up to 3 Rounds. Measured so far: about 12 small LLM calls once per curriculum (Claude Haiku) plus about 3 planning calls per revision; 5 Modules in Round 1 and up to 2 more added in each later Round; each Module is attempted once per learner profile, so 10 coding-agent sessions in Round 1, up to 14 in Round 2, up to 18 in Round 3 (worst case 42 per curriculum); about 95k tokens per coding-agent session (measured once); one LLM judge call per attempt; about 8 to 10 minutes of sandbox compute per attempt; about 9 minutes of one-off sandbox compute per new task, reused after that. For price inputs use Anthropic's published Claude Haiku API token prices (look them up and cite them) and a generic container compute price per minute that you clearly label as an assumption I can replace. Label anything not measured as an estimate. Answer: (a) monthly cash balance and runway across the 24 months, (b) the raise needed to reach breakeven and the month breakeven happens, (c) gross margin on the B2B tier once the free-tier subsidy is charged against it, (d) the free-tier cap (curricula per user per month) that keeps the subsidy sustainable. Then give me a shareable link to the plan.
