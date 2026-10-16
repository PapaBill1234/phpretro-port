# Offline OpenCode quota evidence helper

`scripts/opencode-quota.py` validates a manually transcribed OpenCode native
usage-page snapshot. OpenCode's usage-limit UI links to
`https://chatgpt.com/settings/usage` (migration plan source S8); the pinned V2
contract has no verified quota API (D4). This helper makes no network requests,
does not prove which browser account is open, and does not enforce or authorize
dispatch. The operator must explicitly attest that the browser account matches
the selected OpenCode connection and that all applicable reported windows are
included. A caller-supplied opaque binding must match the selected profile's
expected binding. This is an operator trust boundary, not programmatic identity
verification; opaque metadata is not semantically scanned for secrets, so the
operator must ensure bindings contain no credentials or personal data.

## Evidence schema v1

The top-level object has exactly these keys: `schema_version` (integer `1`),
`profile` (`primary` or `secondary`), `connection_binding` (opaque
`[A-Za-z0-9._:-]` identifier, 1–128 chars), `source`
(`manual_native_usage_page`), `operator_attested` (`true`), `observed_at`,
`verified_at`, and nonempty `windows`. Timestamps are ISO 8601 with an explicit
timezone. `observed_at` is when the page values were observed; `verified_at` is
when the operator confirmed them. Both must be no more than 300 seconds old at
validation, not future-dated, and observation cannot follow verification. The
300-second limit is a conservative offline policy candidate, not an approved
native guard or evidence-backed source freshness guarantee.

Each window has `id` (unique nonempty string, max 128 chars), `percentage`
(finite number 0–100), and `basis` (`used`, `remaining`, or `unspecified`), plus
optional `duration_minutes` (positive integer) and `reset_at` (zoned timestamp
or null/omitted). An unavailable allowance must be omitted, not represented as
zero; every actually reported applicable window must be supplied. Preserve the
reported ID and duration: 300- and 10080-minute windows are not daily
allowances. Reset time passing never proves replenishment. USD balance, token
counts, catalog pricing and Desktop Codex usage are not accepted as this source.

`used` is conservatively converted to `100 - percentage` remaining; `remaining`
uses its reported percentage. For `unspecified`, the lower bound is
`min(percentage, 100 - percentage)` and the report retains the original
percentage and ambiguous basis. At 10% or less conservative remaining in any
window, decision is `checkpoint` and dispatch is marked blocked. Invalid,
missing, wrong-profile/binding, stale or otherwise unverified evidence returns
`blocked`; only a complete fresh snapshot above the threshold returns the
validator's `allow` classification. No classification is wired to runtime
dispatch.

## Private checkpoint

`write_checkpoint` requires an already-existing absolute root with canonical
path spelling (no parent traversal or redundant separators), no symlink
components, current-user ownership and mode 0700. It writes only whitelisted
operator-supplied metadata, using a mode-0600 exclusive temporary file, fsync
and atomic replacement. Existing targets must be current-user-owned private
regular files; unsafe targets are rejected without replacement. These checks
are not an OS sandbox and do not promise protection against a malicious
same-user process racing path changes. Tests use synthetic temporary
directories only. Never put credentials, account names, transcripts, arbitrary
operator notes or task contents in the record; schema whitelisting cannot detect
sensitive text misused as an opaque binding. A saved checkpoint is not
resumable authority: resume requires a new manual observation and verification,
including after an apparent reset. No connection/account switch, reset credit
use, automatic reconciliation, or auto-resume occurs.

This closes only an offline evidence-validation slice; it is not live quota
measurement, an autonomous guard, proof of spend safety, nor closure of A10.
