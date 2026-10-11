// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation frontend/src/viewModel.ts,
// commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7; shell version renamed to prevent a collision.

import { isRecord } from "./profileFields.js";

export type SiteShellViewModel = {
  version: "site-shell.v1";
  siteTitle: string;
  welcome: string;
  navigation: readonly { label: string; href: string }[];
};

function isLocalHref(value: unknown): value is string {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") &&
    !/[\\\u0000-\u0020\u007f]/.test(value);
}

export function acceptSiteShellViewModel(value: unknown): SiteShellViewModel {
  if (!isRecord(value) || value.version !== "site-shell.v1") {
    throw new Error("unsupported site-shell view-model version");
  }
  if (typeof value.siteTitle !== "string" || typeof value.welcome !== "string" ||
      !Array.isArray(value.navigation) || value.navigation.some((item) => !isRecord(item) ||
      typeof item.label !== "string" || !isLocalHref(item.href))) {
    throw new Error("malformed site-shell view-model");
  }
  return {
    version: "site-shell.v1",
    siteTitle: value.siteTitle,
    welcome: value.welcome,
    navigation: value.navigation.map((item) => ({ label: item.label as string, href: item.href as string }))
  };
}
