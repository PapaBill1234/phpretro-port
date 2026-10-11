// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { strict as assert } from "node:assert";
import test from "node:test";
import { acceptAccountViewModel } from "../src/account/viewModel.js";

const profile = {
  id: 7,
  username: "synthetic-user",
  motto: "test motto",
  look: "synthetic-look",
  gender: "M",
  accountCreated: "1970-01-01T00:01:40.000Z"
} as const;

const dashboard = {
  version: "account.v1",
  profile,
  status: "online",
  widgets: [{ name: "feed", value: "synthetic" }]
} as const;

test("accepts the complete account dashboard without dropping fields", () => {
  const model = acceptAccountViewModel(dashboard);
  assert.deepEqual(model.profile, profile);
  assert.equal(model.status, "online");
  assert.deepEqual(model.widgets, [{ name: "feed", value: "synthetic" }]);
});

test("normalizes absent optional widgets to an empty state", () => {
  const model = acceptAccountViewModel({ ...dashboard, widgets: undefined });
  assert.deepEqual(model.widgets, []);
});

test("preserves an explicit empty widget state", () => {
  const model = acceptAccountViewModel({ ...dashboard, widgets: [] });
  assert.deepEqual(model.widgets, []);
});

test("rejects malformed dashboard payloads", () => {
  assert.throws(() => acceptAccountViewModel({ version: "account.v1", profile, status: "online", widgets: [{ name: "feed" }] }), /malformed/);
  assert.throws(() => acceptAccountViewModel({ ...dashboard, version: "account.v2" }), /unsupported/);
  assert.throws(() => acceptAccountViewModel({ ...dashboard, profile: { ...profile, id: 0 } }), /malformed/);
  assert.throws(() => acceptAccountViewModel({ ...dashboard, widgets: "feed" }), /malformed/);
});
