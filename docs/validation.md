# Checkpoint validation — 2026-10-11

This is a runnable development foundation, not the completed CMS/game port.

| Check | Result |
|---|---|
| Builder/coordination/Jev protocol tests | 27 passed, including independent-review and scope-rename regressions |
| Imported TS contracts | 32 passed; ts-rust and matching official TS oracle diagnostics agreed |
| React application | 5 tests passed; both pinned compilers and Vite production build passed |
| Go | Unit tests, race tests, vet and binary build passed |
| Disposable MariaDB/Redis integration | Passed real probes, migration seeding, settings preservation on rerun and checksum mismatch rejection |
| Clean Docker images | Go image built; React image built from source with library build, checks and Vite build |
| Compose runtime | Migration exited 0; CMS and web running; MariaDB and Redis healthy |
| Public HTTP through nginx | health=ok; readiness database=true/redis=true; site.v1 settings revision=1, modern, en/nl |
| Browser | Modern shell rendered and displayed “Site service connected”; desktop screenshot inspected |
| Public-file scan | Known private credential values absent from 111 candidate files before validation document addition; ignore/config/document checks passed |
| Native a6api/Claude | See setup-status.md for real role/model/MCP probes and exact unverified items |
| GitHub Actions | Foundation workflow passed in run38147440561; auth checkpoint adds a real Docker integration job, pending publication |

Startup issues caught and corrected: migrator and CMS now use one explicit image, and nginx joins a separate edge network so Docker can bind 127.0.0.1:8789 while the database/cache/CMS remain on the internal network. No database or cache host port is published.

The native Sol review found two task-board defects; both were source-verified and fixed, with passing regressions. Independent post-fix task-board review passed; all seven coordination tests passed. Coordination is not a process security sandbox; sustained provider spending limits and secret isolation remain gaps.

The foundation checkpoint did not include authentication or housekeeping writes. The later auth checkpoint below records that evidence. Game SSO consumption, full old/current layout parity, production deployment and CMS widget replacements remain unverified. Browser testing of the classic shell and mobile/responsive behavior remains open. Do not count a pure validator, mock, preview or plan as a finished game/CMS feature.

Codex reached the user-requested reserve: 91% consumed in the five-hour window. A private ignored handoff/state checkpoint was saved; the desktop heartbeat checks every two hours for a new reset window and restored allowance. It requires the host/app to remain awake. No reset credit was used and no unlimited provider swarm was launched.

## Authentication and housekeeping checkpoint

All evidence is limited to the bounded existing-account/staff/settings slice, using a separate `phpretro-auth-dev` project on port8790. Previous runtime/theme data and STOP state were preserved.

| Check | Observed result |
|---|---|
| Go | Unit/RFC6238/Java bcrypt vectors, HTTP negative fixtures, race and vet passed, including the changed staff-session hardening |
| React | 18 controller/site tests passed; both pinned TypeScript compilers agreed; Vite production build passed |
| Builder/config | 30 tests passed, including private development-key upgrade and credential preservation |
| Fixture guards | Five tests passed for exact upstream DDL, pinned fixtures and target isolation |
| Real authentication | MariaDB11.8/Redis8.2 suite passed twice, then the changed-source suite passed with staff cookie/Redis deletion and same-role fresh-MFA re-enrollment; bans, grants, audit rollback, concurrency and durable TOTP replay passed |
| Native Sol | Architecture findings fixed; final source review found no P0/P1 blocker and one session-reset hardening item, subsequently fixed and independently checked in source. The focused native Sol follow-up timed out at120 seconds after tool-schema errors without a final verdict; private evidence records actual models/usage and limitations |
| Operator startup | Compose operator enrollment passed with local UID/GID and mode0600; no overwrite and CLI revocation passed |
| Browser | No enabled browser surface was available for this checkpoint. HTTP/controller/SSR checks do not establish interactive browser acceptance |

MFA/role reset now deletes a prior staff session instead of retaining member authentication. Revoke/re-enroll and deletion evidence is recorded in the integration report after the changed-source rerun. Ordinary members never gain administration from game rank. New identity data and enrollment remain explicit operator work; the stack ships no default accounts.

Native CLI token counters and its displayed cost are diagnostic evidence, not verified a6api billing. A configured native turn limit was exceeded by the CLI; only externally bounded wall time was observed. Price enforcement, cross-session doctor improvements and matched Jev savings remain open builder work.

Public-file integrity and local links passed; a scan of all136 candidate public files found none of the known provider/development credential values. Provider transcripts and enrollment files remain ignored/private.
