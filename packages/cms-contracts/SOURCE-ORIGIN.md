# Source origin and adaptations

Source: [PapaBill1234/phpretro-preservation at af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7](https://github.com/PapaBill1234/phpretro-preservation/tree/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/frontend). Source code notice: GPL-3.0-only. Preserve applicable notices and the full [repository license](../../LICENSE).

| This package | Source |
| --- | --- |
| `src/account/viewModel.ts`, `src/profile/viewModel.ts`, `src/profileFields.ts` | Source account/profile validators; shared profile shape extracted and projected once; IDs require safe positive integers |
| `src/community/viewModel.ts` | Source community validator; all output rows projected; group IDs require safe positive integers; fixtures moved to tests |
| `src/home/viewModel.ts` | Source user-home validator/presenter; output projected; IDs require safe positive integers |
| `src/siteShell.ts` | Source `src/viewModel.ts` presentation shell; renamed `site-shell.v1` to distinguish it from user-home `home.v1`; local navigation item validation added |
| `src/localization/index.ts` | Source fallback/catalogue helpers; shell type/version renamed; synthetic catalogues moved into tests |
| Account/profile/community/home/localization/locale-presentation tests | Source tests with React element assertions removed; imports and shell version renamed; their synthetic fixtures retained only in tests |
| `tests/siteShell.test.ts`, `tests/payloadBoundaries.test.ts`, `src/index.ts`, compiler comparison script/configuration | Newly authored integration and regression work for the identified source gaps |

No React views, `StarterShell` component, theme manifest, demo browser entrypoint, original artwork, API client, authenticated principal or token handling was imported. String/date/figure semantics, authorization and business validation remain backend responsibilities; these are structural contract helpers.

Development tools are exact npm pins: `tsc-rs@0.2.0`, `typescript@7.1.0-dev.20260929.1` and `@types/node@24.5.2`, with a generated lockfile. The selected TypeScript oracle matches the upstream revision named by the inspected ts-rust snapshot. Development-tool licenses remain their own; this package's adapted source is GPL-3.0-only.
