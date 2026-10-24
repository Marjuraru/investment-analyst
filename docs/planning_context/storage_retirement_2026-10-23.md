# Storage retention decision and concrete list (2026-10-23)

Authority: `DATA-CHASSIS-48` / Issue #357. This document is the unique, concrete list requested by
the user before importing. It records measured bytes, the retained coverage and the last known
consumer. **No pre-existing candidate below was deleted by BUILD.** Deletion of a pre-existing
candidate requires the user's literal confirmation; uncertainty is retained, never converted into
garbage.

## Already executed before this block (reuse as evidence, do not recreate)

| Path | Bytes | Event |
| --- | ---: | --- |
| `ops/data-chassis-47/continuity-archive-v1/chunks` | 11,818,823,680 | Authorized retirement 2026-10-23; remote `ia_drive_crypt:continuity-47/chunks` retains the cryptcheck |
| `ops/data-chassis-47/v2-restore-20261022` | 2,085,920,768 | Authorized retirement 2026-10-23; matched the retained backup manifest |
| `ops/data-chassis-47/.v2-restore-20261022.b32816b6046d4b868d8dcfac16ae6d0d.tmp` | 2,085,912,576 | Authorized retirement 2026-10-23; temporary restore residue |

These three events are not repeated. The retained backup remains
`ops/data-chassis-47/v2-backup-20261022` (`backup_id 6911fd4a-8043-53e7-b6fb-30ae6b7aa8a6`,
262,091 files, manifest SHA-256
`95ebcbbf873449a86522641d7969b40850cf3502119ce922e4115ce1f17212c5`). The remote archive manifest is
`bcdf1dd56f88cf928b6fe5690a757d7970930bdfb2a24c6abc13dbe5ff1b5b4f` (812 files /
11,817,006,975 B). It does **not** include the external v1 raw folder; that is why v1 cannot be
retired from the archive alone.

## Retained anchors

| Path | Measured bytes | Decision | Reason |
| --- | ---: | --- | --- |
| `workspaces/active-v2-20261021` | live workspace | KEEP_ACTIVE | Operating v2; never retired in this sequence |
| `workspaces/default` (v1) | 14,292,043,424 | KEEP_ACTIVE / RETIRE_AFTER | Source of this recovery and of the excluded raw archive; retire only after final external representation and restore are proven |
| `ops/data-chassis-47/v2-backup-20261022` | 1,313,667,875 payload | KEEP_ACTIVE until successor verified; then rotate | Parent generation for hardlink reuse; authorized rotation preserves the valid backup and receipt |
| `runtime/releases/097e4e3…` and `runtime/releases/0e93c6d…` | release trees | KEEP_ACTIVE | Active release and rollback; not deleted here |
| `.worktrees/**` protected worktrees | — | KEEP_ACTIVE | Protected local work; no deletion |
| Remote `ia_drive_crypt` objects | 11,817,006,975 archive + backup objects | KEEP_ACTIVE | Recovery and audit; no remote deletion without a concrete restore of the retained set |

## Pre-existing candidates — proposal only, no deletion performed

| Path | Measured bytes | Retained coverage / evidence | Last consumer | Proposed decision |
| --- | ---: | --- | --- | --- |
| `workspaces/data-chassis-43-rehearsal` (F5/rehearsal) | 6,703,729,201 | F5 is a partial rehearsal, not active v2; selection and successor backup cover the imported evidence | Historical rehearsal only | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/independent-reads-W-20261017` | 4,559,610,793 | Immutable read baseline; hashes and controls retained; duplicated by W2 control | Audit evidence | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/physical-delta-G3-20261021` | 2,372,855,347 | Independent read copy; compare with W2 | Audit evidence | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/physical-delta-W2-20261018` | 2,367,176,797 | `files.jsonl` SHA-256 `1854b18…`; duplicate control of independent-reads | Audit evidence | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/fresh-start-20261021` | 2,510,866,246 | Activation evidence for the current UUID; final successor backup covers the active cut | Activation / audit | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/recovery-W2-20261017` | 1,091,834,448 | Intermediate recovery; successor covers the final inventory | Migration evidence | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/physical-delta-base-20261015` | 600,983,336 | Baseline delta evidence | Migration evidence | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/prepare-W2-20261018` | 597,689,553 | Preparation evidence; no unique financial payload beyond the archived source | Migration evidence | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/retire-W2-20261019` | 596,673,457 | Retirement staging evidence | Migration evidence | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/physical-delta-W-20261017` | 353,986,799 | Duplicate/control delta | Audit evidence | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/physical-delta-pre-f-20261015` | 353,322,434 | Pre-freeze delta evidence | Migration evidence | RETIRE_AFTER (49), not now |
| `ops/data-chassis-45/cut-f*`, `cut-f1..f5` | 0.05–1.7 MB each | Frozen cuts and receipts | Audit evidence | ARCHIVE_ONLY; retain |
| `ops/data-chassis-45/f-complement-20261015` | 55,989,223 | Complement publication evidence | Audit evidence | ARCHIVE_ONLY; retain |
| `ops/data-chassis-45/cleanup-E0-20261018` | 52,860,243 | Cleanup receipt/inventory | Audit evidence | ARCHIVE_ONLY; retain |
| `ops/data-chassis-45/cleanup-regenerated-writer-caches-20261019` | 3,022,497 | Regenerated cache evidence | Audit evidence | ARCHIVE_ONLY; retain |
| `ops/data-chassis-45/reconcile-f4-20261021` | 16,116,579 | Reconciliation evidence | Audit evidence | ARCHIVE_ONLY; retain |
| `ops/data-chassis-44/drive-restore-staging` | small manifest/staging | Remote restore receipt; remote objects retained | Audit evidence | ARCHIVE_ONLY; retain |
| `ops/data-chassis-47/pilot-v2-20261022` and pilot receipts | small | Pilot replay/selection evidence; pilot is not the active workspace | Audit evidence | ARCHIVE_ONLY; retain |
| `ops/data-chassis-47/continuity-archive-v1` (manifest/receipt) | small | Archive manifest and remote cryptcheck | Audit evidence | KEEP_ACTIVE |
| `ops/data-chassis-45/PLAN-handoff-*`, `incident-*`, `*checkpoint*`, `*budget*` | small | Decision and incident evidence | Audit evidence | KEEP_ACTIVE |
| `ops/data-chassis-45/vhd-compaction-*` | small | Host/VHD diagnosis; no compaction authorized | Audit evidence | KEEP_ACTIVE |

No entry is `UNKNOWN` without a concrete pending test. The list does not claim any candidate was
physically reclaimed: `du`/`du -sb` report logical allocated bytes, and no VHD compaction is
performed or inferred.

## Block-created staging

The selection, import checkpoint, successor backup, packed transport, remote receipts and temporary
restore are created only by this block. Their exact paths and hashes are published with the BUILD
evidence. The block may remove its own failed scratch or temporary restore after the last consumer
receipt; it does not remove the selected manifest, successor backup, remote receipts, or any
pre-existing candidate. The final restore is deleted only after its receipt is published.

## v1 retirement event

`workspaces/default` is **not** retired here. It remains the source for excluded/raw evidence until
all of the following exist: a complete selected-set representation with hashes, an external archive
of the excluded/raw representation (including the external raw folder), a verified remote restore of
that representation, and a post-cut consumer check. The finite retirement event and any missing
check are recorded in the DATA-CHASSIS-49 list; this block does not extend that authorization.
