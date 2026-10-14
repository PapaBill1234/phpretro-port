# Eliza builder usage reporting

`../scripts/eliza-usage.py` is a private, offline, metadata-only reporter for Eliza
builder token and attempt usage. It reads trusted
`.builder/current-workers/<attempt>/` bundles and writes an allowlisted summary into
the private `.builder/eliza-usage/` tree. It is **not** CMSport, and it never reads
transcripts, keys, accounts, auth state, history or cloud/OAuth data.

## What it reads

For each attempt directory it reads two bounded, owned, regular, non-symlink files:

- `task.json` — trusted metadata; only `attempt_id`, the task id (aliased from
  `task_id`, `taskId` or `task`), `provider` and `model` are read, and any
  secret-looking field name is dropped.
- `result.private.json` — the raw result stream. Native Claude
  `--output-format json` interleaves CLI startup text, one complete JSON config frame
  (which may carry secrets), intervening diagnostic text and the final result value.
  A tolerant scanner accepts only complete JSON values that begin at the start of a
  line and end before trailing prose, so JSON embedded in a diagnostic or inside a
  quoted string is never mistaken for a top-level result, and the **last
  `type=result` value** is used. Codex bridge result files with `usage.last` /
  `usage.cumulative` are read using one snapshot only, so a `last` and `cumulative`
  pair is never double-counted. Model ids may carry a routing suffix such as
  `deepseek-flash[1m]`.

Reads are bounded by `--max-files`, `--max-file-bytes` and `--max-total-bytes`; unsafe
or oversized inputs are skipped by reason code.

## Output allowlist

Only these fields are ever emitted, per attempt:

`attempt` (hashed attempt id), `task_id`, `provider`, `model`, `result_subtype`,
`seconds`, `reported_turns`, `input_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`output_tokens`, `source_evidence` (source content hash). Missing accounting is
recorded as `null` (unknown), never as zero; boolean values are treated as unknown.

Never emitted: worker result text, errors, private paths, raw model-usage maps, session
or account identities, provider headers, keys or transcripts. Cache-read, cache-write,
input and output counters are aggregated separately; there is no "cached tokens are
free" assumption and no token-derived session-limit rating. When any attempt in a group
leaves a field unknown, the group `totals` entry stays `null`; a `known_totals` /
`known_counts` subtotal over known attempts is reported alongside it and rendered as
`partial` with its unknown count, never presented as the total. Costs stay unknown and
no savings are claimed.

Deduplication collapses identical copies of the same attempt (same attempt id and same
source content hash). Different attempt files remain separate charges even when a
resumed session id repeats.

## Costs and Doctor advice

Costs stay **unknown** unless the operator supplies a verified price table
(`--price-table`) whose entries carry `currency`, `unit`, `provider`, `model`, a
context-validity marker and explicit prices for every counter. No prices, unit
conversions or caps are fabricated.

Doctor recommendations are deterministic rules over observed evidence only
(repeated budget/turn stops, high cache-read context, no-progress attempts). This is
not freeform LLM inference and not a matched Jev experiment; `savings_measured` stays
`false` and no savings are claimed without a matched same-task, same-model comparison.

## Output and CLI

The CLI is offline: no network, model or worker dispatch. It writes
`summary.json` and `summary.md` with mode `0600` inside a `0700`, owned tree, and
prints only count/status metadata to stdout. `--print-summary` prints the already
sanitized allowlisted summary.

```
python3 scripts/eliza-usage.py --workers-dir .builder/current-workers \
    --output-dir .builder/eliza-usage
```

## Relationship to other tooling

This reporter is separate from native Doctor history and Codex usage reporting. It
does not aggregate Doctor output and does not claim that Doctor reports total session
usage or measured savings. Tests live in `../tests/ops/test_eliza_usage.py` and use
synthetic sandboxes only; a root reviewer later runs it over real private state.
