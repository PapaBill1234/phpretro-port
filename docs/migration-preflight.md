# Migration preflight

`migration-preflight` is a bounded, strictly read-only inventory planner. It accepts versioned JSON from stdin (or `-input path`), validates the shape and limits, and emits a sanitized JSON report. It never connects to MariaDB, imports records, mutates Polaris, or authorizes a cutover.

```sh
go run ./cmd/migration-preflight -input inventory.json > plan.json
cat inventory.json | go run ./cmd/migration-preflight
```

Exit status `2` means malformed/rejected input; status `3` means a valid inventory produced a blocked plan. Plans are always `report_only`; schema/collation verification remains an unresolved blocker even when caller-supplied facts claim verification. Explicit proposed target IDs and relationship decisions are preserved, while collisions, missing mappings, unsupported domains, invalid names, and orphan or unsupported relationships remain visible as blockers.

This slice does not provide authoritative MariaDB schema/collation reconciliation, credential migration, a database import/write path, staged checkpoints, crash recovery, backup/restore rehearsal, disposable-service evidence, or production cutover/delta handling. Those are required gates before any separately reviewed importer can be considered.
