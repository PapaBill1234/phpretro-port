---
name: token-audit
description: Analyze metadata-only multi-agent usage and matched Jev trials to improve PHPRetro cost per accepted task.
---

Read sanitized run records, not raw transcripts, keys or production data. Group by task/model/role/version and report input, cache-read input, cache-write input, output, wall time, retries, checks and acceptance. Preserve missing counters as null/unknown. Inspect provider definitions before combining input/cache counters; do not double-count included cache tokens.

Use native `/doctor` for setup/context overhead and `/doctor prompt-audit` when supported. It does not aggregate every session or prove provider billing accuracy. Actual a6api request/billing records are the cost authority.

For Jev experiments match question/task, main model, starting SHA, tests and settings with/without Jev. Include main-model input/cached-input/output, elapsed time, Jev calls/tokens/estimated cost and answer correctness. Repeat enough samples to separate noise; do not report savings from one cheap call.

Recommend a narrow change supported by evidence: smaller brief, fewer retries, cached source excerpt, changed role, or less delegation. Skills are progressive disclosure, not lossless compression. Re-evaluate accepted quality after changing budgets/routing.
