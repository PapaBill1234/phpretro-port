// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { strict as assert } from "node:assert";
import test from "node:test";
import { acceptCommunityViewModel } from "../src/community/viewModel.js";
import { communityFixture, emptyCommunityFixture } from "./communityFixtures.js";

test("accepts bounded community lists and tags", () => {
  const model = acceptCommunityViewModel(communityFixture);
  assert.equal(model.rooms.length, 1);
  assert.equal(model.tags[0].tag, "retro");
});

test("accepts a clean empty state", () => {
  assert.deepEqual(acceptCommunityViewModel(emptyCommunityFixture), emptyCommunityFixture);
});

test("rejects malformed and over-bounded payloads", () => {
  assert.throws(() => acceptCommunityViewModel({ ...communityFixture, rooms: [{ id: 0 }] }), /malformed/);
  assert.throws(() => acceptCommunityViewModel({ ...communityFixture, tags: Array.from({ length: 21 }, () => communityFixture.tags[0]) }), /malformed/);
  assert.throws(() => acceptCommunityViewModel({ ...communityFixture, version: "community.v2" }), /unsupported/);
});
