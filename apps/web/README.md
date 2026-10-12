# Web foundation and account slice

React/TypeScript with original CSS and geometric artwork. The modern and classic skins use one data/controller implementation and the same localized content. This is a running foundation, not completed Habbo/PHPRetro theme or feature parity.

The app reads `GET /api/v1/site` (`site.v1`): site name, active theme, default/enabled languages, settings revision and service health. API failure displays an unavailable message with retry; no fake user/session/hotel ticket is supplied. A degraded response remains visibly degraded. Preview theme/language controls affect the current browser view only; “Use saved settings” restores the API-selected presentation.

The account panel checks `GET /api/v1/auth/session`, signs into existing accounts with `POST /api/v1/auth/login` and optional staff TOTP, and confirms logout through `POST /api/v1/auth/logout`. A strict `session.v1` parser rejects extra fields, invalid IDs/roles and malformed tokens. The controller exposes only a safe principal; the opaque cookie stays HttpOnly and the CSRF value stays in a private, in-memory closure. Password and OTP form values are cleared when submitted. Requests use same-origin credentials, no-store caching, blocked redirects and a five-second timeout; no browser storage or token URL is used.

An authoritative administrator session loads `GET /api/v1/housekeeping/site`. Saving uses `PUT` with the exact `site-update.v1` payload, expected revision and `X-CSRF-Token`; only a strictly valid response with the next revision reports success and updates public settings. Browser previews remain separate. Conflict keeps the unsaved draft and offers an explicit reload. A 401 clears the principal; a 403 removes loaded privileged settings until the session is checked again. Network or invalid logout responses retain the known session and display recovery guidance. Guest/member controllers cannot call housekeeping endpoints; the backend remains responsible for every permission check.

From the repository root, first build `packages/cms-contracts` with its locked dependencies. Then, in this directory:

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run check
npm run build
npm run dev
```

Vite proxies `/api` to local port 8080. The Dockerfile uses repository-root build context, builds the local contracts package and app, and serves static files via nginx with `/api` proxied to `cms:8080`. Root Compose binds the web service to local port 8789.

Exact compiler pins are `tsc-rs@0.2.0` and the corresponding `typescript@7.1.0-dev.20260929.1` oracle. Both check the React JSX/NodeNext project. Tests cover strict auth/settings contracts, safe principal projection, route/body/CSRF behavior, privilege changes, conflict/reload, network/timeout/malformed responses, serialized requests, and actual React server rendering in English/Dutch. Public-site tests cover theme/locale fallback and availability. Production build uses Vite; Rust is the compiler implementation, not the application language.

No images, fonts, CSS or media were copied from Habbo or PHPRetro. All decorative artwork is newly authored CSS geometry; it is a starter visual direction, not the finished original avatar/game resource pack. Account registration, hotel access, the remaining housekeeping modules, news/social features and measured visual parity remain tracked gaps.

Fresh local verification on 2026-10-11: lifecycle-disabled locked install passed; both pinned compilers passed the React JSX/NodeNext project; **18 contract/controller/rendering tests passed**; Vite production build passed (40 modules, 16.79 kB CSS and 214.91 kB JavaScript before gzip). Controller tests use synthetic responses and do not prove database persistence. Docker Compose/API integration and browser verification are separate checks performed by the integrator; these local results do not claim those checks have passed.
