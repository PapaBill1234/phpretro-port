// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import type { CommunityViewModel } from "../src/community/viewModel.js";

export const communityFixture: CommunityViewModel = {
  version: "community.v1",
  rooms: [{ id: 1, name: "Retro Lounge", description: "A synthetic public room.", owner: "community" }],
  discussions: [{ id: 1, title: "Welcome discussion", groupId: 1 }],
  news: [{ id: 1, title: "Community update", summary: "A synthetic update.", headerImage: "", date: "2026-01-01" }],
  tags: [{ tag: "retro", quantity: 1 }]
};

export const emptyCommunityFixture: CommunityViewModel = { version: "community.v1", rooms: [], discussions: [], news: [], tags: [] };
