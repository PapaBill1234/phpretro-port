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
| GitHub Actions | Workflow supplied; remote execution outcome must be checked after publication |

Startup issues caught and corrected: migrator and CMS now use one explicit image, and nginx joins a separate edge network so Docker can bind 127.0.0.1:8789 while the database/cache/CMS remain on the internal network. No database or cache host port is published.

The native Sol review found two task-board defects; both were source-verified and fixed, with passing regressions. Independent review after those fixes remains pending. Coordination is not a process security sandbox; sustained provider spending limits and secret isolation remain gaps.

No acceptance evidence exists yet for real authentication, housekeeping writes, game SSO consumption, full old/current layout parity, production deployment, or CMS widget replacements. Browser testing of the classic shell and mobile/responsive behavior remains open. Do not count a pure validator, mock, preview or plan as a finished game/CMS feature.

Codex reached the user-requested reserve: 91% consumed in the five-hour window. A private ignored handoff/state checkpoint was saved; the desktop heartbeat checks every two hours for a new reset window and restored allowance. It requires the host/app to remain awake. No reset credit was used and no unlimited provider swarm was launched.
