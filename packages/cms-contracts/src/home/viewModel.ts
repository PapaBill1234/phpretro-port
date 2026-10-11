// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { isPositiveId, isRecord } from "../profileFields.js";

export type HomeRecord = {
  ownerId: number;
  username: string;
  showHome: boolean;
  publicContent: string;
};

export type HomeViewer = { ownerId?: number };

export type HomeViewModel = {
  version: "home.v1";
  ownerId: number;
  username: string;
  publicContent: string;
};

export function presentHome(record: HomeRecord, viewer: HomeViewer = {}): HomeViewModel {
  if (!isPositiveId(record.ownerId) || record.username.trim() === "") {
    throw new Error("home owner not found");
  }
  const isOwner = viewer.ownerId === record.ownerId;
  if (!record.showHome && !isOwner) {
    throw new Error("unsupported home state");
  }
  const model: HomeViewModel = {
    version: "home.v1",
    ownerId: record.ownerId,
    username: record.username,
    publicContent: record.publicContent
  };
  return { version: "home.v1", ownerId: model.ownerId, username: model.username, publicContent: model.publicContent };
}

export function acceptHomeViewModel(value: unknown): HomeViewModel {
  if (!isRecord(value) || value.version !== "home.v1") {
    throw new Error("unsupported view-model version");
  }
  const model = value as HomeViewModel;
  if (!isPositiveId(model.ownerId) || typeof model.username !== "string" ||
      typeof model.publicContent !== "string") {
    throw new Error("malformed home view-model");
  }
  return { version: "home.v1", ownerId: model.ownerId, username: model.username, publicContent: model.publicContent };
}
