// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

export type ProfileFields = {
  id: number;
  username: string;
  motto: string;
  look: string;
  gender: "M" | "F";
  accountCreated: string;
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isPositiveId(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

export function acceptProfileFields(value: unknown): ProfileFields {
  if (!isRecord(value) || !isPositiveId(value.id) ||
      typeof value.username !== "string" || typeof value.motto !== "string" ||
      typeof value.look !== "string" || (value.gender !== "M" && value.gender !== "F") ||
      typeof value.accountCreated !== "string") {
    throw new Error("malformed profile fields");
  }
  return {
    id: value.id,
    username: value.username,
    motto: value.motto,
    look: value.look,
    gender: value.gender,
    accountCreated: value.accountCreated
  };
}
