# CMS theme contract v1

Status: proposed contract for implementation. Two first-party packages, `classic` and `modern`, share the same Go API and React application controllers. Classic preserves the old PHPRetro website structure; modern reproduces the agreed current-site layout through original implementation and artwork. Design fidelity is measured against a dated route/viewport reference set, not an unbounded claim of 100% parity with a changing website.

## Boundary and package shape

The theme is a presentation package compiled with the application. It controls React layout/components, CSS design tokens, original assets and approved content slots. It never owns authentication, authorization, SQL, endpoint definitions, migrations, background jobs, business rules or game protocol behavior.

```text
packages/theme-contract/        # Versioned TypeScript types and test harness
packages/theme-classic/         # theme.json, entry.tsx, styles, assets, provenance.json
packages/theme-modern/
apps/web/src/controllers/      # Shared headless forms, routing and data loading
apps/web/src/theme/             # Registry, manifest validation, error boundaries
```

Themes imported from other open CMS projects are ported into this interface as source packages after license/security review. Their PHP/templates/scripts are not executed. No housekeeping upload of arbitrary JavaScript, npm installs, server templates or executable archives exists in v1. Adding a theme requires a normal reviewed build and deploy; housekeeping selects among already installed packages.

## Manifest ABI

```json
{
  "id": "classic",
  "name": "Classic",
  "version": "1.0.0",
  "contractVersion": "1.0",
  "apiVersion": "v1",
  "entry": "./entry.tsx",
  "routes": ["home", "login", "register", "me", "news.list", "news.detail", "community", "settings", "game.launch"],
  "slots": ["site.header", "site.footer", "home.hero", "home.news", "profile.sidebar"],
  "capabilities": ["news", "profile", "locale-picker", "game-launch"],
  "locales": ["en", "nl"],
  "assetManifest": "./provenance.json"
}
```

Manifest IDs follow a validated lowercase identifier format, cannot be paths, and must be unique. The build validates package version, contract/API version, required routes, component exports, locale catalogs, declared capabilities and asset provenance. Incompatible major ABI versions fail the build; optional capabilities permit an accessible shared fallback. Theme IDs and selections are data in CMS settings; they do not alter backend routing or tables.

Core owns the canonical routes, URLs, loaders, authentication guards, page titles and redirect validation. A theme declares which named page renderers it supplies; it cannot register privileged or conflicting routes. The example route list is illustrative: the complete PHPRetro feature inventory must map every retained route, including homes/groups/widgets where applicable, to typed route IDs and acceptance fixtures before the theme ABI freezes. Housekeeping uses the shared admin shell and form controllers with bounded visual tokens, so a public theme cannot hide or replace access checks.

## Components, DTOs and slots

```ts
// Types are generated/shared from the versioned public API contract.
type ThemeID = string;
type RouteID = 'home' | 'login' | 'register' | 'me' | 'news.list'
  | 'news.detail' | 'community' | 'settings' | 'game.launch';
type SlotID = 'site.header' | 'site.footer' | 'home.hero'
  | 'home.news' | 'profile.sidebar';
type Capability = 'news' | 'profile' | 'locale-picker' | 'game-launch';

interface ThemeContext {
  locale: string;
  dir: 'ltr' | 'rtl';
  t: Translate;
  links: CanonicalLinks;
  site: PublicSiteDTO;       // Public name, theme and language settings only
  viewer: ViewerDTO | null;  // Safe display fields; no session/token/hash
  enabled: ReadonlySet<Capability>;
}
interface LoginPageProps {
  context: ThemeContext;
  form: LoginFormController; // Shared submit, validation, busy/error state
}
interface HomePageProps {
  context: ThemeContext;
  data: HomePageDTO;         // Sanitized content and safe image/link metadata
}
interface ThemeModule {
  manifest: ThemeManifest;
  Shell: React.ComponentType<ShellProps>;
  pages: PageRendererMap;    // Per-route typed props, not Record<string, any>
  slots: Partial<SlotRendererMap>;
  tokens: ThemeDesignTokens;
}
```

Expand the illustrative `PageRendererMap`, `SlotRendererMap` and controller types into complete discriminated contracts before building the themes. Each route has a defined DTO and renderer; avoid a generic bag of arbitrary backend fields. Go exposes stable domain DTOs, not raw Polaris/CMS rows. OpenAPI generates frontend types and validation fixtures; the selected theme does not change their shape.

Shared controllers own submission, client-side validation, CSRF behavior, session recovery and navigation. Backend handlers enforce permission checks regardless of presentation. Rich content is sanitized at its trusted content boundary and represented by an approved renderer; themes do not call `dangerouslySetInnerHTML` on raw content. Links use validated protocols/origins. Themes cannot request emulator command scopes.

Slots receive typed context/data and render UI only. A missing optional slot falls back to a shared implementation. A required route missing its renderer is an install-time validation error. Do not let slots invent additional data-fetching or credential channels; any new capability requires a contract change with server tests.

## Selection, fallback and locale behavior

- Housekeeping sets hotel default theme, allowed themes, default locale and enabled locales through audited CMS APIs. User overrides are optional and must reference installed, permitted IDs. Precedence: permitted user choice, hotel default, bundled safe default.
- Use per-page error boundaries and a shared accessible error renderer. Unknown IDs, disabled themes or failed optional components produce deterministic fallback and a sanitized diagnostic; auth failures never turn into an anonymous success page.
- Publishing a theme/locale selection validates compatibility and locale coverage before saving. Cache settings in Redis with a version; invalidate after commit. A stale cached setting cannot authorize a forbidden operation.
- Translation keys belong to semantic UI actions/content, not one theme. Text in components uses `t(...)`; dates, currency and pluralization use locale-aware formatting. No language-specific routes or table names are introduced by themes.
- Fallback order: requested locale, configured base locale, bundled English. Missing keys produce a monitored development diagnostic; production fallback stays usable. Required authentication/admin keys must have complete enabled-locale coverage before release. Optional content translations may explicitly fall back and indicate their source language.
- Both themes must cover every enabled locale, long translations and RTL if Arabic is enabled. The CMS locale policy and Octane client locale bootstrap must be mapped explicitly; switching the CMS language alone does not prove the game client changed language.

## Asset provenance and Flash replacement

Every first-party asset has a provenance entry; original source files accompany generated output. Do not download, trace, extract or copy original Habbo/PHPRetro assets into the new theme packages. A visual reference may guide layout measurements, while artwork, logos, CSS and components are recreated independently. Track fonts/icons and their licenses too.

```json
{
  "assets": [{
    "path": "assets/welcome-scene.svg",
    "sha256": "<computed-at-build>",
    "origin": "original-project-artwork",
    "author": "project-art-team",
    "license": "<chosen-project-art-license>",
    "source": "art/welcome-scene.source.svg",
    "referencePurpose": "layout composition only"
  }]
}
```

Build rejects missing/invalid provenance entries, unexpected remote dependencies, unsafe SVG content and paths escaping the package. Treat the example license/hash as placeholders that must be resolved, never proof of provenance. Keep UI/client artwork and dependency license records separate from code licensing.

Inventory each PHPRetro SWF by role before replacement. Website effects/widgets become React + CSS/SVG/Canvas/WebAudio where appropriate; stateful tools become typed React components backed by Go APIs. Rust/WASM is used only when a measured workload or required format parser justifies it, behind a narrow tested interface. There is no general SWF-to-TypeScript conversion promise. Do not reconstruct the whole game client from a CMS SWF: Octane and its renderer cover that separate product surface.

Octane's existing custom-theme system is explicitly CSS-only and cannot implement this structural CMS ABI. Octane also includes at least one source-declared extraction of original game bitmaps. Adoption therefore requires a separate asset provenance inventory and any replacement work demanded by the original-assets requirement. [Octane CSS-only themes](https://github.com/duckietm/Octane/blob/67ec3ce1fd4d3f9d134c3b06ae137a2c49a313c1/custom-themes/README.md#L39), [Embedded bitmap origin](https://github.com/duckietm/Octane/blob/67ec3ce1fd4d3f9d134c3b06ae137a2c49a313c1/src/api/snowwar/SnowWarProjectileAssets.ts#L1). CMS themes may style the launcher frame but do not replace the game client's components/protocol.

## Acceptance tests

| Contract | Proof required |
|---|---|
| API neutrality | Same page fixtures, endpoints, authentication and permissions pass under classic and modern |
| Install validation | Unknown ABI, duplicate ID, missing required route, unsafe asset or incomplete required locale is rejected |
| Route/slot coverage | All canonical routes render; every optional slot fallback is tested with a missing renderer |
| Theme switching | Default/user choices persist; disabled/unknown IDs fall back; rollback works without a DB migration |
| Authorization | Direct API requests remain denied even if UI controls are shown/modified; themes receive no secrets |
| Locale switching | Housekeeping policy, user override, fallback, plurals, long strings and enabled RTL work in both themes |
| Accessibility | Keyboard operation, landmarks, labels, focus/error handling, contrast and reduced-motion settings pass |
| Visual parity | Named route/viewport snapshots match the dated approved references within documented tolerances, using original assets |
| Failure behavior | Page exception, failed asset load and stale settings render usable fallback with sanitized diagnostics |
| Asset provenance | Every served asset maps to verified origin/license/source and hashed output; no original extracted art slips through |
| Import example | A third small theme can be added using the package ABI without changes to Go domain code, API routes or database schema |

Maintain `docs/gaps.md` rows for missing routes, Flash replacements, parity differences, locale holes and asset provenance, each with evidence, owner, acceptance test and next action. A theme is ready only when the same functional contract passes and its remaining differences are explicitly accepted.
