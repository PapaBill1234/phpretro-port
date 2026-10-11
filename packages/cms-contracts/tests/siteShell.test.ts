// SPDX-License-Identifier: GPL-3.0-only
import { strict as assert } from "node:assert";
import test from "node:test";
import { acceptSiteShellViewModel } from "../src/siteShell.js";
import { acceptHomeViewModel } from "../src/home/viewModel.js";

const shell = {
  version: "site-shell.v1",
  siteTitle: "Synthetic site",
  welcome: "Welcome",
  navigation: [{ label: "Home", href: "/" }]
};

test("shell and user-home cannot be confused by their version tags", () => {
  assert.equal(acceptSiteShellViewModel(shell).version, "site-shell.v1");
  assert.throws(() => acceptHomeViewModel(shell), /unsupported/);
  assert.throws(() => acceptSiteShellViewModel({ version: "home.v1", ownerId: 1, username: "synthetic", publicContent: "public" }), /unsupported/);
});

test("navigation accepts local paths and rejects malformed or external targets", () => {
  assert.equal(acceptSiteShellViewModel({ ...shell, navigation: [{ label: "News", href: "/articles?page=2" }] }).navigation[0]?.href, "/articles?page=2");
  for (const href of ["javascript:alert(1)", "//outside.example", "/\\outside.example", "/\n/outside.example", "https://outside.example"]) {
    assert.throws(() => acceptSiteShellViewModel({ ...shell, navigation: [{ label: "link", href }] }), /malformed/);
  }
  assert.throws(() => acceptSiteShellViewModel({ ...shell, navigation: [7] }), /malformed/);
  assert.throws(() => acceptSiteShellViewModel({ ...shell, navigation: [{ href: "/" }] }), /malformed/);
});

test("shell projection removes extra fields without mutating source payload", () => {
  const input = { ...shell, internalNotes: "synthetic-internal", navigation: [{ ...shell.navigation[0], internalNotes: "synthetic-internal" }] };
  assert.deepEqual(acceptSiteShellViewModel(input), shell);
  assert.equal(input.internalNotes, "synthetic-internal");
});
