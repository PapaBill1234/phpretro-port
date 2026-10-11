// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { isPositiveId } from "../profileFields.js";

export type CommunityRoom = {
  id: number;
  name: string;
  description: string;
  owner: string;
};

export type CommunityDiscussion = { id: number; title: string; groupId: number };

export type CommunityNews = {
  id: number;
  title: string;
  summary: string;
  headerImage: string;
  date: string;
};

export type CommunityTag = { tag: string; quantity: number };

export type CommunityViewModel = {
  version: "community.v1";
  rooms: CommunityRoom[];
  discussions: CommunityDiscussion[];
  news: CommunityNews[];
  tags: CommunityTag[];
};

const MAX_ITEMS = 20;

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function boundedArray(value: unknown, name: string): unknown[] {
  if (!Array.isArray(value) || value.length > MAX_ITEMS) throw new Error(`malformed community ${name}`);
  return value;
}

export function acceptCommunityViewModel(value: unknown): CommunityViewModel {
  if (!isRecord(value) || value.version !== "community.v1") {
    throw new Error("unsupported community view-model version");
  }
  const rooms = boundedArray(value.rooms, "rooms");
  const discussions = boundedArray(value.discussions, "discussions");
  const news = boundedArray(value.news, "news");
  const tags = boundedArray(value.tags, "tags");
  if (rooms.some((row) => !isRecord(row) || !isPositiveId(row.id) || typeof row.name !== "string" || typeof row.description !== "string" || typeof row.owner !== "string") ||
      discussions.some((row) => !isRecord(row) || !isPositiveId(row.id) || typeof row.title !== "string" || !isPositiveId(row.groupId)) ||
      news.some((row) => !isRecord(row) || !isPositiveId(row.id) || typeof row.title !== "string" || typeof row.summary !== "string" || typeof row.headerImage !== "string" || typeof row.date !== "string") ||
      tags.some((row) => !isRecord(row) || typeof row.tag !== "string" || typeof row.quantity !== "number" || !Number.isInteger(row.quantity) || row.quantity < 0)) {
    throw new Error("malformed community view-model");
  }
  return {
    version: "community.v1",
    rooms: rooms.map((row) => {
      const item = row as Record<string, unknown>;
      return { id: item.id as number, name: item.name as string, description: item.description as string, owner: item.owner as string };
    }),
    discussions: discussions.map((row) => {
      const item = row as Record<string, unknown>;
      return { id: item.id as number, title: item.title as string, groupId: item.groupId as number };
    }),
    news: news.map((row) => {
      const item = row as Record<string, unknown>;
      return { id: item.id as number, title: item.title as string, summary: item.summary as string, headerImage: item.headerImage as string, date: item.date as string };
    }),
    tags: tags.map((row) => {
      const item = row as Record<string, unknown>;
      return { tag: item.tag as string, quantity: item.quantity as number };
    })
  };
}

