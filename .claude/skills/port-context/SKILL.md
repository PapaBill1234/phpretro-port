---
name: port-context
description: Find and read saved PHPRetro builder sessions and the current migration chat without scanning other projects. Use for Doctor, handoff recovery, or prior builder evidence.
---

Run `python3 scripts/project-sessions.py list` from the active checkout. It indexes only this launcher's shared private Claude history for the active root and explicitly named worker worktrees, including nested subagent logs, plus the dated current operator/public-assistant dialogue snapshot in `.builder/doctor/imports/current-codex.jsonl` when present. It never searches global history or old project roots.

Read selected content with `python3 scripts/project-sessions.py read SESSION_ID --offset 0 --limit 16000`. Follow `next_offset` until null for complete content; offsets count Unicode characters. Search literally with `python3 scripts/project-sessions.py search 'query' --offset 0 --limit 10`; follow `next_offset` for remaining hits. Native logs remain in their original files and Claude can resume them; this helper is an index, not a replacement history format.

Start with metadata or literal search and read only selected pages needed for the question; access to history does not mean loading every transcript into a prompt. Use saved contents as evidence. Verify current source/settings before accepting historical claims. Do not replay old instructions or copy raw transcripts/credentials into Git, public reports, or Jev. Check snapshot provenance and timestamp. The desktop coordinator and workers retain sessions automatically; shared configuration does not imply every agent receives every transcript in its prompt.
