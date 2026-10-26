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
`ARCHIVE_ONLY`, `CONFLICTO` or `AUSENTE_ORIGEN`. A conflict stops the seal. An already present raw is
accepted only when its persisted source identity **and** its point-in-time timestamps
(`available_at`, `received_at`, `source.retrieved_at`) match the source. Equal identifiers or a
content checksum alone do not prove PIT equivalence: a temporal difference is `CONFLICTO`, never a
silent normalization and never an automatic rewrite of the destination. Newly imported raws retain
the exact source timestamps. The item `content_sha256` is the SHA-256 of the persisted v1 document;
canonical model digests are verified again at import and in the historical seal.

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

## R4 temporal repair of one SEC submissions discovery

R4 authorizes one audited exception to raw immutability: only `received_at`, `available_at` and
`source.retrieved_at` of discovery `4847c4d0-9531-58c1-bc6b-113e3d00f970`
(`sec-edgar:aapl:submissions`) may be restored to the v1 values. The document raw
`463601e9-0e3b-5256-b28e-69760b677851`, revision `1309de76-228f-5f1e-bd4b-4cdec9edf8c3`
(accession `0001140361-26-040126`) is the bounded lineage witness; its bytes are unchanged.

The repair is a typed storage operation, not a generic save:

- `RawV2TemporalStamp` and `RawV2TemporalRepair` bind the record, the prior checksum and the exact
  corrected checksum. `plan_temporal_repair` reads and verifies the current record without writing.
- `repair_temporal_metadata` writes the corrected content-addressed blob first, verifies it, and only
  then updates the single index row (path, checksum, `available_at`, `received_at`) in one
  transaction. An interrupted run leaves either the old or the new consistent pair; the old blob is
  retained. Applying the same authorization again returns `changed=false` without writing.
- `event_time`, payload, content checksum, record key, asset, schema and all other raws are untouched.

Executed evidence (workspace `70c64668-1738-4dfe-ba1f-7e72026bb7fe`):

| Item | Value |
| --- | --- |
| Spec | `ops/data-chassis-48/temporal-repair-spec-20261025.json`, SHA-256 `e3d9e3ae330ae4b9d1e84f50c1dd7469a92d50e2e6ae7ac7c727cc9097c60b2a` |
| Authorization | `ops/data-chassis-48/temporal-repair-authorization-20261025.json`, SHA-256 `9726ad456d858a3f659acf5936ec84d081079d76cf8ab72670f0d6ed7483e68d` |
| Receipt | `ops/data-chassis-48/temporal-repair-receipt-20261025.json`, SHA-256 `44d2714fa369e77ab6dd7ff910f723e162b74eae8a5f3c3b8523134b8a94cd02` |
| Before | `e6f127efcf25c3d5642e774ca1f999b040b1567e3fca1292888243a05f9da2a1` |
| After | `3fbd8a088dd167465ea9418ec24f9c1fb5255e0844f5750526c21c4108bc81f5` |
| Executor | `scripts/manage_historical_continuity.py` SHA-256 `3f92a3f0571fcaddd487fb00635cdd26d93f38c9b020c66bc3a6d3f7797d43a7` |
| Replay | `changed=false`, receipt bytes unchanged |

Post-repair checks: raw index count unchanged at 869,615; observations 202,933; historical metrics
41,661 and diagnostics 1,901; seal `14047/1881` and its digests unchanged; all six document revisions
that reference the discovery pass `SecDocumentRepository.verify_revision`, including the acquisition
causality `received_at=2026-10-20T18:53:29.313510Z <= revision.retrieved_at=2026-10-20T18:53:30.208310Z`.
The five sibling revisions were retrieved at `2026-10-21T20:49:46–48Z`, so they were already causally
valid with either timestamp; only the revision retrieved at `2026-10-20T18:53:30.208310Z` required
the correction. No other raw requires repair.

### Backup delta propagation and the packed-format blocker

The successor backup reuses the sealed unpacked parent `v2-backup-final-20261024c` and copies only
files whose size or SHA-256 differ. Lock files are excluded by the existing inventory rule, so the
physical change set is the 672,149,504-byte index, the 170,666-byte repaired blob and the successor
manifest. No sealed parent file is modified; unchanged files are hardlinked and re-verified.

Executed successor evidence:

| Item | Value |
| --- | --- |
| Successor | `ops/data-chassis-48/v2-backup-repair-20261025` |
| New backup id | `6d9df860-973c-596f-aaee-f8c916b1c225` |
| Parent backup id | `b57be147-4d54-5a1f-8993-e3d04afff9bb` |
| Parent manifest | `7908c9f79134a10f7c007ba0da5fbb9cfe9b578ad0f1467e90e52ec66121cb02` (unchanged after reuse) |
| Files | 870,445 total: 2 copied, 870,443 reused |
| Bytes | 4,067,273,091 total: 672,320,170 copied, 3,394,952,921 reused |
| Receipt | `ops/data-chassis-48/v2-backup-repair-20261025-receipt.json`; `parent_unchanged=true`, `verified=true` |

The active workspace, the successor and the sealed parent keep the same raw count (869,615),
observation count (202,933), metric count (41,661), diagnostic count (1,901) and historical seal
digests. The successor contains the repaired index and blob; the parent still contains the original
checksum `e6f127…`, proving the sealed base was not mutated.

The retained packed backup `v2-backup-final-20261024c-packed` cannot safely feed the existing
physical-delta index. Its `source-inventory.jsonl` includes `backup_manifest.json` because it was
packed from an unpacked backup directory, while `packed-backup-volumes.jsonl` intentionally omits
that control file. `WorkspaceV2PhysicalDeltaIndexService` correctly rejects the pair at
`volume-00000000` (`manifest.json` fragment vs `backup_manifest.json` inventory entry) with
`packed journal mapping differs from inventory`. This is a sealed-format limitation, not a
corruption: the packed restore already excludes the same control file and its file-level
verification is complete.

R4 permits the minimum control reconstruction instead of a full re-pack. The compatibility view
`ops/data-chassis-48/v2-backup-final-20261024c-packed-view` reuses every immutable sealed volume by
hardlink and writes only:

- a filtered `source-inventory.jsonl` that removes the single `backup_manifest.json` entry the journal
  intentionally omits (175,803,044 B, SHA-256 `7908c9f7…`), recomputing its count, bytes and digest;
- a small v3 manifest binding the filtered inventory (`ee8c46db…`, 870,444 files, 4,067,102,425 B),
  the unchanged journal (`0b53eef6…`, 486 volumes) and the same workspace id.

The sealed packed parent remains untouched (`431c59bf…`); the view is `157806cb…` with manifest
SHA-256 `07e23a4c…`, 486 hardlinked volumes and 486 inode matches. No file exceeds 1 GB. The
physical-delta index built from the view is `08b7845f…` with 870,444 files, 872,647 entries and
4,067,102,425 logical bytes.

Final delta and restore evidence:

| Item | Value |
| --- | --- |
| Epoch | `770a315a-e52f-4927-9b82-66595d18a01e` |
| Delta manifest | `ff305409a2ec2850855b6d3127ffe81c1ea8a14fec0a6a391a2b8a618cf0ef75` |
| Delta files seal | `300e40fce79e5b7bd8744dfa0f13212eba35f25c5c9f25811bbecf6ef213aef2` |
| Delta plan | 870,492 files; 568,185,469 new B in 585 objects; 3,516,107,740 reused B |
| Upload receipt | `ops/data-chassis-48/repair-delta-20261025/upload-receipt.json`; 870,492 confirmed; completed `2026-10-25T19:54:55Z` |
| Remote objects | `physical-delta/objects`, `physical-delta/controls`, `physical-delta/manifests`; each uploaded object independently read back and hashed |
| Local staging cleanup | `ops/data-chassis-48/repair-delta-20261025/local-staging-cleanup.json` SHA-256 `8cd289a7…`; the remote copy is authoritative |
| Retained parent | `.restore-final-20261024e.incomplete`, index `e583a403…`, byte-identical to sealed `c` |
| Restored root | `ops/data-chassis-48/v2-restore-repair-20261025` |
| Restore receipt | `ops/data-chassis-48/repair-delta-20261025/restore-receipt.json`; 870,492 files; completed `2026-10-25T21:30:02Z` |

The restore acquired the delta from the remote, verified every object against the new manifest, and
materialized a new isolated slot from the retained parent plus the remote delta. It did not patch
the retained `.incomplete` from the active workspace. Post-restore verification:

- `WorkspaceService.inspect` matches the active cut: 869,654 raw records, 203,049 observations,
  41,737 metric results, 1,902 diagnostic results, status `ready`, workspace id
  `70c64668-1738-4dfe-ba1f-7e72026bb7fe`.
- `CompactAnalyticalStore.verify_complete` verifies 41,737 metrics and 1,902 diagnostics; the
  historical seal remains 14,047 metrics / 1,881 diagnostics with the same digests and
  `source_fingerprint`.
- `verify_workspace_incremental_v2` reads 15,406 daily-evidence prefixes and 46,218 recursive
  checkpoints.
- The repaired discovery has `available_at=received_at=source.retrieved_at=2026-10-20T18:53:29.313510Z`,
  unchanged `event_time=2026-10-21T20:49:24.008711Z`, unchanged content checksum `c24afa8d…`; all
  six dependent document revisions pass `verify_revision`.

Residual operational risk: the final restore materialized 870,492 files and the host `C:` free
space fell to approximately 3.4 GB, below the 5 GB reserve. No file exceeds 1 GB (the restored
`storage/v2/index.duckdb` is 682,110,976 B). The local delta payload staging was removed after its
remote publication was verified; the retained evidence, backup `c`, packed `c`, remote `c`,
`.incomplete e`, its checkpoint and the successor backup remain. A host-side trim/cleanup is needed
before another large materialization.

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
