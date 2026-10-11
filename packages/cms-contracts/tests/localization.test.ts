// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation, commit af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.

import { strict as assert } from "node:assert";
import test from "node:test";
import {
  createLocalizedSiteShellViewModel,
  localeFallbackChain,
  missingKey,
  selectCatalog,
  translate,
  validateCatalog
} from "../src/localization/index.js";
import { syntheticCatalogs } from "./localizationFixtures.js";

test("validates the typed catalog shape and rejects extra or missing keys", () => {
  assert.equal(validateCatalog(syntheticCatalogs[0]), true);
  assert.equal(validateCatalog({ locale: "xx", messages: { siteTitle: "x", welcome: "x" } }), false);
  assert.equal(validateCatalog({ locale: "xx", messages: { siteTitle: "x", welcome: "x", navigationHome: "x", extra: "x" } }), false);

});

test("uses deterministic regional, language, then default fallback", () => {
  assert.deepEqual(localeFallbackChain("fr-CA"), ["fr-ca", "fr", "en"]);
  assert.equal(selectCatalog(syntheticCatalogs, "fr-CA")?.locale, "fr");
  assert.equal(selectCatalog(syntheticCatalogs, "de-DE")?.locale, "en");
});


test("makes missing keys explicit without hiding them", () => {
  assert.equal(missingKey("welcome"), "[missing:welcome]");
  assert.equal(translate(undefined, "welcome"), "[missing:welcome]");
  assert.throws(() => translate(undefined, "welcome", "throw"), /missing localization key/);
});

test("builds a localized site shell view model", () => {
  const model = createLocalizedSiteShellViewModel(syntheticCatalogs, "fr-CA");
  assert.equal(model.locale, "fr");
  assert.equal(model.welcome, "Vue de démarrage synthétique");
  assert.deepEqual(model.navigation, [{ label: "Accueil", href: "/" }]);
});
test("keeps the site-shell.v1 contract fixed across locale display states", () => {
  const english = createLocalizedSiteShellViewModel(syntheticCatalogs, "en-US");
  const french = createLocalizedSiteShellViewModel(syntheticCatalogs, "fr-FR");
  const expectedKeys = ["version", "locale", "siteTitle", "welcome", "navigation"];

  assert.deepEqual(Object.keys(english), expectedKeys);
  assert.deepEqual(Object.keys(french), expectedKeys);
  assert.equal(english.version, "site-shell.v1");
  assert.equal(french.version, "site-shell.v1");
  assert.equal(english.siteTitle, french.siteTitle);
  assert.notEqual(english.welcome, french.welcome);
  assert.notEqual(english.navigation[0]?.label, french.navigation[0]?.label);
  assert.equal(english.navigation[0]?.href, "/");
  assert.equal(french.navigation[0]?.href, "/");
});

test("uses the same deterministic default display state for unknown locales", () => {
  const first = createLocalizedSiteShellViewModel(syntheticCatalogs, "zz-ZZ");
  const second = createLocalizedSiteShellViewModel(syntheticCatalogs, "ZZ-zz");

  assert.deepEqual(second, first);
  assert.equal(first.locale, "en");
  assert.equal(first.version, "site-shell.v1");
  assert.deepEqual(first.navigation, [{ label: "Home", href: "/" }]);
});

test("preserves the typed presentation shape after fallback normalization", () => {
  const model = createLocalizedSiteShellViewModel(syntheticCatalogs, "FR-ca");
  assert.deepEqual(model, {
    version: "site-shell.v1",
    locale: "fr",
    siteTitle: "PHPRetro",
    welcome: "Vue de démarrage synthétique",
    navigation: [{ label: "Accueil", href: "/" }]
  });
});
