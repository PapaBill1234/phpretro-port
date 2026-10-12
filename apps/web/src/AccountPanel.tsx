// SPDX-License-Identifier: GPL-3.0-only
import { useEffect, useState, useSyncExternalStore, type FormEvent } from "react";
import type { Messages } from "./locales.js";
import { type AdminSiteSettings, type AuthController, type Failure, type FailureCode, type SiteDraft } from "./auth.js";
import type { LocaleId, ThemeId } from "./site.js";

const failureLabels: Record<FailureCode, keyof Messages> = {
  network: "accountOffline", invalid_response: "invalidResponse", invalid_request: "invalidSettings",
  invalid_credentials: "invalidCredentials", unauthenticated: "sessionExpired", forbidden: "accessDenied",
  invalid_origin: "originDenied", invalid_csrf: "csrfExpired", settings_conflict: "settingsConflict",
  rate_limited: "tooManyAttempts", dependency_unavailable: "accountOffline", http: "requestFailed"
};
function ErrorMessage({ failure, t }: { failure: Failure | null; t: Messages }) {
  return failure ? <p className="account-error" role="alert">{t[failureLabels[failure.code]]}</p> : null;
}

function SettingsEditor({ controller, settings, t, onSaved }: {
  controller: AuthController; settings: AdminSiteSettings; t: Messages; onSaved: (settings: AdminSiteSettings) => void;
}) {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [draft, setDraft] = useState<SiteDraft>(() => ({ siteName: settings.siteName, activeTheme: settings.activeTheme,
    defaultLocale: settings.defaultLocale, enabledLocales: [...settings.enabledLocales] }));
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    setDraft({ siteName: settings.siteName, activeTheme: settings.activeTheme,
      defaultLocale: settings.defaultLocale, enabledLocales: [...settings.enabledLocales] });
  }, [settings]);
  function change(update: Partial<SiteDraft>) { setSaved(false); setDraft((previous) => ({ ...previous, ...update })); }
  function toggleLocale(locale: LocaleId) {
    const enabled = draft.enabledLocales.includes(locale) ? draft.enabledLocales.filter((value) => value !== locale) : [...draft.enabledLocales, locale];
    if (!enabled.length) return;
    change({ enabledLocales: enabled, defaultLocale: enabled.includes(draft.defaultLocale) ? draft.defaultLocale : enabled[0] });
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setSaved(false);
    const result = await controller.saveSettings(draft);
    if (result) { setSaved(true); onSaved(result); }
  }
  return <section className="settings-panel" aria-labelledby="settings-heading">
    <div className="account-heading"><div><span className="overline">{t.housekeeping}</span><h3 id="settings-heading">{t.siteSettings}</h3></div>
      <span className="revision-label">{t.revision} {settings.settingsRevision}</span></div>
    <p className="account-note">{t.persistedNote}</p>
    <form onSubmit={(event) => { void submit(event); }}>
      <fieldset disabled={state.busy} className="account-fields settings-fields">
        <label className="wide-field">{t.siteName}<input value={draft.siteName} onChange={(event) => change({ siteName: event.target.value })} required maxLength={100} /></label>
        <label>{t.savedTheme}<select value={draft.activeTheme} onChange={(event) => change({ activeTheme: event.target.value as ThemeId })}>
          <option value="modern">{t.modern}</option><option value="legacy">{t.legacy}</option></select></label>
        <label>{t.defaultLanguage}<select value={draft.defaultLocale} onChange={(event) => change({ defaultLocale: event.target.value as LocaleId })}>
          {draft.enabledLocales.map((locale) => <option value={locale} key={locale}>{locale === "en" ? "English" : "Nederlands"}</option>)}</select></label>
        <div className="wide-field"><span className="field-label">{t.enabledLanguages}</span><div className="locale-options">
          {(["en", "nl"] as const).map((locale) => <label key={locale}><input type="checkbox" checked={draft.enabledLocales.includes(locale)}
            disabled={draft.enabledLocales.length === 1 && draft.enabledLocales.includes(locale)} onChange={() => toggleLocale(locale)} />{locale === "en" ? "English" : "Nederlands"}</label>)}
        </div></div>
        <div className="account-actions wide-field"><button className="button account-button" type="submit">{state.busy ? t.saving : t.saveSettings}</button>
          <button className="account-secondary" type="button" onClick={() => { setSaved(false); void controller.loadSettings(); }}>{t.reloadSettings}</button></div>
      </fieldset>
    </form>
    <ErrorMessage failure={state.siteError} t={t} />
    {saved && !state.siteError && <p className="account-success" role="status">{t.settingsSaved}</p>}
  </section>;
}

export function AccountPanel({ controller, t, onSaved }: {
  controller: AuthController; t: Messages; onSaved: (settings: AdminSiteSettings) => void;
}) {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submittedPassword = password, submittedOtp = otp;
    setPassword(""); setOtp("");
    await controller.login(username, submittedPassword, submittedOtp);
  }
  const user = state.session.status === "authenticated" ? state.session.user : undefined;
  return <section id="account" className="account-section container" aria-labelledby="account-heading">
    <div className="account-heading"><div><span className="overline">{t.account}</span><h2 id="account-heading">{user ? t.welcomeBack : t.signIn}</h2></div>
      <button type="button" className="account-secondary" disabled={state.busy} onClick={() => { void controller.refreshSession(); }}>{t.refreshSession}</button></div>
    {state.session.status === "loading" ? <p className="account-note" role="status">{t.sessionLoading}</p> : user ?
      <div className="account-summary"><p><strong>{user.username}</strong><span className="role-badge">{user.role === "admin" ? t.administrator : t.member}</span></p>
        <button className="account-secondary" disabled={state.busy} onClick={() => { void controller.logout(); }}>{state.busy ? t.working : t.signOut}</button></div> :
      <>
        <p className="account-note">{t.existingAccountNote}</p>
        <form onSubmit={(event) => { void login(event); }}>
          <fieldset className="account-fields login-fields" disabled={state.busy}>
            <label>{t.username}<input name="username" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required maxLength={25} /></label>
            <label>{t.password}<input name="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
            <label>{t.authenticatorCode}<input name="otp" type="text" autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6}
              value={otp} onChange={(event) => setOtp(event.target.value)} /><small>{t.staffCodeNote}</small></label>
            <button className="button account-button" type="submit">{state.busy ? t.working : t.signIn}</button>
          </fieldset>
        </form>
      </>}
    <ErrorMessage failure={state.error} t={t} />
    {user?.role === "admin" && (state.site ? <SettingsEditor controller={controller} settings={state.site} t={t} onSaved={onSaved} /> :
      <div className="settings-panel"><h3>{t.siteSettings}</h3><p className="account-note">{state.busy ? t.settingsLoading : t.settingsUnavailable}</p>
        <ErrorMessage failure={state.siteError} t={t} />
        <button type="button" className="account-secondary" disabled={state.busy} onClick={() => { void controller.refreshSession(); }}>{t.refreshSession}</button></div>)}
  </section>;
}
