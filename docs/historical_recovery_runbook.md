# Historical recovery runbook (DATA-CHASSIS-48)

Authority: `DATA-CHASSIS-48` / Issue #357, base `origin/main@5992fdb8ad3bcf1c0df2b60239dd528dec9936fc`,
expected branch `codex/data-chassis-48`, writer role `BUILD_PRODUCT`, `R3/CRITICAL`, `route_effect: COMPLETES`.
This document describes the executed procedure and its evidence. It does not authorize a new
workspace activation, schema change, provider change, or deletion of pre-existing evidence.

## Contracts

| Contract | Purpose | Location |
| --- | --- | --- |
| `historical-recovery-selection-v1` | Sealed, paginated, consumer-referenced historical selection | `src/investment_analyst/workspace/historical_recovery.py` |
| `historical-recovery-progress-v1` | Durable checkpoint bound to selection, source and destination | same |
| `historical-recovery-import-v1` | Import/replay receipt | same |
| `backup-reuse-receipt-v1` | Successor generation accounting, new vs reused bytes | `src/investment_analyst/workspace/backup_reuse.py` |
| `backup-parent-inspection-v1` | Compact parent generation identity | same |

The selection is read-only. It never opens v1 or the active v2 as a writer. The import uses exactly
one writer connection after the service is stopped. No SQL schema, financial identity, catalog entry
or provider endpoint changes.

## Selection rules

The universe is the 37 catalog assets of `catalog/default_assets.v1.json`. Thirty-one have source
data; the six BVL identities are contractual `N/A` for market and issuer fundamentals. The selection
is consumer-driven, not a bulk archive copy:

- all `day_1` observations for the 31 sourced assets, including revisions and original
  `available_at`/`observed_at`;
- all `annual` and `quarterly` issuer observations, including SEC company facts and the 13F
  institutional observation family;
- all `event` observations used by declared activity and derivatives consumers;
- every metric referenced by a persisted diagnostic, recursively through
  `input_metric_result_ids`, plus all `cazatiburones.*` artifacts whose weights and denominators
  need complete 13F position sets;
- all 13F position raw records for the managers present in the active v2 manager universe, with
  their reports, outcomes, semantics, correspondence and manager submissions;
- SEC primary/filer documents and all content-addressed document bytes for the supported issuers;
- the transitive raw closure of every selected observation.

`minute_1`, `hour_1`, FRED macro series outside the catalog, and non-referenced reproducible
metrics/diagnostics stay `ARCHIVE_ONLY`. They are recorded as exclusions, never silently dropped.

Each selected entity is classified `YA_PRESENTE`, `IMPORTAR_FALTANTE`, `RECONSTRUIR_DERIVADO`,
`ARCHIVE_ONLY`, `CONFLICTO` or `AUSENTE_ORIGEN`. A conflict stops the seal. An already present raw or
observation entity is accepted only when its persisted source identity matches; the v2 normalization
of timestamps is recorded as `source-identity-v2-normalization`, not treated as a content change.
The item `content_sha256` is the SHA-256 of the persisted v1 document; canonical model digests are
verified again at import and in the historical seal.

## Commands

Selection (read-only; new absolute output root; optional parent backup for the delta):

```bash
PYTHONPATH=src .venv/bin/python scripts/manage_historical_continuity.py recovery-select \
  --source-workspace "$V1" \
  --destination-workspace "$ACTIVE_V2" \
  --known-at 2026-10-22T22:00:20Z \
  --output-root "$OPS/data-chassis-48/selection-20261023" \
  --backup-parent "$OPS/data-chassis-47/v2-backup-20261022" \
  --reserve-bytes 5000000000
```

Import (exclusive writer; stop the local service first and restart immediately after):

```bash
systemctl --user stop investment-analyst.service
PYTHONPATH=src .venv/bin/python scripts/manage_historical_continuity.py recovery-import \
  --source-workspace "$V1" \
  --destination-workspace "$ACTIVE_V2" \
  --manifest "$OPS/data-chassis-48/selection-20261023/selection.json" \
  --budget "$OPS/data-chassis-48/selection-20261023/budget.json" \
  --checkpoint "$OPS/data-chassis-48/recovery-checkpoint.json"
systemctl --user start investment-analyst.service
```

Read-only status:

```bash
PYTHONPATH=src .venv/bin/python scripts/manage_historical_continuity.py recovery-status \
  --source-workspace "$V1" --destination-workspace "$ACTIVE_V2" \
  --manifest "$OPS/data-chassis-48/selection-20261023/selection.json" \
  --checkpoint "$OPS/data-chassis-48/recovery-checkpoint.json"
```

Successor backup with physical reuse of identical parent files, then a packed transport:

```bash
PYTHONPATH=src .venv/bin/python scripts/manage_workspace_v2_backup.py create-reuse \
  --source "$ACTIVE_V2" --destination "$OPS/data-chassis-48/v2-backup-20261023" \
  --parent "$OPS/data-chassis-47/v2-backup-20261022" \
  --reserve-bytes 5000000000 --receipt "$OPS/data-chassis-48/backup-reuse-receipt.json"

PYTHONPATH=src .venv/bin/python scripts/manage_workspace_v2_backup.py create-packed \
  --source "$OPS/data-chassis-48/v2-backup-20261023" \
  --destination "$OPS/data-chassis-48/v2-packed-20261023" \
  --reserve-bytes 5000000000 --writer-closed \
  --receipt "$OPS/data-chassis-48/packed-receipt.json"
```

Remote publication and one final functional restore are performed with the existing
`plan-remote`, `upload-remote`, `reconcile-remote` and `restore-remote` commands. The restore
destination is temporary and is removed only after its receipt is published.

## Budget and memory

The admission receipt measures real host `C:` and WSL free bytes, the selected payload, the bytes
that are already present, the import peak, the successor backup peak and the restore peak. The
default reserve is 5,000,000,000 B plus the conservative peak. A selection is written for evidence
even when not admitted; an import refuses to open a writer when admission fails. Import pages are
limited to 256 models and 16 MiB; CAS copies stream in 1 MiB chunks. Every reader/writer stays within
the existing memory, thread and temp limits; no limit is relaxed to force an operation.

## Ordering and rollback

1. Seal the selection and publish its budget/matrix before any destination write.
2. Stop the local service, import under the single writer, verify source identity and page digests,
   publish the historical seal, restart the service.
3. Verify destination counts, historical seal and read-only status.
4. Build the successor backup from the active v2; reuse identical files only from the immutable
   parent backup, never from the active DB/WAL. Verify the successor before publishing it.
5. Create the packed transport from the verified successor, upload it, and perform one bounded
   functional restore. Remove only the temporary restore after its receipt.
6. Retain the v1 source and the parent backup until the pre-existing retention list is explicitly
   authorized. Rollback is restore from the verified successor or the parent plus its unchanged
   files; no destructive workspace operation is part of rollback.

## Known limitations

- Alpaca/IEX is one-exchange coverage, not consolidated SIP.
- BVL native identities remain contractually `N/A` for market and issuer fundamentals; SMV
  registry evidence is retained without inventing a market.
- FRED macro series are not bound to a current catalog consumer and remain `ARCHIVE_ONLY`.
- External provider absences remain explicit; they are never converted to `N/A` or `PASS`.
- Historical v1 rows are preserved as `HISTORICAL`; they are never promoted to `LIVE` and no
  scheduler, queue, outbox, acknowledgement or preference state is imported.
- The selection does not materialize every reproducible v1 metric; current engines recompute the
  derivatives a current consumer needs.

## Executed evidence

The exact selection id, digest, page count, matrix, budget, import counters, backup reuse accounting,
remote restore receipt and post-deployment smoke are recorded in the DATA-CHASSIS-48 BUILD marker and
in `docs/data_chassis.md`. This runbook is the procedure; GitHub and the persisted receipts remain the
authoritative evidence.
