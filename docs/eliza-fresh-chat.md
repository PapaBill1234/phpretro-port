# Fresh chat (New Chat in the shell ChatOverlay)

Status: pending review. Deliverable is a source patch against the pinned upstream
checkout — **not** a live-app change.

## Artifacts

- `builder/eliza/patches/new-chat.patch` — the public patch (3 files, +188/−3).
- Upstream pin: `28fef47c9d9d3acc7792be0fdaa3070020293cc5` (clean; MIT notices and
  `LICENSE` untouched; no CMS/theme/port edits).

Apply on the pin:

```
git -C <clean-pin-checkout> apply builder/eliza/patches/new-chat.patch
```

## Requirement

The real `packages/ui/src/components/shell/ChatOverlay.tsx` composer "+" menu
must offer **New chat** that creates and selects a fresh persisted conversation
with empty visible messages, keeps the previous conversation in history, does not
leak the old thread's context into the new one, and (after reload) retains both
conversations with the new one selected. Creation-pending and creation-failure
must not lose the current chat.

## What the patch does (source-backed)

Reuses the existing upstream helper; it does not fake a placeholder or clear all
history.

- `ChatOverlay.tsx`
  - Subscribes to `s.handleNewConversation` (same selector as the already-used
    `s.handleSelectConversation`) — `state/useChatCallbacks.ts#handleNewConversation`,
    the exact helper `ConversationsSidebar`'s "New chat" calls. It interrupts the
    current pipeline, resets draft state, creates the conversation via
    `client.createConversation`, activates it, commits empty messages
    (`conversationMessagesRef = []`), and `claimConversationMessagesOwnership`
    isolates the room — so the old thread's context is not leaked. On failure it
    rolls back to the previous conversation (its `catch` block).
  - Adds a **New chat** `DropdownMenuItem` to the composer "+" menu, disabled
    while `booting || firstRunOpen || responding || conversationLoading ||
    newChatPending`.
  - Updates the stale sheet-header comment ("no clear/new-chat").
- `packages/ui/src/components/shell/useNewChatAction.ts` (new) — small hook that
  wraps the shared creator: fire-once per gesture (sync ref closes the
  same-frame double-tap gap before the disabled item paints), exposes a `pending`
  flag, and reports a create failure via `onError` without throwing into the
  gesture handler.

## Qualification (what was actually run)

Environment: pinned runtimes from
`research/.phpretro-builder/runtimes` (Node `node-v24.15.0-linux-x64`,
Bun `bun/package`); dependencies **not** installed — scratch `node_modules`
symlinks the reviewed research install (read-only).

1. Focused real component/hook test — `PASS`
   `packages/ui/src/components/shell/__e2e__/new-chat.test.tsx` (jsdom, pinned
   Vitest 5.0.3), 3/3:
   - activates one fresh conversation through the shared creator (`handleNewConversation`
     invoked once);
   - a same-gesture double tap enqueues only one create (guard);
   - a create failure is reported via `onError`, not thrown, and `pending`
     releases.
   Command:
   ```
   cd packages/ui
   NODE_OPTIONS="--no-experimental-webstorage --disable-warning=ExperimentalWarning" \
     ../../node_modules/.bin/vitest run --config ./vitest.config.ts \
     src/components/shell/__e2e__/new-chat.test.tsx
   ```
2. Lint/format — `PASS` `biome check` on the changed files, no diagnostics.
3. Type sanity — `tsc --noEmit -p packages/ui/tsconfig.json`, filtered to the
   changed files, reports **no** error on the new code. (The run also emits
   pre-existing environmental `TS2307` (missing `lucide-react` / `motion/react`
   / `@capacitor/keyboard` type resolution) and unrelated implicit-any hits,
   present on the untouched pin.)
4. Patch integrity — `git apply --check --reverse` on the pin tree succeeds,
   i.e. the patch is exactly the reviewed diff and applies cleanly to `28fef47`.

## Pending / unknown (not run here)

- **Real-browser acceptance is not proven.** Creating the conversation, the empty
  transcript, the preserved prior conversation, room isolation, and the
  reload-retains-both-and-selects-new behaviors exercised through the actual
  running overlay + persistence layer (`ChatOverlay` menu interaction and
  `client.createConversation` persistence/reload) remain a pending browser proof.
  The worker had no isolated browser/server within budget; no live app was
  restarted and no private conversations, `.env`/account keys, or research
  original assets were read.
- The `+`-menu interaction itself (opening the Radix menu and clicking the item)
  is not covered by an automated test here; the wiring is verified by source
  inspection plus the hook/guard tests above.
