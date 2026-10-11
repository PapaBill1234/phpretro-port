# PHPRetro Port

A Go CMS project and React/TypeScript website for Polaris and Octane, with MariaDB, Redis, interchangeable themes, and housekeeping-managed localization.

This repository contains the researched migration plan, source contracts, ranked gaps, builder tooling, an audited backend/frontend contract subset reused from the previous attempt, and a runnable Go/React/MariaDB/Redis foundation. It is not a finished CMS. Progress is counted only when implementation and its acceptance evidence land.

Start with [the build plan](docs/build-plan.md), [the gaps ledger](docs/gaps.md), and [builder operations](docs/builder-operations.md). Source findings are in [legacy inventory](docs/legacy-inventory.md) and [the emulator contract](docs/emulator-contract.md). The [theme contract](docs/theme-contract.md) keeps presentation independent of the backend.

Decisions made on 2026-10-11:

- Go replaces the PHP CMS. Polaris remains the Java game server.
- MariaDB stores both CMS and emulator data in separately owned databases; Redis is a cache and coordination aid.
- React uses ordinary TypeScript. `pingdotgg/ts-rust` supplies the Rust-written TypeScript compiler/type checker; it is not a Flash converter or a new browser language.
- Octane and Octane-Renderer supply the browser game-client integration, subject to protocol and asset provenance checks.
- `legacy` and `modern` themes recreate the requested layouts using newly authored code and original or explicitly licensed assets.
- Agents commit bounded changes on their own branches; the integrator publishes reviewed changes to `main`.

No credentials, provider transcripts, upstream art, or legacy SWF binaries belong in this public repository. Code is licensed GPL-3.0-only; third-party code and assets retain their own applicable licenses and notices. See [third-party notices](THIRD_PARTY_NOTICES.md) for the reused subset.

The provider connection and local machine readiness status are recorded in [setup status](docs/setup-status.md). Run `python3 scripts/repo-check.py` to validate the public package. Observed checks and remaining verification are in [validation](docs/validation.md).

The first development implementation supplies persisted site settings, dependency readiness and two original theme previews. [Start locally](docs/run-local.md) for the runnable foundation and its current scope.
