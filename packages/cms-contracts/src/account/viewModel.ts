// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { acceptProfileFields, isRecord, type ProfileFields } from "../profileFields.js";

export type AccountWidget = { name: string; value: string };
export type AccountViewModel = {
  version: "account.v1";
  profile: ProfileFields;
  status: string;
  widgets: AccountWidget[];
};

export function acceptAccountViewModel(value: unknown): AccountViewModel {
  if (!isRecord(value) || value.version !== "account.v1") {
    throw new Error("unsupported account view-model version");
  }
  if (typeof value.status !== "string") throw new Error("malformed account view-model");
  const profile = acceptProfileFields(value.profile);
  const widgets = value.widgets === undefined ? [] : value.widgets;
  if (!Array.isArray(widgets) || widgets.some((widget) => !isRecord(widget) ||
      typeof widget.name !== "string" || typeof widget.value !== "string")) {
    throw new Error("malformed account view-model");
  }
  return {
    version: "account.v1",
    profile,
    status: value.status,
    widgets: widgets.map((widget) => ({ name: widget.name as string, value: widget.value as string }))
  };
}
