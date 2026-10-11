---
name: jev-triage
description: Narrow already retrieved PHPRetro evidence with batched typed Jev judgments; excludes code generation and permission decisions.
---

Use exact search or deterministic filters to find candidate paths/excerpts first. Give Jev a small redacted state containing only those facts and one batch of related yes/no, choice or score questions. Include no-match when appropriate. Prefer the file-input CLI for large local candidate sets; do not load full files into the lead merely to forward them.

Use the verdict to select the next source inspection, not to establish truth. Verify important auth/schema/security conclusions against their cited sources. Handle `escalate`, unavailable and uncertain responses yourself; no unchanged retry.

Do not send keys, credentials, personal data, complete files or unrelated context. Jev does not generate code/prose or discover missing facts. Never invoke `jev_gate`, enable a PreToolUse gate or use a verdict for permission/automatic approval.

Record call count, usage and latency; cost may be unknown. A call proves no savings. Follow `docs/builder-operations.md` for matched same-task/same-model comparisons before making an efficiency claim.
