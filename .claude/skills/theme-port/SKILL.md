---
name: theme-port
description: Port or add a React PHPRetro theme through the stable theme ABI, localization and original asset catalogue.
---

Read `docs/theme-contract.md` and the assigned route/state reference. Recreate layout and interactions in theme components consuming shared DTOs/controllers. Keep business behavior, permissions and ticket issuance outside the theme.

Match the pinned geometry/breakpoints/states; original art is reviewed against approved original art. Do not import upstream CSS/HTML/JS/fonts/sprites because the code repository is open source. Record provenance for every new art/audio/font item. Octane skins are a separate interface.

Capabilities select supported modules and shared fallbacks. A missing required capability rejects activation; missing optional slots fall back. Install reviewed source packages at build time, not arbitrary uploaded executable themes.

Validate translation keys/placeholders, long strings, plurals and RTL fixtures. Run the same route/DTO/accessibility fixtures for both legacy and modern themes. A theme change must not require a database or auth change; return a backend dependency if the DTO is insufficient.
