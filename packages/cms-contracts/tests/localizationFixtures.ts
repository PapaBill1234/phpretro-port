// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import type { LocalizationCatalog } from "../src/localization/index.js";

export const syntheticCatalogs: readonly LocalizationCatalog[] = [
  {
    locale: "en",
    messages: {
      siteTitle: "PHPRetro",
      welcome: "Synthetic starter view",
      navigationHome: "Home"
    }
  },
  {
    locale: "fr",
    messages: {
      siteTitle: "PHPRetro",
      welcome: "Vue de démarrage synthétique",
      navigationHome: "Accueil"
    }
  }
];

