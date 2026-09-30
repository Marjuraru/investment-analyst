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

from investment_analyst.analytics.analysis_domain import (
    DomainMembershipError,
    require_authorized_domain,
    validate_metric_key_for_domain,
)
from investment_analyst.analytics.analysis_snapshot import (
    AnalysisSnapshot,
    analysis_snapshot_identity,
    canonical_evidence_set_digest,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.storage.analytical_v2_validation import (
    MAX_CHUNK_SIZE,
    AnalyticalV2ValidationError,
    chunked_sequence,
    fetch_diagnostics_chunked,
    fetch_snapshots_chunked,
    validate_keyset_cursor,
    validate_keyset_limit,
)
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
            return BatchWriteReceipt()

        created_ids: list[UUID] = []
        reused_ids: list[UUID] = []

        # 1. Type, deterministic identity and domain membership checks
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
            try:
                require_authorized_domain(item.domain)
            except DomainMembershipError as error:
                raise AnalysisSnapshotV2Error(str(error)) from error

        # 2. Gather cited diagnostics across the batch and hydrate them in chunks <= 256
        all_cited_diag_ids = {did for s in snapshots for did in s.diagnostic_ids}
        try:
            diags_by_id = fetch_diagnostics_chunked(self._connection, all_cited_diag_ids)
        except AnalyticalV2ValidationError as error:
            raise AnalysisSnapshotV2Error(str(error)) from error

        # 3. Gather all cited metric IDs (both direct and from diagnostics)
        all_metric_ids = {mid for s in snapshots for mid in s.metric_ids}
        for diag in diags_by_id.values():
            for comp in diag.components:
                all_metric_ids.update(comp.metric_result_ids)
            for ev in diag.evidence:
                all_metric_ids.add(ev.metric_result_id)

        # 4. Fetch metric metadata in chunks <= 256
        metric_info: dict[UUID, tuple[str, datetime, str, str | None]] = {}
        for m_chunk in chunked_sequence(sorted(all_metric_ids, key=str), MAX_CHUNK_SIZE):
            m_placeholders = ", ".join("?" for _ in m_chunk)
            rows = self._connection.execute(
                f"SELECT result_id, asset_id, available_at, metric_key, evidence_set_id "
                f"FROM metric_results_v2 WHERE result_id IN ({m_placeholders})",
                [str(item) for item in m_chunk],
            ).fetchall()
            for r_id, a_id, avail, key, es_id in rows:
                metric_info[UUID(str(r_id))] = (
                    str(a_id),
                    _parse_instant_text(avail),
                    str(key),
                    str(es_id) if es_id is not None else None,
                )

        for mid in all_metric_ids:
            if mid not in metric_info:
                raise RecordNotFoundError(f"snapshot references missing metric {mid}")

        # 5. Gather all referenced EvidenceSets across all cited metrics in chunks <= 256
        all_es_ids = {info[3] for info in metric_info.values() if info[3] is not None}
        es_info: dict[str, tuple[str, datetime, str]] = {}
        for es_chunk in chunked_sequence(sorted(all_es_ids), MAX_CHUNK_SIZE):
            es_placeholders = ", ".join("?" for _ in es_chunk)
            rows = self._connection.execute(
                f"SELECT evidence_set_id, asset_id, available_at, canonical_hash "
                f"FROM evidence_sets_v2 WHERE evidence_set_id IN ({es_placeholders})",
                list(es_chunk),
            ).fetchall()
            for e_id, a_id, avail, c_hash in rows:
                es_info[str(e_id)] = (
                    str(a_id),
                    _parse_instant_text(avail),
                    str(c_hash),
                )

        for es_id in all_es_ids:
            if es_id not in es_info:
                raise RecordNotFoundError(
                    f"snapshot metric references missing evidence set {es_id}"
                )

        # 6. Verify each snapshot
        for item in snapshots:
            known_at = item.known_at.astimezone(UTC)
            snap_es_ids: set[str] = set()

            # Verify direct metrics
            for mid in item.metric_ids:
                m_asset, m_avail, m_key, es_id = metric_info[mid]
                if m_asset != item.asset_id:
                    raise AnalysisSnapshotV2Error(
                        f"snapshot for {item.asset_id} references foreign metric {mid} "
                        f"belonging to {m_asset}"
                    )
                if m_avail > known_at:
                    raise AnalysisSnapshotV2Error(
                        f"snapshot cut at {known_at.isoformat()} references future metric "
                        f"{mid} available at {m_avail.isoformat()}"
                    )
                try:
                    validate_metric_key_for_domain(m_key, item.domain)
                except DomainMembershipError as error:
                    raise AnalysisSnapshotV2Error(str(error)) from error
                if es_id:
                    snap_es_ids.add(es_id)

            # Verify diagnostics and their cited metrics
            for did in item.diagnostic_ids:
                diag = diags_by_id[did]
                if diag.asset_id != item.asset_id:
                    raise AnalysisSnapshotV2Error(
                        f"snapshot for {item.asset_id} references foreign diagnostic {did} "
                        f"belonging to {diag.asset_id}"
                    )
                if diag.available_at > known_at:
                    raise AnalysisSnapshotV2Error(
                        f"snapshot cut at {known_at.isoformat()} references future diagnostic "
                        f"{did} available at {diag.available_at.isoformat()}"
                    )
                diag_mids: set[UUID] = set()
                for comp in diag.components:
                    diag_mids.update(comp.metric_result_ids)
                for ev in diag.evidence:
                    diag_mids.add(ev.metric_result_id)
                for mid in diag_mids:
                    _, _, m_key, es_id = metric_info[mid]
                    try:
                        validate_metric_key_for_domain(m_key, item.domain)
                    except DomainMembershipError as error:
                        raise AnalysisSnapshotV2Error(str(error)) from error
                    if es_id:
                        snap_es_ids.add(es_id)

            # Verify evidence sets and digest
            snap_es_hashes: list[str] = []
            for es_id in sorted(snap_es_ids):
                es_asset, es_avail, es_hash = es_info[es_id]
                if es_asset != item.asset_id:
                    raise AnalysisSnapshotV2Error(
                        f"snapshot for {item.asset_id} references foreign evidence set {es_id} "
                        f"belonging to {es_asset}"
                    )
                if es_avail > known_at:
                    raise AnalysisSnapshotV2Error(
                        f"snapshot cut at {known_at.isoformat()} references future evidence set "
                        f"{es_id} available at {es_avail.isoformat()}"
                    )
                snap_es_hashes.append(es_hash)

            expected_digest = canonical_evidence_set_digest(snap_es_hashes)
            if expected_digest != item.evidence_set_digest:
                raise AnalysisSnapshotV2Error(
                    f"snapshot evidence_set_digest {item.evidence_set_digest} does not match "
                    f"resolved hashes digest {expected_digest}"
                )

            # Check existing row
            snap_id_str = str(item.snapshot_id)
            existing_rows = self._connection.execute(
                f"SELECT {', '.join(ANALYSIS_SNAPSHOT_V2_COLUMNS)} "
                f"FROM {ANALYSIS_SNAPSHOT_V2_TABLE} WHERE snapshot_id = ?",
                [snap_id_str],
            ).fetchall()

            if existing_rows:
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

    def get_snapshots(self, snapshot_ids: Collection[UUID]) -> dict[UUID, AnalysisSnapshot]:
        """Hydrate typed AnalysisSnapshots, verifying links and cited references."""
        try:
            return fetch_snapshots_chunked(self._connection, snapshot_ids)
        except AnalyticalV2ValidationError as error:
            raise AnalysisSnapshotV2Error(str(error)) from error

    def get_snapshot(self, snapshot_id: UUID) -> AnalysisSnapshot:
        """Hydrate typed AnalysisSnapshot, verifying links and cited references."""
        results = self.get_snapshots([snapshot_id])
        return results[snapshot_id]

    def list_snapshots(
        self,
        *,
        asset_id: str | None = None,
        domain: str | None = None,
        known_to: datetime | None = None,
        cursor_at: datetime | str | None = None,
        cursor_id: UUID | str | None = None,
        limit: int | None = None,
    ) -> list[AnalysisSnapshot]:
        """List and hydrate analysis snapshots in stable order without N+1 queries."""
        try:
            normalized_cursor = validate_keyset_cursor(cursor_at, cursor_id)
            if limit is not None:
                validate_keyset_limit(limit)
        except AnalyticalV2ValidationError as error:
            raise AnalysisSnapshotV2Error(str(error)) from error

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

        if normalized_cursor is not None:
            clauses.append("(known_at > ? OR (known_at = ? AND snapshot_id > ?))")
            parameters.extend([normalized_cursor[0], normalized_cursor[0], normalized_cursor[1]])

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = f" LIMIT {limit}" if limit is not None else ""
        rows = self._connection.execute(
            f"SELECT snapshot_id FROM {ANALYSIS_SNAPSHOT_V2_TABLE}{where} "
            f"ORDER BY known_at, snapshot_id{limit_clause}",
            parameters,
        ).fetchall()
        ids = [UUID(str(row[0])) for row in rows]
        hydrated = self.get_snapshots(ids)
        return [hydrated[sid] for sid in ids]


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
