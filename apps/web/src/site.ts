// SPDX-License-Identifier: GPL-3.0-only
import { localization } from "@phpretro-port/cms-contracts";

export const themeIds = ["modern", "legacy"] as const;
export const localeIds = ["en", "nl"] as const;
export type ThemeId = (typeof themeIds)[number];
export type LocaleId = (typeof localeIds)[number];
export type SiteSettings = {
  version: "site.v1"; siteName: string; activeTheme: string; defaultLocale: string;
  enabledLocales: string[]; settingsRevision: number; health: "ok" | "degraded";
};
export type SiteResult = { status: "available"; settings: SiteSettings } |
  { status: "unavailable"; reason: "network" | "http" | "invalid" };

// A public read started before a confirmed save must not restore an older revision.
export function mergeSiteResult(current: SiteResult | undefined, incoming: SiteResult): SiteResult {
  return current?.status === "available" && incoming.status === "available" &&
    current.settings.settingsRevision > incoming.settings.settingsRevision ? current : incoming;
}

export function resolveTheme(value: string | undefined): ThemeId {
  return value === "legacy" ? "legacy" : "modern";
}

export function resolveSiteLocale(requested: string | undefined, settings?: SiteSettings): LocaleId {
  const available = settings?.enabledLocales.filter((locale) => (localeIds as readonly string[]).includes(locale)) ?? [...localeIds];
  const resolved = localization.resolveLocale(requested ?? settings?.defaultLocale ?? "en", available, settings?.defaultLocale ?? "en");
  return resolved === "nl" ? "nl" : "en";
}

export function parseSiteSettings(value: unknown): SiteSettings {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid site settings");
  const row = value as Record<string, unknown>;
  if (row.version !== "site.v1" || typeof row.siteName !== "string" || !row.siteName.trim() || row.siteName.length > 100 ||
      typeof row.activeTheme !== "string" || typeof row.defaultLocale !== "string" || !Array.isArray(row.enabledLocales) ||
      row.enabledLocales.length === 0 || row.enabledLocales.length > 32 || row.enabledLocales.some((locale) => typeof locale !== "string") ||
      typeof row.settingsRevision !== "number" || !Number.isSafeInteger(row.settingsRevision) || row.settingsRevision < 1 ||
      (row.health !== "ok" && row.health !== "degraded")) throw new Error("invalid site settings");
  return {
    version: "site.v1", siteName: row.siteName, activeTheme: row.activeTheme, defaultLocale: row.defaultLocale,
    enabledLocales: [...row.enabledLocales] as string[], settingsRevision: row.settingsRevision, health: row.health
  };
}

export async function loadSite(fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<SiteResult> {
  let response: Response;
  try { response = await fetcher("/api/v1/site", { credentials: "same-origin", cache: "no-store", signal }); }
  catch { return { status: "unavailable", reason: "network" }; }
  if (!response.ok) return { status: "unavailable", reason: "http" };
  try { return { status: "available", settings: parseSiteSettings(await response.json()) }; }
  catch { return { status: "unavailable", reason: "invalid" }; }
}
