"""Typed analysis snapshot v2 staging table with verified references.

Part of DATA-CHASSIS-32.
Snapshots live in the same file-backed DuckDB index as the raw v2 staging
under the same writer lock, in their own ``analysis_snapshots_v2`` table with
ordered metric and diagnostic link tables.
No ``document_json`` column exists.
Reads rehydrate the strict AnalysisSnapshot model, confirming that every cited
metric and diagnostic exists, belongs to the same asset, and is available at or
before the snapshot known_at cut. Fails closed on conflict, missing, future, foreign,
or mutated reference.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from duckdb import DuckDBPyConnection

from investment_analyst.analytics.analysis_snapshot import (
    AnalysisSnapshot,
    analysis_snapshot_identity,
    canonical_evidence_set_digest,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)

ANALYSIS_SNAPSHOT_V2_TABLE: Final[str] = "analysis_snapshots_v2"
ANALYSIS_SNAPSHOT_V2_METRIC_LINKS_TABLE: Final[str] = "analysis_snapshot_v2_metric_links"
ANALYSIS_SNAPSHOT_V2_DIAGNOSTIC_LINKS_TABLE: Final[str] = "analysis_snapshot_v2_diagnostic_links"

ANALYSIS_SNAPSHOT_V2_COLUMNS: Final[tuple[str, ...]] = (
    "snapshot_id",
    "asset_id",
    "domain",
    "known_at",
    "policy_version",
    "evidence_set_digest",
    "created_at",
)
_FULL_SNAPSHOT_V2_COLUMNS: Final[tuple[str, ...]] = (*ANALYSIS_SNAPSHOT_V2_COLUMNS, "inserted_at")
MAX_SNAPSHOT_V2_PAGE: Final[int] = 256


class AnalysisSnapshotV2Error(StorageError):
    """Raised when an analysis snapshot v2 row, link or table cannot be trusted."""


def _instant_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AnalysisSnapshotV2Error("snapshot v2 instant must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_instant_text(value: object) -> datetime:
    if value is None:
        raise AnalysisSnapshotV2Error("snapshot v2 instant is missing")
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalysisSnapshotV2Error("snapshot v2 index instant is not timezone-aware")
    return parsed.astimezone(UTC)


def analysis_snapshot_v2_tables_exist(connection: DuckDBPyConnection) -> bool:
    """Return True if all analysis snapshot v2 index and link tables exist."""
    try:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    except Exception:
        return False
    names = {str(row[0]) for row in rows}
    return {
        ANALYSIS_SNAPSHOT_V2_TABLE,
        ANALYSIS_SNAPSHOT_V2_METRIC_LINKS_TABLE,
        ANALYSIS_SNAPSHOT_V2_DIAGNOSTIC_LINKS_TABLE,
    }.issubset(names)


def ensure_analysis_snapshot_v2_tables(
    connection: DuckDBPyConnection, *, create: bool = True
) -> None:
    """Require the typed snapshot tables, creating them only when authorized."""
    try:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{ANALYSIS_SNAPSHOT_V2_TABLE}'"
        ).fetchall()
    except Exception as error:
        raise AnalysisSnapshotV2Error("analysis snapshot v2 index table is missing") from error
    names = {str(row[0]) for row in rows}
    if not names:
        if not create:
            raise AnalysisSnapshotV2Error("analysis snapshot v2 index table is missing")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ANALYSIS_SNAPSHOT_V2_TABLE} (
                snapshot_id VARCHAR PRIMARY KEY,
                asset_id VARCHAR NOT NULL,
                domain VARCHAR NOT NULL,
                known_at VARCHAR NOT NULL,
                policy_version VARCHAR NOT NULL,
                evidence_set_digest VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                inserted_at VARCHAR NOT NULL DEFAULT (CAST(CURRENT_TIMESTAMP AS VARCHAR))
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ANALYSIS_SNAPSHOT_V2_METRIC_LINKS_TABLE} (
                snapshot_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                metric_result_id VARCHAR NOT NULL,
                PRIMARY KEY (snapshot_id, position)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ANALYSIS_SNAPSHOT_V2_DIAGNOSTIC_LINKS_TABLE} (
                snapshot_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                diagnostic_id VARCHAR NOT NULL,
                PRIMARY KEY (snapshot_id, position)
            )
            """
        )
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{ANALYSIS_SNAPSHOT_V2_TABLE}'"
        ).fetchall()
        names = {str(row[0]) for row in rows}
    if names != set(_FULL_SNAPSHOT_V2_COLUMNS):
        raise AnalysisSnapshotV2Error("analysis snapshot v2 index table is incompatible")
    if "document_json" in names:
        raise AnalysisSnapshotV2Error("analysis snapshot v2 index must not store documents")
    for table in (
        ANALYSIS_SNAPSHOT_V2_METRIC_LINKS_TABLE,
        ANALYSIS_SNAPSHOT_V2_DIAGNOSTIC_LINKS_TABLE,
    ):
        try:
            link_rows = connection.execute(
                f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"
            ).fetchall()
        except Exception as error:
            raise AnalysisSnapshotV2Error(f"snapshot v2 link table {table} is missing") from error
        link_names = {str(row[0]) for row in link_rows}
        if not link_names:
            raise AnalysisSnapshotV2Error(f"snapshot v2 link table {table} is missing")


class AnalysisSnapshotV2Store:
    """Access layer for typed analysis snapshots in staging."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def _verify_metric_reference(
        self,
        metric_id: UUID,
        *,
        asset_id: str,
        known_at: datetime,
    ) -> str | None:
        """Verify metric exists, matches asset, is available at known_at, and return set id."""
        rows = self._connection.execute(
            "SELECT asset_id, available_at, evidence_set_id FROM metric_results_v2 "
            "WHERE result_id = ?",
            [str(metric_id)],
        ).fetchall()
        if not rows:
            raise RecordNotFoundError(f"snapshot references missing metric {metric_id}")
        metric_asset_id, metric_avail_text, evidence_set_id = rows[0]
        if str(metric_asset_id) != asset_id:
            raise AnalysisSnapshotV2Error(
                f"snapshot for {asset_id} references foreign metric {metric_id} "
                f"belonging to {metric_asset_id}"
            )
        metric_avail = _parse_instant_text(metric_avail_text)
        if metric_avail > known_at:
            raise AnalysisSnapshotV2Error(
                f"snapshot cut at {known_at.isoformat()} references future metric "
                f"{metric_id} available at {metric_avail.isoformat()}"
            )
        return str(evidence_set_id) if evidence_set_id is not None else None

    def _verify_diagnostic_reference(
        self,
        diagnostic_id: UUID,
        *,
        asset_id: str,
        known_at: datetime,
    ) -> None:
        """Verify diagnostic exists, matches asset, and is available at or before known_at."""
        rows = self._connection.execute(
            "SELECT asset_id, available_at FROM diagnostic_results_v2 WHERE diagnostic_id = ?",
            [str(diagnostic_id)],
        ).fetchall()
        if not rows:
            raise RecordNotFoundError(f"snapshot references missing diagnostic {diagnostic_id}")
        diag_asset_id, diag_avail_text = rows[0]
        if str(diag_asset_id) != asset_id:
            raise AnalysisSnapshotV2Error(
                f"snapshot for {asset_id} references foreign diagnostic {diagnostic_id} "
                f"belonging to {diag_asset_id}"
            )
        diag_avail = _parse_instant_text(diag_avail_text)
        if diag_avail > known_at:
            raise AnalysisSnapshotV2Error(
                f"snapshot cut at {known_at.isoformat()} references future diagnostic "
                f"{diagnostic_id} available at {diag_avail.isoformat()}"
            )

    def _resolve_evidence_set_hashes(
        self,
        evidence_set_ids: Collection[str],
        *,
        asset_id: str,
        known_at: datetime,
    ) -> list[str]:
        """Resolve and verify canonical hashes of referenced EvidenceSets."""
        if not evidence_set_ids:
            return []
        hashes: list[str] = []
        for es_id in sorted(set(evidence_set_ids)):
            if not es_id:
                continue
            rows = self._connection.execute(
                "SELECT asset_id, available_at, canonical_hash FROM evidence_sets_v2 "
                "WHERE evidence_set_id = ?",
                [es_id],
            ).fetchall()
            if not rows:
                raise RecordNotFoundError(
                    f"snapshot metric references missing evidence set {es_id}"
                )
            es_asset_id, es_avail_text, canonical_hash = rows[0]
            if str(es_asset_id) != asset_id:
                raise AnalysisSnapshotV2Error(
                    f"snapshot for {asset_id} references foreign evidence set {es_id} "
                    f"belonging to {es_asset_id}"
                )
            es_avail = _parse_instant_text(es_avail_text)
            if es_avail > known_at:
                raise AnalysisSnapshotV2Error(
                    f"snapshot cut at {known_at.isoformat()} references future evidence set "
                    f"{es_id} available at {es_avail.isoformat()}"
                )
            hashes.append(str(canonical_hash))
        return hashes

    def save_snapshots(self, snapshots: Collection[AnalysisSnapshot]) -> BatchWriteReceipt:
        """Save snapshots idempotently, verifying identity, references, and evidence digest."""
        if not snapshots:
            return BatchWriteReceipt(created_count=0, reused_count=0)

        created_ids: list[UUID] = []
        reused_ids: list[UUID] = []

        for item in snapshots:
            if not isinstance(item, AnalysisSnapshot):
                raise AnalysisSnapshotV2Error("save_snapshots requires AnalysisSnapshot instances")

            expected_id = analysis_snapshot_identity(
                asset_id=item.asset_id,
                domain=item.domain,
                known_at=item.known_at,
                policy_version=item.policy_version,
                metric_ids=item.metric_ids,
                diagnostic_ids=item.diagnostic_ids,
                evidence_set_digest=item.evidence_set_digest,
            )
            if item.snapshot_id != expected_id:
                raise RecordConflictError(
                    f"snapshot identity {item.snapshot_id} is not deterministic"
                )

            known_at = item.known_at.astimezone(UTC)
            # Verify metrics and collect referenced evidence set IDs
            referenced_es_ids: set[str] = set()
            for mid in item.metric_ids:
                es_id = self._verify_metric_reference(
                    mid, asset_id=item.asset_id, known_at=known_at
                )
                if es_id:
                    referenced_es_ids.add(es_id)

            # Verify diagnostics
            for did in item.diagnostic_ids:
                self._verify_diagnostic_reference(did, asset_id=item.asset_id, known_at=known_at)

            # Verify EvidenceSets digest
            resolved_hashes = self._resolve_evidence_set_hashes(
                referenced_es_ids, asset_id=item.asset_id, known_at=known_at
            )
            expected_digest = canonical_evidence_set_digest(resolved_hashes)
            if expected_digest != item.evidence_set_digest:
                raise AnalysisSnapshotV2Error(
                    f"snapshot evidence_set_digest {item.evidence_set_digest} does not match "
                    f"resolved hashes digest {expected_digest}"
                )

            snap_id_str = str(item.snapshot_id)
            existing_rows = self._connection.execute(
                f"SELECT {', '.join(ANALYSIS_SNAPSHOT_V2_COLUMNS)} "
                f"FROM {ANALYSIS_SNAPSHOT_V2_TABLE} WHERE snapshot_id = ?",
                [snap_id_str],
            ).fetchall()

            if existing_rows:
                # Rehydrate existing snapshot and compare semantic content
                existing = self.get_snapshot(item.snapshot_id)
                if (
                    existing.asset_id == item.asset_id
                    and existing.domain == item.domain
                    and existing.known_at == item.known_at
                    and existing.policy_version == item.policy_version
                    and existing.evidence_set_digest == item.evidence_set_digest
                    and existing.metric_ids == item.metric_ids
                    and existing.diagnostic_ids == item.diagnostic_ids
                ):
                    # Reuse first persisted row (including original created_at)
                    reused_ids.append(item.snapshot_id)
                    continue
                raise RecordConflictError(f"snapshot content conflict for {item.snapshot_id}")

            # Insert new snapshot row
            self._connection.execute(
                f"""
                INSERT INTO {ANALYSIS_SNAPSHOT_V2_TABLE} (
                    snapshot_id, asset_id, domain, known_at,
                    policy_version, evidence_set_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    snap_id_str,
                    item.asset_id,
                    item.domain,
                    _instant_text(item.known_at),
                    item.policy_version,
                    item.evidence_set_digest,
                    _instant_text(item.created_at),
                ],
            )

            # Insert metric links in position order
            for pos, mid in enumerate(item.metric_ids):
                self._connection.execute(
                    f"""
                    INSERT INTO {ANALYSIS_SNAPSHOT_V2_METRIC_LINKS_TABLE} (
                        snapshot_id, position, metric_result_id
                    ) VALUES (?, ?, ?)
                    """,
                    [snap_id_str, pos, str(mid)],
                )

            # Insert diagnostic links in position order
            for pos, did in enumerate(item.diagnostic_ids):
                self._connection.execute(
                    f"""
                    INSERT INTO {ANALYSIS_SNAPSHOT_V2_DIAGNOSTIC_LINKS_TABLE} (
                        snapshot_id, position, diagnostic_id
                    ) VALUES (?, ?, ?)
                    """,
                    [snap_id_str, pos, str(did)],
                )

            created_ids.append(item.snapshot_id)

        return BatchWriteReceipt(created_ids=tuple(created_ids), reused_ids=tuple(reused_ids))

    def get_snapshot(self, snapshot_id: UUID) -> AnalysisSnapshot:
        """Hydrate typed AnalysisSnapshot, verifying links and cited references."""
        snap_id_str = str(snapshot_id)
        rows = self._connection.execute(
            f"SELECT {', '.join(ANALYSIS_SNAPSHOT_V2_COLUMNS)} "
            f"FROM {ANALYSIS_SNAPSHOT_V2_TABLE} WHERE snapshot_id = ?",
            [snap_id_str],
        ).fetchall()
        if not rows:
            raise RecordNotFoundError(f"analysis snapshot {snapshot_id} not found")
        row = rows[0]
        asset_id = str(row[1])
        domain = str(row[2])
        known_at = _parse_instant_text(row[3])
        policy_version = str(row[4])
        evidence_set_digest = str(row[5])
        created_at = _parse_instant_text(row[6])

        # Hydrate metric links
        metric_rows = self._connection.execute(
            f"""
            SELECT position, metric_result_id
            FROM {ANALYSIS_SNAPSHOT_V2_METRIC_LINKS_TABLE}
            WHERE snapshot_id = ? ORDER BY position
            """,
            [snap_id_str],
        ).fetchall()
        metric_ids: list[UUID] = []
        for pos, mid in metric_rows:
            if int(pos) != len(metric_ids):
                raise AnalysisSnapshotV2Error("snapshot metric link positions are corrupt")
            metric_ids.append(UUID(str(mid)))
            self._verify_metric_reference(metric_ids[-1], asset_id=asset_id, known_at=known_at)

        # Hydrate diagnostic links
        diag_rows = self._connection.execute(
            f"""
            SELECT position, diagnostic_id
            FROM {ANALYSIS_SNAPSHOT_V2_DIAGNOSTIC_LINKS_TABLE}
            WHERE snapshot_id = ? ORDER BY position
            """,
            [snap_id_str],
        ).fetchall()
        diagnostic_ids: list[UUID] = []
        for pos, did in diag_rows:
            if int(pos) != len(diagnostic_ids):
                raise AnalysisSnapshotV2Error("snapshot diagnostic link positions are corrupt")
            diagnostic_ids.append(UUID(str(did)))
            self._verify_diagnostic_reference(
                diagnostic_ids[-1], asset_id=asset_id, known_at=known_at
            )

        return AnalysisSnapshot(
            snapshot_id=snapshot_id,
            asset_id=asset_id,
            domain=domain,
            known_at=known_at,
            policy_version=policy_version,
            metric_ids=tuple(metric_ids),
            diagnostic_ids=tuple(diagnostic_ids),
            evidence_set_digest=evidence_set_digest,
            created_at=created_at,
        )

    def list_snapshots(
        self,
        *,
        asset_id: str | None = None,
        domain: str | None = None,
        known_to: datetime | None = None,
    ) -> list[AnalysisSnapshot]:
        """List and hydrate analysis snapshots in stable order."""
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if domain is not None:
            clauses.append("domain = ?")
            parameters.append(domain)
        if known_to is not None:
            clauses.append("known_at <= ?")
            parameters.append(_instant_text(known_to))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT snapshot_id FROM {ANALYSIS_SNAPSHOT_V2_TABLE}{where} "
            "ORDER BY known_at, snapshot_id",
            parameters,
        ).fetchall()
        return [self.get_snapshot(UUID(str(row[0]))) for row in rows]


__all__ = [
    "ANALYSIS_SNAPSHOT_V2_COLUMNS",
    "ANALYSIS_SNAPSHOT_V2_DIAGNOSTIC_LINKS_TABLE",
    "ANALYSIS_SNAPSHOT_V2_METRIC_LINKS_TABLE",
    "ANALYSIS_SNAPSHOT_V2_TABLE",
    "MAX_SNAPSHOT_V2_PAGE",
    "AnalysisSnapshotV2Error",
    "AnalysisSnapshotV2Store",
    "analysis_snapshot_v2_tables_exist",
    "ensure_analysis_snapshot_v2_tables",
]
