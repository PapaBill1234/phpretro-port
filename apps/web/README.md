# Web foundation

React/TypeScript with original CSS and geometric artwork. The modern and classic skins use one data/controller implementation and the same localized content. This is a running foundation, not completed Habbo/PHPRetro theme or feature parity.

The app reads `GET /api/v1/site` (`site.v1`): site name, active theme, default/enabled languages, settings revision and service health. API failure displays an unavailable message with retry; no fake user/session/hotel ticket is supplied. A degraded response remains visibly degraded. Preview theme/language controls affect the current browser view only; authenticated housekeeping mutations remain unimplemented.

From the repository root, first build `packages/cms-contracts` with its locked dependencies. Then, in this directory:

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run check
npm run build
npm run dev
```

Vite proxies `/api` to local port 8080. The Dockerfile uses repository-root build context, builds the local contracts package and app, and serves static files via nginx with `/api` proxied to `cms:8080`. Root Compose binds the web service to local port 8789.

Exact compiler pins are `tsc-rs@0.2.0` and the corresponding `typescript@7.1.0-dev.20260929.1` oracle. Both check the React JSX/NodeNext project. Tests cover API projection, malformed/unavailable/degraded responses, theme fallback and English/Dutch locale fallback. Production build uses Vite; Rust is the compiler implementation, not the application language.

No images, fonts, CSS or media were copied from Habbo or PHPRetro. All decorative artwork is newly authored CSS geometry; it is a starter visual direction, not the finished original avatar/game resource pack. Account registration, authentication, hotel access, housekeeping, news/social features and measured visual parity remain tracked gaps.

Fresh local verification on 2026-10-11: lifecycle-disabled locked install passed; both pinned compilers passed the React JSX/NodeNext project; **5 contract/controller tests passed**; Vite production build passed (38 modules, 12.86 kB CSS and 198.14 kB JavaScript before gzip). Docker Compose/API integration and browser layout verification are separate checks performed by the integrator; these local results do not claim those checks have passed.
