// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { strict as assert } from "node:assert";
import test from "node:test";
import { createLocalizedSiteShellViewModel } from "../src/localization/index.js";
import { syntheticCatalogs } from "./localizationFixtures.js";
import { acceptSiteShellViewModel } from "../src/siteShell.js";

const expectedHomeKeys = ["version", "locale", "siteTitle", "welcome", "navigation"];

test("renders deterministic English and French presentation states", () => {
  const english = createLocalizedSiteShellViewModel(syntheticCatalogs, "en-US");
  const french = createLocalizedSiteShellViewModel(syntheticCatalogs, "fr-FR");

  assert.equal(english.locale, "en");
  assert.equal(french.locale, "fr");
  assert.equal(english.siteTitle, "PHPRetro");
  assert.equal(french.siteTitle, "PHPRetro");
  assert.notEqual(english.welcome, french.welcome);
  assert.notEqual(english.navigation[0]?.label, french.navigation[0]?.label);
});

test("uses a deterministic English fallback label for unknown locale casing", () => {
  const first = createLocalizedSiteShellViewModel(syntheticCatalogs, "zz-ZZ");
  const second = createLocalizedSiteShellViewModel(syntheticCatalogs, "ZZ-zz");

  assert.deepEqual(second, first);
  assert.equal(first.locale, "en");
  assert.equal(first.welcome, "Synthetic starter view");
  assert.deepEqual(first.navigation, [{ label: "Home", href: "/" }]);
});

test("preserves the typed site-shell.v1 output shape across display states", () => {
  for (const locale of ["en-US", "fr-FR", "unknown"]) {
    const model = createLocalizedSiteShellViewModel(syntheticCatalogs, locale);
    assert.deepEqual(Object.keys(model), expectedHomeKeys);
    assert.equal(model.version, "site-shell.v1");
    assert.equal(typeof model.locale, "string");
    assert.equal(typeof model.siteTitle, "string");
    assert.equal(typeof model.welcome, "string");
    assert.equal(model.navigation.length, 1);
    assert.equal(model.navigation[0]?.href, "/");
    assert.doesNotThrow(() => acceptSiteShellViewModel(model));
  }
});

test("keeps navigation stable while locale display strings vary", () => {
  const states = [
    createLocalizedSiteShellViewModel(syntheticCatalogs, "en-US"),
    createLocalizedSiteShellViewModel(syntheticCatalogs, "fr-FR"),
    createLocalizedSiteShellViewModel(syntheticCatalogs, "de-DE")
  ];

  assert.deepEqual(states.map((state) => state.navigation), [
    [{ label: "Home", href: "/" }],
    [{ label: "Accueil", href: "/" }],
    [{ label: "Home", href: "/" }]
  ]);
  assert.notEqual(states[0]?.welcome, states[1]?.welcome);
});

test("contains no route, auth, session, persistence, or capability fields", () => {
  const model = createLocalizedSiteShellViewModel(syntheticCatalogs, "fr-FR");
  const forbiddenFields = [
    "route", "routes", "auth", "authentication", "session", "persistence",
    "storage", "capability", "capabilities", "token", "user", "permission"
  ];

  assert.deepEqual(Object.keys(model), expectedHomeKeys);
  for (const field of forbiddenFields) {
    assert.equal(Object.hasOwn(model, field), false, `unexpected field: ${field}`);
  }
  assert.deepEqual(Object.keys(model.navigation[0] ?? {}), ["label", "href"]);
});
