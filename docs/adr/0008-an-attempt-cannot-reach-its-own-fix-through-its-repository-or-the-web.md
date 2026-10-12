# An Attempt cannot reach its own fix through its repository or the web

CONTEXT.md says a Student Agent never sees the Ground Truth and that its knowledge of the Target Skill is bounded to the Chunks it retrieves. Three gaps in the first end-to-end runs (2026-10-11) broke both rules without anyone choosing to.

**The repository carried the fix.** An Attempt was a `git clone` checked out at the pre-fix commit, so the merged fix and everything after it sat in the Attempt's own history behind an `origin` remote. Measured on a live workspace: 32 commits past the checked-out one were reachable from `origin/master`, and 1,390 in the full clone. An agent with a shell could `git log origin/master` its way to the answer. An Attempt's workspace is now `git init` plus a fetch of the pre-fix commit alone, so it holds that commit's ancestors and nothing newer, and no remote is left to fetch more from (4,194 commits against 5,584, verified against the real repo, with `entire enable` and `entire graph index` unaffected). The Judge fetches the Ground Truth itself, into the Attempt's workspace, after the Attempt ends (`judge.fetch_ground_truth`).

**The agent could browse.** OpenCode's `webfetch` and `websearch` let a Student Agent read documentation the Curriculum doesn't teach (the first Attempt that ran made only `webfetch` calls), so a pass would show the agent could search, not that the Curriculum worked. Both are denied in `opencode.json`.

**What this does not close.** A shell can still reach github.com (`curl`, `git fetch <url>`). OpenCode has no per-host network rule, and `--auto` runs shell commands unprompted, so an agent that sets out to fetch the fix can. What is bounded is the repository and the tools it was given, not the machine. A Round's pass rate should be read with that in mind until the Attempt runs somewhere without outbound network.

## Considered Options

- **Keep the full clone and trust the prompt** — rejected: the prompt already says nothing about the fix, and the leak was never the prompt. Ground Truth separation is meant to be structural, not a request.
- **Clone fully, then delete the later refs and `gc --prune=now`** — rejected: objects can linger in packs, reflogs and `fsck` output, so it leaves a leak that looks closed. Never having the objects is the only version a test can pin.
- **Deny the shell's network access too** — rejected for now: OpenCode cannot scope it, and denying the shell outright removes the edit-run-commit loop an Attempt exists to perform (ADR-0005).
