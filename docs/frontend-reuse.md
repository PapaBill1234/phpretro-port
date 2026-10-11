# Reuse audit: phpretro-preservation frontend

Audit date: 2026-10-11. Source: [PapaBill1234/phpretro-preservation at `af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7`](https://github.com/PapaBill1234/phpretro-preservation/tree/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7). This report covers frontend themes, presentation widgets, localization, tests and code licensing. Backend reuse is evaluated separately. No application source has been copied into this repository by this audit.

## Finding

**Reuse the typed contracts, small view adapters and localization regression work as a starter foundation.** The frontend is a useful tested seed, with 12 source files and 7 test files totaling 765 lines. It is not an implementation of either the PHPRetro original layout or the current Habbo layout. No CSS, images, fonts, sound, SWF replacements or canvas/Web Audio widget code are present under `frontend/`.

The entrypoint creates `DemoUser` with `look: "development-placeholder"`, synthetic account/home state and a community fixture. It renders all views on one screen; it has no API fetching, router, authentication flow, housekeeping screen or locale switch. `StarterShell` is a heading and paragraph. The account `widgets` array contains string name/value display items, not portable plugin widgets or a home editor. [Entrypoint](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/src/main.tsx#L10), [shell](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/src/StarterShell.tsx#L4), [account widgets](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/src/account/AccountView.tsx#L3).

## Keep, adapt and reject map

“Keep” means preserve the useful implementation or tests with attribution. “Adapt” means a specific contract change is necessary before using it as a production module. “Reject” means exclude it from production scope, while retaining synthetic fixtures in tests if useful.

| Source files | Decision | Concrete use and required adaptation |
| --- | --- | --- |
| `src/profile/viewModel.ts`, `src/account/viewModel.ts` | Keep + adapt | Positive integer-ID checks, gender/field checks, explicit projection and normalized profile tabs are useful. Share the duplicated profile DTO, align fields with the backend contract, validate lengths/date/figure semantics as appropriate and avoid treating the browser as the security authority. |
| `src/community/viewModel.ts` | Keep + adapt | Preserve the 20-item bounds and typed list validation. Copy/project rooms and tags as well as discussions/news; currently rooms/tags preserve input objects and extra fields. Specify string/date/image URL bounds and whether group IDs must be positive. Align pagination with backend API. [Validation/projection](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/src/community/viewModel.ts#L28). |
| `src/home/viewModel.ts` | Keep + adapt | Home visibility and malformed-owner cases are useful unit behaviors. Go must enforce private-home access before sending data; a frontend owner check cannot protect an already delivered record. Resolve the version collision described below and project accepted payload fields rather than returning the input object. |
| `src/viewModel.ts` | Adapt | Useful presentation-shell DTO and unsupported-version check. Its navigation validator only checks `Array.isArray`, not item label/href shapes. Define safe navigation targets before rendering them; project allowed fields and give this shell a distinct contract ID. |
| `src/themeManifest.ts` | Adapt substantially | Preserve versioned manifests and the rule that themes declare their presentation boundary. Current manifest hardcodes `StarterShell`, a single `home.v1` view model and only `presentation`; it cannot load multiple themes or declare optional feature slots. Add a component registry, versions/compatibility rules, theme tokens/resources and typed page/widget capabilities. [Current restrictions](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/src/themeManifest.ts#L4). |
| `src/StarterShell.tsx`, `src/profile/ProfileView.tsx`, `src/account/AccountView.tsx`, `src/community/CommunityView.tsx` | Keep as presentation/test scaffolds; adapt for actual themes | React text interpolation and empty-state branches are reusable patterns. Replace bare field dumps/lists with agreed page components, interactions and original artwork. Localize empty labels and accessibility labels. The profile navigation is an empty `<nav>`, not working tabs. |
| `src/localization/index.ts` | Keep + adapt | Preserve typed catalogue validation, regional→language→default fallback, explicit missing-key behavior and casing tests. Expand the fixed 3-key schema into namespaced catalogues; move `syntheticCatalogs` to fixtures, load approved catalogues, handle plurals/interpolation/date formats and wire locale state to the backend/housekeeping policy. [Catalogue/fallback](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/src/localization/index.ts#L3). |
| `tests/*.test.ts` | Keep + adapt | All seven test files provide a useful regression seed. Update version/key assertions when contracts change and add tests for uncovered boundary cases and API integration. Preserve synthetic-only data. |
| `src/main.tsx` | Reject as production entrypoint | Its hardcoded demo screen should live in a fixture/preview harness. Replace it with application bootstrapping, router, API state, selected theme and selected locale after contracts are ready. |
| `index.html` | Adapt | Normal Vite host markup can be reused, but `lang="en"` and static title do not implement runtime locale switching. |
| `package.json`, `package-lock.json`, `tsconfig.json`, `vite.config.ts` | Keep as reproducible initial test/build baseline; adapt deliberately | Preserve exact dependency pins for the initial import. Separate emitted test output from Vite production output before CI expansion; both currently use `dist/`. Add the accepted ts-rust lane only after a matched compiler compatibility check. Do not import the old ops/orchestrator setup as a frontend dependency. |
| Historic references, archived orchestration and claimed visual completion | Reject as implementation evidence | Keep selected documents as contextual references only. Actual source and freshly run checks establish frontend coverage. There are no original/current theme assets to salvage here. |

## Contract collision to fix first

Two incompatible payloads share the same literal version:

- [`src/viewModel.ts`](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/src/viewModel.ts#L1): `home.v1` means `siteTitle`, `welcome`, `navigation`.
- [`src/home/viewModel.ts`](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/src/home/viewModel.ts#L10): `home.v1` means `ownerId`, `username`, `publicContent`.

Local imports keep current tests passing because each test uses the intended module, but a shared theme/API dispatch registry could not distinguish the two by version. Give the shell and user-home separate stable names, synchronize them with backend DTOs and rename related tests/imports in one change. Do not claim the existing `home.v1` tag is a complete shared cross-layer contract.

## Localization and visual gaps

The only frontend catalogues are synthetic English/French with `siteTitle`, `welcome` and `navigationHome`. The actual browser entrypoint never calls the locale helper. Empty-state and accessibility strings remain hardcoded in the view components, and document language stays English. There is no housekeeping selection, persisted user preference, live catalogue loading, locale-change invalidation or translated content UI in this frontend.

`visual-localization.test.ts` compares returned view-model values and field keys. It does not start a browser, render screenshots, test responsive layout, or measure visual parity. Account/profile/community tests call component functions and examine React element props; they do not exercise DOM events, browser navigation or real authenticated API flows. Keep those tests, but name their evidence as presentation-contract coverage. [Visual test implementation](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/tests/visual-localization.test.ts#L8).

The placeholder `look` string is displayed as text; there is no placeholder avatar artwork to convert into production art. Both target layouts and the avatar/badge/Trax widgets still require the original-asset pipeline in [flash-modernization.md](flash-modernization.md). Treat the current frontend as scaffolding, not a head start on faithful visual reconstruction.

## Fresh local verification

Inspected `package.json` scripts, Vite/TypeScript configuration and lockfile before running commands. No root package lifecycle script exists. The lockfile marks `esbuild` and `fsevents` as packages with install scripts, so lifecycle scripts were disabled for installation; the explicitly invoked Vite build still completed successfully. Dependencies came from the existing pinned npm lock; no package/version inference or upstream orchestration was used.

Environment: Linux, Node **26.10.0**, npm **11.19.1**; package requires Node `>=26.7.0`. Pinned frontend packages include React/React DOM **19.1.1**, TypeScript **5.9.2**, Vite **6.4.4** and React plugin **4.7.0**. [Package scripts/pins](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend/package.json#L6).

| Command in the audited clone's `frontend/` | Fresh result |
| --- | --- |
| `timeout 120 npm ci --ignore-scripts --no-audit --no-fund` | Exit 0; 71 packages installed; reported 3 seconds |
| `timeout 60 npm run typecheck` | Exit 0; TypeScript 5.9.2 strict check passed |
| `timeout 60 npm test` | Exit 0; 29 tests passed, 0 failed/skipped; test runner duration 536 ms |
| `timeout 60 npm run build` | Exit 0; Vite transformed 33 modules; build reported 1.54 seconds; generated 0.32 kB HTML and 192.30 kB JS (60.18 kB gzip) |
| `git status --short` after checks | Clean tracked source/lockfile state; installation/build artifacts remain ignored in the research clone |

The Vite build was run after the test suite because it clears/replaces the same `dist/` directory that TypeScript tests use. These results establish local compilation, unit regression and bundling. They do not establish frontend/backend integration, production security, visual parity, ts-rust compatibility or emulator/client behavior. The upstream full Go/security merge gate is outside this frontend audit and is not claimed as passed here.

## ts-rust compatibility status

This frontend currently uses ordinary `typescript@5.9.2`; it contains no `tsc-rs` dependency, Rust/WASM application module or compatibility check. `NodeNext` module resolution and `react-jsx` are configured, with `.js` import specifiers in TypeScript source. These are the actual features that must pass when adopting the requested compiler.

The inspected [pingdotgg/ts-rust snapshot `d8fe7823432d360859cdd4ddc13f9a21f40c9846`](https://github.com/pingdotgg/ts-rust/tree/d8fe7823432d360859cdd4ddc13f9a21f40c9846) describes `tsc-rs` as a Rust implementation of the TypeScript compiler/checker, pinned to a TypeScript 7.1 development revision. Its published compatibility claims concern that upstream pin, not this project's TypeScript 5.9.2 baseline. [Compiler role and pin](https://github.com/pingdotgg/ts-rust/blob/d8fe7823432d360859cdd4ddc13f9a21f40c9846/README.md#L49). **No ts-rust execution or diagnostic comparison was performed in this reuse audit.**

Keep the proven baseline while adding a separately pinned `tsc-rs` check. Compare project inclusion, JSX/React declaration handling, NodeNext resolution, diagnostics and emit/test behavior; explain compiler-version differences separately from Rust-port defects. Continue using the bundler for the web application. The compiler's implementation language does not itself turn the React app into Rust/WASM or modernize Flash assets. Do not switch the only merge gate based on an upstream compatibility claim.

## Minimal coherent reuse set

Import the following as a **testable starter library**, maintaining its source origin. This closes all source imports used by the seven current frontend tests without copying the synthetic browser entrypoint:

```text
frontend/package.json
frontend/package-lock.json
frontend/tsconfig.json
frontend/src/StarterShell.tsx
frontend/src/themeManifest.ts
frontend/src/viewModel.ts
frontend/src/localization/index.ts
frontend/src/home/viewModel.ts
frontend/src/account/viewModel.ts
frontend/src/account/AccountView.tsx
frontend/src/profile/viewModel.ts
frontend/src/profile/ProfileView.tsx
frontend/src/community/viewModel.ts
frontend/src/community/CommunityView.tsx
frontend/tests/contracts.test.ts
frontend/tests/localization.test.ts
frontend/tests/visual-localization.test.ts
frontend/tests/home.test.ts
frontend/tests/account.test.ts
frontend/tests/profile.test.ts
frontend/tests/community.test.ts
```

For a browser app, add reviewed/adapted `index.html` and `vite.config.ts` plus a newly integrated entrypoint. Until that entrypoint exists, the imported library's `typecheck`/`test` commands are meaningful and its Vite `build` script must not be advertised as runnable. A pure shell/localization-only import can be smaller, but drops the independently tested profile/account/community/home contract work; the listed set is the coherent recommended salvage baseline.

Make the first adaptation a coordinated contract change: resolve duplicate `home.v1`, consolidate the shared profile DTO, strengthen/project external payload validation and separate fixture exports. Then connect a single backend-driven vertical slice before expanding routes/themes/widgets. Keep the existing tests alongside those adaptations and add tests that verify the resolved mismatch rather than preserve ambiguous tags.

## License and provenance

The source [LICENSE](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/LICENSE#L1) and frontend package both state **GPL-3.0-only** for newly authored implementation, while third-party source/assets retain their own terms. Preserve that attribution/license information and record the pinned source commit for reused files. Do not silently relabel the reused implementation as permissive or GPL-or-later. The source notice links the full GNU license; the delivery repository should include the applicable full license text and origin notices as part of integration.

This frontend has no media pack to import, so its successful build does not establish proprietary art rights. Retain dependency license notices when distributing the bundle and apply the separate per-file original asset manifest to new artwork/audio. Do not bulk-copy archived references, operational credentials/logs or unrelated ops files with the frontend salvage set.
