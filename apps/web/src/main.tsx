// SPDX-License-Identifier: GPL-3.0-only
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { siteShell } from "@phpretro-port/cms-contracts";
import { messages } from "./locales.js";
import { loadSite, resolveSiteLocale, resolveTheme, type LocaleId, type SiteResult, type ThemeId } from "./site.js";
import "./styles.css";

function Neighborhood({ label }: { label: string }) {
  return <div className="neighborhood" role="img" aria-label={label}>
    <div className="sun" /><div className="cloud cloud-one" /><div className="cloud cloud-two" />
    <div className="city-ground" /><div className="building building-blue"><span /><span /><span /><span /><span /><span /></div>
    <div className="building building-orange"><span /><span /><span /><span /></div>
    <div className="building building-pink"><span /><span /><span /><span /><span /><span /></div>
    <div className="tree tree-one" /><div className="tree tree-two" /><div className="path" />
    <div className="person person-one" /><div className="person person-two" /><div className="person person-three" />
    <span className="spark spark-one">✦</span><span className="spark spark-two">✦</span>
  </div>;
}

function App() {
  const [result, setResult] = useState<SiteResult | undefined>();
  const [attempt, setAttempt] = useState(0);
  const [previewTheme, setPreviewTheme] = useState<ThemeId>();
  const [previewLocale, setPreviewLocale] = useState<LocaleId>();
  const settings = result?.status === "available" ? result.settings : undefined;
  const theme = previewTheme ?? resolveTheme(settings?.activeTheme);
  const locale = resolveSiteLocale(previewLocale, settings);
  const t = messages[locale];
  const shell = siteShell.acceptSiteShellViewModel({ version: "site-shell.v1", siteTitle: settings?.siteName ?? "PHPRetro Port", welcome: t.title,
    navigation: [{ label: t.explore, href: "/#features" }, { label: t.project, href: "/#project" }] });

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setResult(undefined);
    const timeout = setTimeout(() => controller.abort(), 5000);
    void loadSite(fetch, controller.signal).then((next) => { if (active) setResult(next); }).finally(() => clearTimeout(timeout));
    return () => { active = false; clearTimeout(timeout); controller.abort(); };
  }, [attempt]);
  useEffect(() => { document.documentElement.lang = locale; document.title = shell.siteTitle; }, [locale, shell.siteTitle]);

  return <div className={`site theme-${theme}`}>
    <a className="skip-link" href="#main">{t.explore}</a>
    <div className="preview-bar"><div className="container preview-inner">
      <span className="preview-tag">{t.preview}</span><span className="preview-note">{t.previewNote}</span>
      <label>{t.theme}<select value={theme} onChange={(event) => setPreviewTheme(event.target.value as ThemeId)}><option value="modern">{t.modern}</option><option value="legacy">{t.legacy}</option></select></label>
      <label>{t.language}<select value={locale} onChange={(event) => setPreviewLocale(event.target.value as LocaleId)}>
        {(!settings || settings.enabledLocales.includes("en")) && <option value="en">English</option>}
        {(!settings || settings.enabledLocales.includes("nl")) && <option value="nl">Nederlands</option>}
      </select></label>
    </div></div>
    <header className="site-header"><div className="container header-inner">
      <a className="brand" href="/" aria-label={shell.siteTitle}><span className="brand-mark" aria-hidden="true">P</span><span>{shell.siteTitle}<small>COMMUNITY HOTEL</small></span></a>
      <nav aria-label={locale === "nl" ? "Hoofdnavigatie" : "Main navigation"}>{shell.navigation.map((item) => <a key={item.href} href={item.href}>{item.label}</a>)}</nav>
      <span className="build-badge">{t.preview}</span>
    </div></header>
    <main id="main">
      <section className="hero"><div className="container hero-inner"><div className="hero-copy">
        <p className="eyebrow"><span />{t.eyebrow}</p><h1>{shell.welcome}</h1><p className="hero-description">{t.intro}</p>
        <a className="button" href="#project">{t.cta}<span aria-hidden="true">↗</span></a>
        <button className="text-button" onClick={() => setPreviewTheme(theme === "modern" ? "legacy" : "modern")}>{theme === "modern" ? t.secondary : t.modern}<span aria-hidden="true">→</span></button>
      </div><Neighborhood label={t.artLabel} /></div></section>
      <div className="container status-wrap">
        {!result ? <p className="service-message" role="status">{t.loading}</p> : result.status === "unavailable" ?
          <div className="service-message service-error" role="alert"><span>{t.offline}</span><button onClick={() => setAttempt((value) => value + 1)}>{t.retry}</button></div> :
          <p className={`service-message ${settings?.health === "degraded" ? "service-degraded" : "service-ok"}`} role="status"><span className="status-dot" />{settings?.health === "degraded" ? t.degraded : t.connected}</p>}
      </div>
      <section id="features" className="features container"><div className="section-intro"><span className="overline">{t.explore}</span><h2>{t.foundation}</h2></div><div className="feature-grid">
        {[[t.card1,t.text1,"conversation"],[t.card2,t.text2,"room"],[t.card3,t.text3,"blocks"]].map(([title,text,icon]) => <article className="feature-card" key={icon}><div className={`feature-symbol symbol-${icon}`} aria-hidden="true"><i /><i /><i /></div><h3>{title}</h3><p>{text}</p></article>)}
      </div></section>
      <section id="project" className="project container"><div><span className="overline">{t.project}</span><h2>{shell.siteTitle}</h2><p>{t.foundationText}</p></div><div className="project-pattern" aria-hidden="true"><i /><i /><i /><i /></div></section>
    </main>
    <footer className="container footer"><span>{shell.siteTitle} · {t.original}</span><span>{t.footer}</span></footer>
  </div>;
}

const root = document.getElementById("root");
if (!root) throw new Error("missing root element");
createRoot(root).render(<StrictMode><App /></StrictMode>);
