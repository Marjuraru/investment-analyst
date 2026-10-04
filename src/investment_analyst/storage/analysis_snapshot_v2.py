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
    AnalysisDomain,
    DomainMembershipError,
    require_authorized_domain,
    validate_diagnostic_mode_for_domain,
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
    AnalyticalV2ValidationContext,
    AnalyticalV2ValidationError,
    chunked_sequence,
    fetch_diagnostics_chunked,
    fetch_snapshots_chunked,
    validate_keyset_cursor,
    validate_keyset_limit,
)
from investment_analyst.storage.bounded_insert import (
    BoundedInsertTable,
    insert_bounded,
    write_transaction,
)
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)
from investment_analyst.storage.metric_v2 import MetricV2Error

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
        validation_context = AnalyticalV2ValidationContext()
        try:
            diags_by_id = fetch_diagnostics_chunked(
                self._connection,
                all_cited_diag_ids,
                validation_context=validation_context,
            )
        except (AnalyticalV2ValidationError, MetricV2Error) as error:
            raise AnalysisSnapshotV2Error(str(error)) from error

        # 3. Gather all cited metric IDs (both direct and from diagnostics)
        all_metric_ids = {mid for s in snapshots for mid in s.metric_ids}
        for diag in diags_by_id.values():
            for comp in diag.components:
                all_metric_ids.update(comp.metric_result_ids)
            for ev in diag.evidence:
                all_metric_ids.add(ev.metric_result_id)

        # 4. Resolve cited metrics through the same operation context used for diagnostics.
        try:
            metrics_by_id = validation_context.resolve_metrics(self._connection, all_metric_ids)
        except RecordNotFoundError as error:
            raise RecordNotFoundError(f"snapshot references missing metric: {error}") from error
        except (AnalyticalV2ValidationError, MetricV2Error) as error:
            raise AnalysisSnapshotV2Error(str(error)) from error

        # 5. Reuse EvidenceSets already checked with the reachable metric lineage.
        all_es_uuids = {
            UUID(str(m.parameters["evidence_set_id"]))
            for m in metrics_by_id.values()
            if m.parameters.get("evidence_set_id")
        }
        es_info: dict[str, tuple[str, datetime, str]] = {}
        if all_es_uuids:
            es_by_id = validation_context.require_evidence_sets(all_es_uuids)

            for es_uuid in all_es_uuids:
                es = es_by_id[es_uuid]
                es_info[str(es_uuid)] = (es.asset_id, es.available_at, es.canonical_hash)

        # 6. Verify each snapshot
        for item in snapshots:
            known_at = item.known_at.astimezone(UTC)
            snap_es_ids: set[str] = set()

            # Verify direct metrics
            for mid in item.metric_ids:
                if mid not in metrics_by_id:
                    raise RecordNotFoundError(f"snapshot references missing metric {mid}")
                metric = metrics_by_id[mid]
                if metric.asset_id != item.asset_id:
                    raise AnalysisSnapshotV2Error(
                        f"snapshot for {item.asset_id} references foreign metric {mid} "
                        f"belonging to {metric.asset_id}"
                    )
                if metric.available_at > known_at:
                    raise AnalysisSnapshotV2Error(
                        f"snapshot cut at {known_at.isoformat()} references future metric "
                        f"{mid} available at {metric.available_at.isoformat()}"
                    )
                try:
                    validate_metric_key_for_domain(metric.metric_key, item.domain)
                except DomainMembershipError as error:
                    raise AnalysisSnapshotV2Error(str(error)) from error
                es_id = metric.parameters.get("evidence_set_id")
                if es_id:
                    snap_es_ids.add(str(es_id))

            # Diagnostics check: valuation and events do not have authorized diagnostics
            if (
                item.domain in (AnalysisDomain.VALUATION.value, AnalysisDomain.EVENTS.value)
                and item.diagnostic_ids
            ):
                raise AnalysisSnapshotV2Error(
                    f"domain {item.domain!r} does not have authorized diagnostic mode; "
                    "snapshots with diagnostics require subsequent contract"
                )

            # Verify diagnostics and their cited metrics
            for did in item.diagnostic_ids:
                diag = diags_by_id[did]
                try:
                    validate_diagnostic_mode_for_domain(diag.mode, item.domain)
                except DomainMembershipError as error:
                    raise AnalysisSnapshotV2Error(str(error)) from error
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
                    if mid not in metrics_by_id:
                        raise RecordNotFoundError(
                            f"snapshot diagnostic references missing metric {mid}"
                        )
                    m = metrics_by_id[mid]
                    try:
                        validate_metric_key_for_domain(m.metric_key, item.domain)
                    except DomainMembershipError as error:
                        raise AnalysisSnapshotV2Error(str(error)) from error
                    es_id = m.parameters.get("evidence_set_id")
                    if es_id:
                        snap_es_ids.add(str(es_id))

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

        # 7. Check existing rows in DB in chunks <= 256 for idempotence/conflict
        all_snap_ids = [str(item.snapshot_id) for item in snapshots]
        existing_rows: dict[str, tuple[object, ...]] = {}
        for id_chunk in chunked_sequence(all_snap_ids, MAX_CHUNK_SIZE):
            placeholders = ", ".join("?" for _ in id_chunk)
            columns = ", ".join(ANALYSIS_SNAPSHOT_V2_COLUMNS)
            rows = self._connection.execute(
                f"SELECT {columns} FROM {ANALYSIS_SNAPSHOT_V2_TABLE} "
                f"WHERE snapshot_id IN ({placeholders})",
                list(id_chunk),
            ).fetchall()
            for r in rows:
                existing_rows[str(r[0])] = r

        if existing_rows:
            existing_snaps = fetch_snapshots_chunked(
                self._connection,
                [UUID(k) for k in existing_rows],
                validation_context=validation_context,
                snapshot_rows_by_id={
                    UUID(snapshot_id): row for snapshot_id, row in existing_rows.items()
                },
            )
            for item in snapshots:
                k = str(item.snapshot_id)
                if k in existing_rows:
                    existing = existing_snaps[item.snapshot_id]
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
                    else:
                        raise RecordConflictError(
                            f"snapshot content conflict for {item.snapshot_id}"
                        )

        reused_set = {str(uid) for uid in reused_ids}
        new_snaps = [s for s in snapshots if str(s.snapshot_id) not in reused_set]

        # 8. Insert new snapshot rows and links in batch
        snap_rows = []
        metric_link_rows = []
        diag_link_rows = []
        for item in new_snaps:
            snap_id_str = str(item.snapshot_id)
            snap_rows.append(
                [
                    snap_id_str,
                    item.asset_id,
                    item.domain,
                    _instant_text(item.known_at),
                    item.policy_version,
                    item.evidence_set_digest,
                    _instant_text(item.created_at),
                ]
            )
            for pos, mid in enumerate(item.metric_ids):
                metric_link_rows.append([snap_id_str, pos, str(mid)])
            for pos, did in enumerate(item.diagnostic_ids):
                diag_link_rows.append([snap_id_str, pos, str(did)])
            created_ids.append(item.snapshot_id)

        if snap_rows:
            with write_transaction(self._connection):
                insert_bounded(
                    self._connection,
                    BoundedInsertTable.SNAPSHOTS_V2,
                    snap_rows,
                )
                insert_bounded(
                    self._connection,
                    BoundedInsertTable.SNAPSHOT_METRIC_LINKS_V2,
                    metric_link_rows,
                )
                insert_bounded(
                    self._connection,
                    BoundedInsertTable.SNAPSHOT_DIAGNOSTIC_LINKS_V2,
                    diag_link_rows,
                )

        return BatchWriteReceipt(created_ids=tuple(created_ids), reused_ids=tuple(reused_ids))

    def get_snapshots(self, snapshot_ids: Collection[UUID]) -> dict[UUID, AnalysisSnapshot]:
        """Hydrate typed AnalysisSnapshots, verifying links and cited references."""
        try:
            return fetch_snapshots_chunked(self._connection, snapshot_ids)
        except (AnalyticalV2ValidationError, MetricV2Error) as error:
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

        if limit is not None:
            query_clauses = list(clauses)
            query_params = list(parameters)
            if normalized_cursor is not None:
                query_clauses.append("(known_at > ? OR (known_at = ? AND snapshot_id > ?))")
                query_params.extend(
                    [
                        normalized_cursor[0],
                        normalized_cursor[0],
                        normalized_cursor[1],
                    ]
                )
            where = f" WHERE {' AND '.join(query_clauses)}" if query_clauses else ""
            limit_clause = f" LIMIT {limit}"
            rows = self._connection.execute(
                f"SELECT snapshot_id FROM {ANALYSIS_SNAPSHOT_V2_TABLE}{where} "
                f"ORDER BY known_at, snapshot_id{limit_clause}",
                query_params,
            ).fetchall()
            ids = [UUID(str(row[0])) for row in rows]
        else:
            all_ids: list[UUID] = []
            current_cursor = normalized_cursor
            while True:
                page_clauses = list(clauses)
                page_params = list(parameters)
                if current_cursor is not None:
                    page_clauses.append("(known_at > ? OR (known_at = ? AND snapshot_id > ?))")
                    page_params.extend([current_cursor[0], current_cursor[0], current_cursor[1]])
                where = f" WHERE {' AND '.join(page_clauses)}" if page_clauses else ""
                page_rows = self._connection.execute(
                    f"SELECT snapshot_id, known_at FROM {ANALYSIS_SNAPSHOT_V2_TABLE}{where} "
                    f"ORDER BY known_at, snapshot_id LIMIT {MAX_CHUNK_SIZE}",
                    page_params,
                ).fetchall()
                if not page_rows:
                    break
                for row in page_rows:
                    all_ids.append(UUID(str(row[0])))
                last_row = page_rows[-1]
                current_cursor = (str(last_row[1]), str(last_row[0]))
                if len(page_rows) < MAX_CHUNK_SIZE:
                    break
            ids = all_ids

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
