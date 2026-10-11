// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { strict as assert } from "node:assert";
import test from "node:test";
import { acceptHomeViewModel, presentHome } from "../src/home/viewModel.js";

const record = { ownerId: 7, username: "synthetic-user", showHome: true, publicContent: "public" };

test("reads a public home by synthetic name-equivalent data", () => {
  const view = presentHome(record);
  assert.deepEqual(view, { version: "home.v1", ownerId: 7, username: "synthetic-user", publicContent: "public" });
});

test("owner sees private home fields while another viewer cannot", () => {
  const hidden = { ...record, showHome: false };
  assert.equal(presentHome(hidden, { ownerId: 7 }).publicContent, "public");
  assert.throws(() => presentHome(hidden, { ownerId: 8 }), /unsupported/);
});

test("rejects malformed records and view models", () => {
  assert.throws(() => presentHome({ ...record, ownerId: 0 }), /not found/);
  assert.throws(() => acceptHomeViewModel({ version: "home.v1", ownerId: 1.5, username: "u", publicContent: "p" }), /malformed/);
  assert.throws(() => acceptHomeViewModel({ version: "home.v1", ownerId: 7, username: "u" }), /malformed/);
  assert.throws(() => acceptHomeViewModel({ version: "home.v2" }), /unsupported/);
});
