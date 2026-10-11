// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { acceptProfileFields, isRecord, type ProfileFields } from "../profileFields.js";

export type ProfileViewModel = {
  version: "profile.v1";
  profile: ProfileFields;
  tab: 1 | 2 | 3 | 4 | 5;
};

function normalizeProfileTab(value: unknown): ProfileViewModel["tab"] {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 5
    ? value as ProfileViewModel["tab"]
    : 1;
}

export function acceptProfileViewModel(value: unknown): ProfileViewModel {
  if (!isRecord(value) || value.version !== "profile.v1") {
    throw new Error("unsupported profile view-model version");
  }
  return {
    version: "profile.v1",
    profile: acceptProfileFields(value.profile),
    tab: normalizeProfileTab(value.tab)
  };
}
