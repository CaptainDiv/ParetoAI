# Demand Sources for smart contract development (Solidity/EVM)

**Research date: 2026-10-10.** Every candidate below was fetched with the Analyst's own
`fetch_source_text` (plain `requests.get` + BeautifulSoup, `PAGE_TEXT_CHARS = 5000`, no JS, no auth),
not with a browser or a JS-capable fetcher. Status codes, extracted-text lengths and offsets are
measured, not assumed.

Criteria, per the `Source` entry in `CONTEXT.md` and ADR-0003:

1. **Demand-shaped** — measures what the job/market requires, not what a technology does.
2. **Decomposes** into named sub-skills.
3. **Publishes a weighting or ranking.**
4. **Live-fetchable without auth** by a plain GET of server-rendered HTML.

---

## Headline findings

### 1. ADR-0003 is wrong about O*NET, and this is the single best Source available

ADR-0003 states "O*NET has no smart-contract occupation". **Refuted.** O*NET has
**15-1299.07 — Blockchain Engineers**, "Updated 2026", Bright Outlook
([onetonline.org/link/summary/15-1299.07](https://www.onetonline.org/link/summary/15-1299.07)).
The `/link/details/` view publishes **18 tasks and 41 work activities with numeric 0–100 importance
ratings**, server-rendered, and — verified — **all 17 rated task rows land inside the existing
5000-character cap** (the `Importance` column header begins at offset 2,539 of the extracted text).
It is CC BY 4.0 and `robots.txt` permits `/link/`. This is occupational task data with
per-item importance ratings: the exact first example in the `Source` definition.

### 2. ADR-0003's expectation about certification exam objectives has NOT held up

ADR-0003 says "Certification exam objectives are the one demand Source that both decomposes into
sub-skills **and publishes weightings**, so they carry most of the load for the MVP."

**No smart-contract certifying body found publishes per-domain percentage weightings.** Not
Blockchain Council, not Cyfrin Updraft, not 101 Blockchains, not W3CB. Several publish an
enumerated objectives list (useful, criterion 2) but none publish "Domain 3: 25% of exam"
(criterion 3). Worse, the largest certifier by volume — Blockchain Council — **explicitly forbids
automated fetching in its Terms & Conditions**, so it is out on legal grounds regardless.

**The accepted thin-coverage risk has partly materialised, in exactly this component.** The MVP's
weighting signal cannot come from exam objectives. It has to come from O*NET importance ratings and
the OWASP ranked list instead.

### 3. ADR-0003 is right about Stack Overflow, and it is worse than stated

"Solidity" does not appear **anywhere** in the 2025 survey technology page — not in the extracted
text and not in the raw HTML (checked both). In 2024 it appears once, at **1.1%**, 40th of 49
languages. Confirmed: one language-level datum, no sub-skill decomposition.

### 4. Cross-cutting constraint: `PAGE_TEXT_CHARS = 5000` decides two candidates

For standards documents the cap, not the server, is the binding constraint. The EEA EthTrust
checklist is fetchable and its requirement levels are a real ranking, but its first requirement sits
at **offset ~5,868** — just past the cap, so the Analyst would ingest only front matter and
typographic conventions. Same for the full EthTrust spec (190,887 chars extracted, requirements far
beyond reach). OWASP SCWE is 16,122 chars, of which the cap sees the first ~30%.

---

## Recommended set

Four Sources I would configure now, plus one conditional. All verified fetchable this date.

| # | Source | URL | Weighting it contributes |
|---|---|---|---|
| 1 | O*NET — Blockchain Engineers (details view) | `https://www.onetonline.org/link/details/15-1299.07` | 0–100 task importance (92, 91, 91, 88, …) |
| 2 | OWASP Smart Contract Top 10 (2026) | `https://scs.owasp.org/sctop10/` | Ordinal rank SC01–SC10, survey-derived |
| 3 | Cyfrin Updraft SSCD+ certification objectives | `https://updraft.cyfrin.io/certifications/solidity-certification` | None — enumeration only |
| 4 | OWASP SCWE (weakness enumeration) | `https://scs.owasp.org/SCWE/` | None — enumeration only |
| 5 (conditional) | EEA EthTrust Security Levels v3 Checklist | `http://hub.entethalliance.org/wg-ethtrust-site/spec/v3/checklist.html` | `[S]`/`[M]`/`[Q]` requirement tiers — **needs the cap raised to ≥12,000** |

**What the resulting table would measure, honestly stated.** A blend of two different demand
signals, not one: (a) what a US employer of a blockchain engineer rates as important day-to-day
(O*NET — infrastructure, security testing, cryptographic protocols, deployment automation), and
(b) what practising auditors rank as the most prevalent and costly smart-contract failure classes
(OWASP). Sources 3–5 add decomposition — named Solidity-level sub-skills such as account
abstraction, upgradeable contracts, opcodes, gas optimisation, `delegatecall`,
checks-effects-interactions — but contribute **no weight**, so they can only broaden the table, not
rank it.

**Two honest limitations to carry forward:**

- **Granularity mismatch.** O*NET carries the only real numeric weighting, but its tasks are
  occupation-level ("Test the security and performance of blockchain infrastructures"), not
  Solidity-level ("reentrancy guards", "ERC-20 standard"). The weights are coarse where the
  Curriculum needs them fine.
- **Security bias.** OWASP + SCWE + EthTrust are all security documents. Weighted naively, the table
  will over-represent the security half of the Target Skill. This is the same partitioning hazard
  ADR-0003 diagnosed for documentation, reappearing in a demand-shaped guise: three security Sources
  will make security topics score high because three security Sources were chosen. Mitigate by
  treating O*NET and Cyfrin as the breadth Sources and counting the OWASP family as **one** security
  signal rather than three.

---

## Per-candidate evidence

### Accepted

| Candidate | HTTP | Extracted text | Content present? | 1 Demand | 2 Decomposes | 3 Weighted | 4 Fetchable | Verdict |
|---|---|---|---|---|---|---|---|---|
| **O*NET 15-1299.07 details** | 200 | 71,062 full / 5,000 capped | **Yes** — 17 of 18 tasks with importance ratings inside the cap | **Yes** — occupational task data, the definition's first example | Yes — 18 tasks, 41 work activities, knowledge, skills | **Yes** — 0–100 importance per item | Yes | **ACCEPT (best)** |
| **OWASP SC Top 10 (2026)** | 200 | 383,555 raw / 5,000 capped | **Yes** — full ranked SC01:2026–SC10:2026 list plus descriptions at offsets ~1,200–3,000 | **Yes, argued below** | Yes — 10 named risk categories | **Yes** — ordinal rank 1–10 | Yes | **ACCEPT** |
| **Cyfrin Updraft SSCD+** | 200 | 992,160 raw / 12,788 extracted | **Yes** — "What you're being tested on": 11 objectives, exam code SOL-001, 80 questions, 32% pass rate | Yes — certification exam objectives, explicitly in the definition | Yes — fuzz testing, account abstraction, upgradeable contracts, opcodes, signatures, gas optimisation, stablecoin/DeFi mechanics, transaction data, data types | **No** — no per-domain percentages | Yes (server-rendered despite the SPA-sized payload) | **ACCEPT, weaker** |
| **OWASP SCWE v1.0** | 200 | 525,118 raw / 16,122 extracted | **Yes** — ~150 SCWE IDs in a server-rendered table, each mapped to an SCSVS control group | Partly — a weakness taxonomy, not a market measure | Yes — finest decomposition of any candidate | **No** — status labels only, no prevalence | Yes, but cap sees only ~30% | **ACCEPT, weakest** |

**Is OWASP SC Top 10 demand under the definition?** I think yes, and the reasoning matters because
it is not obvious. A bare vulnerability taxonomy (SWC, SCWE) is *not* demand — it enumerates what
can go wrong, the way documentation enumerates what a technology does. But the 2026 Top 10's
**ordering** is not taxonomic. `https://scs.owasp.org/sctop10/data-sources/` (200, 12,861 chars)
documents that the ranking comes from (a) a structured anonymised survey in which smart contract
auditors, protocol security leads, bug bounty triagers and incident-response teams assign ranks 1–10
and state their confidence, aggregated to mean ranks and tier distributions, and (b) 2025 incident
data from five named breach databases. A survey of practitioners about what they actually encounter
in production is the same genus as "developer survey results", which the definition names
explicitly. The *list* is a taxonomy; the *rank* is a demand measure, and the rank is what the
Analyst would consume. Note that `/sctop10/ranking-methodology/` is linked from the index but
**404s** (verified) — only the data-sources page resolves.

Contrast SCWE, where I do **not** make that argument: SCWE publishes no prevalence ordering at all,
so it contributes decomposition only.

### Rejected

| Candidate | HTTP | Observed | Rejection reason |
|---|---|---|---|
| **BLS** | **403** on `https://www.bls.gov/robots.txt` | Akamai "Access Denied" page: *"Automated retrieval programs (commonly called 'robots' or 'bots') can cause delays… bot activity that doesn't conform to BLS usage policy is prohibited."* | **REJECTED — blocked and prohibited.** Both a technical block and an explicit policy against automated fetching. Do not configure. |
| **Blockchain Council** — Certified Solidity Developer, Certified Blockchain Developer, Certified Smart Contract Developer | 200 each (~170 KB raw, 5,000 capped) | Module titles only ("Introduction, Blockchain Basics, Introduction to Solidity, Smart Contracts Development, Hardhat and Testing, Creating Ethereum Tokens, Voting DApp, Security and Best Practices"), wrapped in promo chrome ("Festive Deal is LIVE", price, review count). No exam weightings anywhere. | **REJECTED — terms of use.** `https://www.blockchain-council.org/terms-and-conditions/` §7 prohibits *"Using any automated system (bots, scrapers, crawlers) to access the Platform without BCC's express written consent."* Technically fetchable; legally not available. Fails criterion 3 independently. |
| **Stack Overflow Developer Survey 2025 / 2024** | 200 | Server-rendered and the percentages *are* in the HTML (JavaScript 66%, HTML/CSS 61.9%, …). But **"Solidity" is absent from the entire 2025 page, raw HTML included**; 2024 has it once at **1.1%**, 40th of 49 languages. | **REJECTED — fails criterion 2.** One language-level datum, zero sub-skill decomposition. Confirms ADR-0003 and is worse than it claimed: the 2025 edition does not report Solidity at all. |
| **W3CB (Web3 Certification Board)** | 200 | `https://w3cb.org/` returns 5,627 bytes of raw HTML yielding **52 characters** of extracted text — a title and nothing else. | **REJECTED — SPA shell.** Exactly the failure mode the DEFAULT_SOURCES comment warns about. Invisible without checking. |
| **O*NET Web Services API** | **401** | `services.onetcenter.org/v1.9/ws/online/occupations/15-1299.07/details/tasks` → Unauthorized, 179-byte body. | **REJECTED — needs auth.** Use the HTML `/link/details/` page instead, which carries the same ratings. |
| **101 Blockchains** | **404** | `/certification/certified-enterprise-blockchain-professional/` and `/certification-courses/` both 404 (the 404 page itself returns 174 KB of chrome — would look "fetched" to a length check). No own objectives page located. | **REJECTED — 404.** |
| **ConsenSys Academy** | — | No first-party syllabus page with published structure; its courses are hosted on Coursera and Udacity, which are auth-gated and not the certifying body's own objectives page. | **REJECTED — no primary source.** |
| **Secureum** | — | Epoch Infinity **paused August 2025**, no further RACEs. RACE quiz contents are documented on a third-party blog (`ventral.digital`), not on a Secureum-published syllabus page. | **REJECTED — discontinued and not primary.** The brief's own rule (go to the body's own page, never a blog) excludes the only available write-up. |
| **Alchemy University** | 200 (redirects to `alchemy.com/university`) | 3,031 chars; course catalogue with lesson counts (Ethereum Bootcamp 91 lessons, Learn Solidity 11 lessons). | **REJECTED — fails 1 and 3.** A course catalogue, no weighting beyond lesson counts. |
| **Speedrun Ethereum** | 200 | 3,969 chars, server-rendered; challenge list (#0 Tokenization, #1 Crowdfunding, #2 Token Vendor, #3 Dice Game, …). Decomposes cleanly. | **REJECTED — fails 1 and 3.** Curriculum sequence, no weighting, no demand measure. Would be a decent Module **Resource**. |
| **SWC Registry** | 200 | 4,343 chars, full weakness table inside the cap. But the page's own banner: *"this content is no longer actively maintained… has not been thoroughly updated since 2020… known to be incomplete and may contain errors as well as crucial omissions,"* and redirects readers to EthTrust and SCSVS. | **REJECTED — stale and unranked.** Superseded by SCWE. Fetchability is fine; the content is five years dead and carries no prevalence ordering. |
| **OWASP SCSVS index** | 200 | 2,689 chars; 11 control groups (SCSVS-ARCH, -CODE, -GOV, -AUTH, -COMM, -CRYPTO, -ORACLE, -BLOCK, -BRIDGE, -DEFI, -COMP) with one-line descriptions. Verification levels L1/L2/L3 were **reworked into "SCS Testing Profiles" in the 2024 refactoring** and the page says the levels are only a temporary carry-over in the checklist. Note: several descriptions are visibly mismatched to their labels (SCSVS-ORACLE described as arithmetic safety, SCSVS-BLOCK as DoS, SCSVS-DEFI as gas optimisation). | **REJECTED — too coarse, and in flux.** 11 category labels is nearer one blob than a sub-skill list; SCWE covers the same ground far more finely. |
| **OWASP SCS Checklist** | 200 | 2,774 chars. The actual checklist is a **Google Sheet / interactive JS widget** — not in the HTML. The page does carry a small useful "What to Check During Audits" table. | **REJECTED — substantive content not in the returned HTML.** |
| **EEA EthTrust full spec v3** | 200 | 190,887 chars extracted — but requests `https://entethalliance.org/specs/ethtrust-sl/` and is **redirected to plain HTTP** `http://hub.entethalliance.org/wg-ethtrust-site/spec/v3/`. First 5,000 chars are front matter, contributors and typographic conventions; `[S]` first appears at offset 6,691, `MUST` at 7,535. | **REJECTED at the current cap.** Fetchable, but the Analyst would ingest only front matter. Use the checklist instead (next row). |
| **EEA EthTrust v3 Checklist** | 200 | 38,393 chars, requirements genuinely present and well-formed ("[S] No `tx.origin`", "[S] Use Check-Effects-Interaction", "[S] No `delegatecall()`", "[S] Check External Calls Return", plus named compiler bugs). First requirement at offset **~5,868** — **just past the 5,000 cap**. | **CONDITIONAL.** Needs `PAGE_TEXT_CHARS` ≥ 12,000 to see the `[S]` tier, ~38,000 for all tiers. See stability warning below. |

---

## Licensing and terms of use

| Source | Position on programmatic fetching |
|---|---|
| **O*NET** | **Clear.** Data is **CC BY 4.0** from USDOL/ETA ([onetcenter.org/license_db.html](https://www.onetcenter.org/license_db.html), [services.onetcenter.org/help/license_data](https://services.onetcenter.org/help/license_data)). Attribution required: credit O*NET and the U.S. Department of Labor, Employment and Training Administration, link the licence, and note any changes; `O*NET®` is a USDOL/ETA trademark. `robots.txt` (verified, 200) disallows only `/shared/rate`, `/exit`, `/share/`, `/follow/`, `/_tryout/`, `/link/jobinfo/`, `/link/localjobs/` and `/demo` — **`/link/details/` and `/link/summary/` are permitted**. Note `onetcenter.org/terms.html` and `onetonline.org/help/legal/` both 404; the licence pages above are the live ones. |
| **BLS** | **Prohibitive.** Explicit anti-bot usage policy, enforced at the CDN with a 403 on every request including `robots.txt`. Ruled out for the same reason the parent spec rules out job-board scraping. |
| **Blockchain Council** | **Prohibitive.** T&C §7 bars bots, scrapers and crawlers without express written consent, notwithstanding a permissive `robots.txt`. Site content is also asserted as proprietary. |
| **OWASP (scs.owasp.org, owasp.org)** | **Clear.** OWASP projects are openly licensed and the material is published for reuse. `scs.owasp.org/robots.txt` **404s** (it is an MkDocs Material site with no robots file), so there is no stated restriction. |
| **EEA EthTrust** | **Apache License 2.0**, stated in the document: *"Unless otherwise explicitly authorised in writing by the EEA, you can only use this document in accordance with those terms."* Permissive for fetching and quoting with attribution. |
| **Cyfrin Updraft** | No prohibition found in the page itself. Not independently verified against a separate T&C document — **worth a check before configuring**, as it is a commercial platform and the same hazard as Blockchain Council applies. |
| **Stack Overflow Survey** | Raw results are offered under the **Open Database License**, with an explicit in-page download acknowledgement. Permissive — but moot, since the content fails criterion 2. |

---

## Stability of recommended URLs

| Source | Stability | Note |
|---|---|---|
| O*NET `/link/details/15-1299.07` | **Stable** | SOC-code-addressed and versioned by O*NET release. The occupation code is the durable identifier; the page is marked "Updated 2026". The content will drift as O*NET re-surveys, which is the point. |
| OWASP `scs.owasp.org/sctop10/` | **Stable URL, moving content** | The bare `/sctop10/` path tracks the *current* edition — it served 2025 and now serves 2026. Good for always-current demand; bad if a Pareto Table needs to be reproducible. Pin `/sctop10/archive/2025/Top10:2025/` (verified 200) if reproducibility matters. |
| Cyfrin `/certifications/solidity-certification` | **Probably stable** | Exam code SOL-001 suggests a durable credential, but it is a single vendor's commercial page with no stability commitment. |
| OWASP `/SCWE/` | **Unstable content** | The page states v1.0 is "under active development, so it is subject to change any time (e.g. structure, IDs, content, URLs)". Expect IDs to move. |
| EEA EthTrust checklist | **Poor — flag this** | The canonical URL the document gives for itself, `https://entethalliance.org/specs/ethtrust-sl/v3/checklist.html`, **404s** (verified). The only working URL is `http://hub.entethalliance.org/wg-ethtrust-site/spec/v3/checklist.html` — a staging-looking host, served over plain **HTTP**, labelled "Editor's Draft" with an unresolved `@@ March 2025` date placeholder. The github.io editor's-draft URL the document cites also 404s. Configuring this means configuring an undocumented internal host. |

---

## Suggested follow-ups (not actioned here)

1. `DEFAULT_SOURCES` in `pareto_ai/analyst.py` can be replaced with the recommended set; the
   placeholder comment's "server-rendered pages only" constraint survives the swap intact and
   correctly predicted the W3CB failure.
2. `PAGE_TEXT_CHARS = 5000` is now a content decision, not just a token budget — it is what excludes
   EEA EthTrust and truncates SCWE. Per-Source caps would be better than one global cap, since O*NET
   needs ~5,000 and the EthTrust checklist needs ~38,000.
3. `ParetoEntry.frequency` still computes `len(sources)`, which cannot represent any of the weighting
   scales found here (0–100 ratings, ordinal 1–10 ranks, `[S]`/`[M]`/`[Q]` tiers). Issue #11.
4. ADR-0003's consequences section needs amending on two points of fact: O*NET *does* have a
   blockchain occupation with task importance ratings, and certification exam objectives do *not*
   publish weightings for this Target Skill.
