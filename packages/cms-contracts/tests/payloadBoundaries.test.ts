// SPDX-License-Identifier: GPL-3.0-only
import { strict as assert } from "node:assert";
import test from "node:test";
import { acceptAccountViewModel } from "../src/account/viewModel.js";
import { acceptProfileViewModel } from "../src/profile/viewModel.js";
import { acceptCommunityViewModel } from "../src/community/viewModel.js";
import { acceptHomeViewModel } from "../src/home/viewModel.js";
import { communityFixture } from "./communityFixtures.js";

const profile = { id: 7, username: "synthetic", motto: "fixture", look: "fixture-look", gender: "M", accountCreated: "2026-01-01T00:00:00Z" };

test("profile and account share safe ID boundaries", () => {
  for (const id of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => acceptProfileViewModel({ version: "profile.v1", profile: { ...profile, id } }), /malformed/);
    assert.throws(() => acceptAccountViewModel({ version: "account.v1", profile: { ...profile, id }, status: "fixture" }), /malformed/);
  }
});

test("accepted payloads project profile, widget and home fields", () => {
  const enrichedProfile = { ...profile, internalNotes: "synthetic-internal" };
  const account = acceptAccountViewModel({ version: "account.v1", profile: enrichedProfile, status: "fixture", widgets: [{ name: "label", value: "fixture", internalNotes: "synthetic-internal" }], internalNotes: "synthetic-internal" });
  assert.deepEqual(account.profile, profile);
  assert.deepEqual(account.widgets, [{ name: "label", value: "fixture" }]);
  assert.equal(Object.hasOwn(account, "internalNotes"), false);
  const home = { version: "home.v1", ownerId: 7, username: "synthetic", publicContent: "public" };
  assert.deepEqual(acceptHomeViewModel({ ...home, internalNotes: "synthetic-internal" }), home);
});

test("community projection drops extra room and tag fields and rejects invalid group IDs", () => {
  const input = { ...communityFixture, rooms: communityFixture.rooms.map((room) => ({ ...room, internalNotes: "synthetic-internal" })), tags: communityFixture.tags.map((tag) => ({ ...tag, internalNotes: "synthetic-internal" })) };
  assert.deepEqual(acceptCommunityViewModel(input), communityFixture);
  const accepted = acceptCommunityViewModel(input);
  assert.notEqual(accepted.rooms[0], input.rooms[0]);
  assert.notEqual(accepted.tags[0], input.tags[0]);
  for (const groupId of [0, -1, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => acceptCommunityViewModel({ ...communityFixture, discussions: [{ ...communityFixture.discussions[0], groupId }] }), /malformed/);
  }
});
