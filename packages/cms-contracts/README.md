# CMS contracts starter library

Pure TypeScript validators and locale helpers adapted from a pinned existing implementation. This package implements no website, theme, backend API, game client, account session or Flash widget. Rust is used by the `tsc-rs` development compiler; the exported library is JavaScript plus TypeScript declarations.

Install reproducibly and verify from this directory:

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run check
```

`check` verifies both expected compiler version strings and compares the same project's diagnostics, then compiles/runs the contract tests with `tsc-rs` and builds the library with declarations. `tsc-rs@0.2.0` reports `7.1.0-dev`; the corresponding pinned `typescript@7.1.0-dev.20260929.1` oracle includes its dated suffix. This checks this small library only; it makes no broad React/JSX or compiler-parity claim. Emitted tests go to `.test-build/`; library outputs go to `dist/`.

The exported modules are `account`, `profile`, `community`, `home`, `siteShell` and `localization`. The domain home retains `home.v1`; the presentation shell uses `site-shell.v1`, resolving two incompatible payloads that shared a tag upstream. Local shell navigation accepts root-relative paths and rejects external/protocol-relative/control-character targets. Synthetic message catalogues and content fixtures are test-only.

These validators project allowed fields and reject malformed structural payloads. They do not validate every business semantic, protect a private-home record already sent to a browser, authorize a user, sanitize arbitrary rich HTML, or prove backend contract agreement. In particular, date/figure/string lengths and full localization/key schemas need the later shared API specification. Private-home visibility must be enforced by Go before returning data.

Source origin, license and changes are in [SOURCE-ORIGIN.md](SOURCE-ORIGIN.md). The prior complete frontend's independent TypeScript 5.9.2/Vite verification and this library's reuse scope are recorded in [frontend-reuse.md](../../docs/frontend-reuse.md).

Fresh verification on 2026-10-11, Linux/Node 26.10.0/npm 11.19.1: lifecycle-disabled `npm ci` passed; `npm run check` passed with identical empty project diagnostics from both pinned compilers, **32 tests passed / 0 failed**, and the Rust compiler emitted the library's JavaScript/declarations. A runtime import of the built ESM entrypoint verified all six module exports and the distinct shell/home version tags. These results cover this library, not either website theme or the original React app under ts-rust.
