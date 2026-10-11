// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { strict as assert } from "node:assert";
import test from "node:test";
import { acceptProfileViewModel } from "../src/profile/viewModel.js";

const profile = {
  version: "profile.v1",
  profile: {
    id: 7,
    username: "retro",
    motto: "Keep it classic",
    look: "hd-180-1.ch-210-66.hr-100-0",
    gender: "F",
    accountCreated: "2020-01-02T03:04:05Z"
  },
  tab: 3
} as const;

test("accepts valid tabs and preserves all six profile fields", () => {
  const model = acceptProfileViewModel(profile);
  assert.equal(model.tab, 3);
  assert.deepEqual(model.profile, profile.profile);
});

test("defaults absent and invalid tabs to tab one", () => {
  for (const tab of [undefined, 0, 6, "2", 2.5, null]) {
    assert.equal(acceptProfileViewModel({ ...profile, tab }).tab, 1);
  }
  assert.equal(acceptProfileViewModel({ ...profile, tab: 1 }).tab, 1);
  assert.equal(acceptProfileViewModel({ ...profile, tab: 5 }).tab, 5);
});

test("rejects missing users and malformed profile state", () => {
  assert.throws(() => acceptProfileViewModel({ version: "profile.v1", tab: 1 }), /malformed/);
  assert.throws(() => acceptProfileViewModel({ ...profile, profile: undefined }), /malformed/);
  assert.throws(() => acceptProfileViewModel({ ...profile, profile: { ...profile.profile, gender: "X" } }), /malformed/);
  assert.throws(() => acceptProfileViewModel({ ...profile, profile: { ...profile.profile, id: 0 } }), /malformed/);
});

test("rejects unsupported versions without expanding capabilities", () => {
  assert.throws(() => acceptProfileViewModel({ ...profile, version: "profile.v2" }), /unsupported/);
  assert.deepEqual(Object.keys(acceptProfileViewModel(profile)), ["version", "profile", "tab"]);
});
