---
name: diff-review
description: Independently review a PHPRetro architecture or completed task diff for source-backed contract violations and regression risks.
---

Use Sol at architecture lock, after two identical failures, and before integration. Read the task acceptance criteria, baseline and actual diff, then inspect direct dependencies and the smallest relevant source contract. Keep review read-only.

Check identity/SSO/session/permission invariants, MariaDB ownership, theme/API separation, translation coverage, asset provenance, migration reversibility and meaningful acceptance evidence where relevant. Verify important findings against source; Jev may triage findings but does not approve a commit.

Return actionable findings with severity, path/line, concrete trigger and impact. Distinguish a missing test from a proven bug. State untested boundaries and compiler/service limitations. Empty findings are acceptable; never invent defects to fill a quota.

Verify the combined diff before integration. A task SHA reviewed before a merge conflict resolution is not review evidence for the resolved result. Release-blocking gaps remain open until their real boundary checks pass.
