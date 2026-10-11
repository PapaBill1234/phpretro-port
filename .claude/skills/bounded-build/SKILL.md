---
name: bounded-build
description: Implement one claimed PHPRetro task in its own worktree with bounded context, checks and a commit handoff.
---

Read the task brief, owned paths and its one relevant contract. Before editing, record base SHA and claim. Exact-search the specified paths; inspect only matching code and direct dependencies.

Reuse verified code from the preservation audit before recreating it. Check license, actual behavior and tests; line count is not evidence. Do not import the old operations/runtime tree or assets wholesale.

Keep changes within scope. Cross-scope changes return a new dependency task. Run the task's meaningful checks, including real-service checks when the acceptance contract requires them. Stop after the second identical failure and return the failing command, concise error, hypothesis and source evidence to Sol.

Commit explicit owned files with the task ID; record checks and remaining gaps. Return `task`, `base`, `commit`, `paths`, `checks`, `evidence`, `open_gaps`, `usage`. Keep the handoff under 600 words. Worker branches may be pushed; the integrator alone updates main after review.

Save large outputs privately by path. Never paste complete files/logs or unrelated history into the lead context. If assigned budget is reached, checkpoint the worktree and return partial status; do not claim completion.
