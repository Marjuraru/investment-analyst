"""Typed diagnostic result v2 staging table with verified components and evidence.

Part of DATA-CHASSIS-32.
Diagnostics live in the same file-backed DuckDB index as the raw v2 staging
under the same writer lock, in their own ``diagnostic_results_v2`` table with
ordered component, evidence, and metric link tables.
No ``document_json`` column exists.
Reads rehydrate the strict DiagnosticResult model, confirming that every cited
metric exists, belongs to the same asset, and is available at or before the
diagnostic available_at cut. Fails closed on conflict, missing, future, foreign,
or mutated reference.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID

from duckdb import DuckDBPyConnection

from investment_analyst.analytics.analysis_domain import (
    DomainMembershipError,
    validate_diagnostic_internal_consistency,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models.diagnostic import (
    DiagnosticResult,
)
from investment_analyst.core.models.enums import (
    DiagnosticMode,
)
from investment_analyst.storage.analytical_v2_validation import (
    MAX_CHUNK_SIZE,
    AnalyticalV2ValidationError,
    chunked_sequence,
    fetch_diagnostics_chunked,
    fetch_metrics_chunked,
    validate_keyset_cursor,
    validate_keyset_limit,
    validate_link_positions,
    verify_metrics_dag_and_lineage,
)
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)
from investment_analyst.storage.metric_v2 import MetricV2Error

DIAGNOSTIC_V2_TABLE: Final[str] = "diagnostic_results_v2"
DIAGNOSTIC_V2_COMPONENTS_TABLE: Final[str] = "diagnostic_v2_components"
DIAGNOSTIC_V2_COMPONENT_METRIC_LINKS_TABLE: Final[str] = "diagnostic_v2_component_metric_links"
DIAGNOSTIC_V2_EVIDENCE_TABLE: Final[str] = "diagnostic_v2_evidence"

DIAGNOSTIC_V2_COLUMNS: Final[tuple[str, ...]] = (
    "diagnostic_id",
    "asset_id",
    "mode",
    "verdict",
    "final_score_text",
    "confidence_text",
    "as_of",
    "available_at",
    "computed_at",
    "algorithm_version",
    "summary",
    "quality",
)
_FULL_DIAGNOSTIC_V2_COLUMNS: Final[tuple[str, ...]] = (*DIAGNOSTIC_V2_COLUMNS, "inserted_at")
MAX_DIAGNOSTIC_V2_PAGE: Final[int] = 256


class DiagnosticV2Error(StorageError):
    """Raised when a diagnostic v2 row, link or table cannot be trusted."""


def _instant_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DiagnosticV2Error("diagnostic v2 instant must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_instant_text(value: object) -> datetime:
    if value is None:
        raise DiagnosticV2Error("diagnostic v2 instant is missing")
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DiagnosticV2Error("diagnostic v2 index instant is not timezone-aware")
    return parsed.astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DiagnosticV2Error("diagnostic v2 value must be a finite Decimal")
    return str(value)


def _parse_decimal_text(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise DiagnosticV2Error("diagnostic v2 value is not a valid Decimal") from error
    if not parsed.is_finite():
        raise DiagnosticV2Error("diagnostic v2 value must be finite")
    return parsed


def diagnostic_v2_tables_exist(connection: DuckDBPyConnection) -> bool:
    """Return True if all diagnostic v2 index and link tables exist."""
    try:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    except Exception:
        return False
    names = {str(row[0]) for row in rows}
    return {
        DIAGNOSTIC_V2_TABLE,
        DIAGNOSTIC_V2_COMPONENTS_TABLE,
        DIAGNOSTIC_V2_COMPONENT_METRIC_LINKS_TABLE,
        DIAGNOSTIC_V2_EVIDENCE_TABLE,
    }.issubset(names)


def ensure_diagnostic_v2_tables(connection: DuckDBPyConnection, *, create: bool = True) -> None:
    """Require the typed diagnostic tables, creating them only when authorized."""
    try:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{DIAGNOSTIC_V2_TABLE}'"
        ).fetchall()
    except Exception as error:
        raise DiagnosticV2Error("diagnostic v2 index table is missing") from error
    names = {str(row[0]) for row in rows}
    if not names:
        if not create:
            raise DiagnosticV2Error("diagnostic v2 index table is missing")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DIAGNOSTIC_V2_TABLE} (
                diagnostic_id VARCHAR PRIMARY KEY,
                asset_id VARCHAR NOT NULL,
                mode VARCHAR NOT NULL,
                verdict VARCHAR NOT NULL,
                final_score_text VARCHAR NOT NULL,
                confidence_text VARCHAR NOT NULL,
                as_of VARCHAR NOT NULL,
                available_at VARCHAR NOT NULL,
                computed_at VARCHAR NOT NULL,
                algorithm_version VARCHAR NOT NULL,
                summary VARCHAR NOT NULL,
                quality VARCHAR NOT NULL,
                inserted_at VARCHAR NOT NULL DEFAULT (CAST(CURRENT_TIMESTAMP AS VARCHAR))
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DIAGNOSTIC_V2_COMPONENTS_TABLE} (
                diagnostic_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                component_key VARCHAR NOT NULL,
                score_text VARCHAR NOT NULL,
                weight_text VARCHAR NOT NULL,
                weighted_contribution_text VARCHAR NOT NULL,
                explanation VARCHAR NOT NULL,
                PRIMARY KEY (diagnostic_id, position)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DIAGNOSTIC_V2_COMPONENT_METRIC_LINKS_TABLE} (
                diagnostic_id VARCHAR NOT NULL,
                component_position INTEGER NOT NULL,
                link_position INTEGER NOT NULL,
                metric_result_id VARCHAR NOT NULL,
                PRIMARY KEY (diagnostic_id, component_position, link_position)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DIAGNOSTIC_V2_EVIDENCE_TABLE} (
                diagnostic_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                metric_result_id VARCHAR NOT NULL,
                direction VARCHAR NOT NULL,
                contribution_text VARCHAR NOT NULL,
                reason VARCHAR NOT NULL,
                PRIMARY KEY (diagnostic_id, position)
            )
            """
        )
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{DIAGNOSTIC_V2_TABLE}'"
        ).fetchall()
        names = {str(row[0]) for row in rows}
    if names != set(_FULL_DIAGNOSTIC_V2_COLUMNS):
        raise DiagnosticV2Error("diagnostic v2 index table is incompatible")
    if "document_json" in names:
        raise DiagnosticV2Error("diagnostic v2 index must not store documents")
    for table in (
        DIAGNOSTIC_V2_COMPONENTS_TABLE,
        DIAGNOSTIC_V2_COMPONENT_METRIC_LINKS_TABLE,
        DIAGNOSTIC_V2_EVIDENCE_TABLE,
    ):
        try:
            link_rows = connection.execute(
                f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"
            ).fetchall()
        except Exception as error:
            raise DiagnosticV2Error(f"diagnostic v2 link table {table} is missing") from error
        link_names = {str(row[0]) for row in link_rows}
        if not link_names:
            raise DiagnosticV2Error(f"diagnostic v2 link table {table} is missing")


class DiagnosticV2Store:
    """Access layer for typed diagnostic results and components in staging."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def _verify_metric_reference(
        self,
        metric_id: UUID,
        *,
        asset_id: str,
        available_at: datetime,
    ) -> None:
        """Verify that referenced metric exists, matches asset, and is available at cut."""
        rows = self._connection.execute(
            "SELECT asset_id, available_at FROM metric_results_v2 WHERE result_id = ?",
            [str(metric_id)],
        ).fetchall()
        if not rows:
            raise RecordNotFoundError(f"diagnostic references missing metric {metric_id}")
        metric_asset_id, metric_available_text = rows[0]
        if str(metric_asset_id) != asset_id:
            raise DiagnosticV2Error(
                f"diagnostic for {asset_id} references foreign metric {metric_id} "
                f"belonging to {metric_asset_id}"
            )
        metric_avail = _parse_instant_text(metric_available_text)
        if metric_avail > available_at:
            raise DiagnosticV2Error(
                f"diagnostic available at {available_at.isoformat()} references future "
                f"metric {metric_id} available at {metric_avail.isoformat()}"
            )

    def save_diagnostics(self, diagnostics: Collection[DiagnosticResult]) -> BatchWriteReceipt:
        """Save diagnostics idempotently, failing closed on conflict or invalid references."""
        if not diagnostics:
            return BatchWriteReceipt()

        # 1. Structure and link position checks
        for item in diagnostics:
            if not isinstance(item, DiagnosticResult):
                raise DiagnosticV2Error("save_diagnostics requires DiagnosticResult instances")
            c_positions = [pos for pos, _ in enumerate(item.components)]
            validate_link_positions(c_positions, "diagnostic components")
            for comp in item.components:
                validate_link_positions(
                    list(range(len(comp.metric_result_ids))), "component metric links"
                )
            validate_link_positions(list(range(len(item.evidence))), "diagnostic evidence")

        # 2. Gather, hydrate and verify cited metrics in chunks <= 256 using fetch_metrics_chunked
        all_cited_mids = sorted(
            {
                mid
                for item in diagnostics
                for comp in item.components
                for mid in comp.metric_result_ids
            }
            | {ev.metric_result_id for item in diagnostics for ev in item.evidence},
            key=str,
        )

        try:
            cited_metrics_by_id = fetch_metrics_chunked(self._connection, all_cited_mids)
            if cited_metrics_by_id:
                verify_metrics_dag_and_lineage(self._connection, cited_metrics_by_id)
        except RecordNotFoundError as error:
            raise RecordNotFoundError(f"diagnostic references missing metric: {error}") from error
        except (AnalyticalV2ValidationError, MetricV2Error) as error:
            raise DiagnosticV2Error(str(error)) from error

        # 3. Check cited metrics per diagnostic
        for item in diagnostics:
            avail = item.available_at.astimezone(UTC)
            item_mids: set[UUID] = set()
            for comp in item.components:
                item_mids.update(comp.metric_result_ids)
            for ev in item.evidence:
                item_mids.add(ev.metric_result_id)

            for mid in item_mids:
                if mid not in cited_metrics_by_id:
                    raise RecordNotFoundError(f"diagnostic references missing metric {mid}")
                metric = cited_metrics_by_id[mid]
                if metric.asset_id != item.asset_id:
                    raise DiagnosticV2Error(
                        f"diagnostic for {item.asset_id} references foreign metric {mid} "
                        f"belonging to {metric.asset_id}"
                    )
                if metric.available_at > avail:
                    raise DiagnosticV2Error(
                        f"diagnostic available at {avail.isoformat()} references future "
                        f"metric {mid} available at {metric.available_at.isoformat()}"
                    )

            # Validate domain consistency
            metric_keys = {mid: cited_metrics_by_id[mid].metric_key for mid in item_mids}
            try:
                validate_diagnostic_internal_consistency(item, metric_keys)
            except DomainMembershipError as error:
                raise DiagnosticV2Error(str(error)) from error

        # 4. Check existing diagnostics in DB in chunks <= 256 for idempotence/conflict
        all_diag_ids = [str(item.diagnostic_id) for item in diagnostics]
        existing_rows: dict[str, tuple[object, ...]] = {}
        for id_chunk in chunked_sequence(all_diag_ids, MAX_CHUNK_SIZE):
            placeholders = ", ".join("?" for _ in id_chunk)
            columns = ", ".join(DIAGNOSTIC_V2_COLUMNS)
            rows = self._connection.execute(
                f"SELECT {columns} FROM {DIAGNOSTIC_V2_TABLE} "
                f"WHERE diagnostic_id IN ({placeholders})",
                list(id_chunk),
            ).fetchall()
            for r in rows:
                existing_rows[str(r[0])] = r

        created_ids: list[UUID] = []
        reused_ids: list[UUID] = []

        if existing_rows:
            existing_diags = fetch_diagnostics_chunked(
                self._connection, [UUID(k) for k in existing_rows]
            )
            for item in diagnostics:
                k = str(item.diagnostic_id)
                if k in existing_rows:
                    existing_diag = existing_diags[item.diagnostic_id]
                    if (
                        existing_diag.asset_id == item.asset_id
                        and existing_diag.mode == item.mode
                        and existing_diag.verdict == item.verdict
                        and existing_diag.final_score == item.final_score
                        and existing_diag.confidence == item.confidence
                        and existing_diag.as_of == item.as_of
                        and existing_diag.available_at == item.available_at
                        and existing_diag.computed_at == item.computed_at
                        and existing_diag.algorithm_version == item.algorithm_version
                        and existing_diag.summary == item.summary
                        and existing_diag.quality == item.quality
                        and existing_diag.components == item.components
                        and existing_diag.evidence == item.evidence
                    ):
                        reused_ids.append(item.diagnostic_id)
                    else:
                        raise RecordConflictError(
                            f"diagnostic content conflict for {item.diagnostic_id}"
                        )

        reused_set = {str(uid) for uid in reused_ids}
        diag_rows = []
        comp_rows = []
        link_rows = []
        ev_rows = []

        for item in diagnostics:
            diag_id_str = str(item.diagnostic_id)
            if diag_id_str in reused_set:
                continue

            diag_rows.append(
                [
                    diag_id_str,
                    item.asset_id,
                    item.mode.value if hasattr(item.mode, "value") else str(item.mode),
                    item.verdict.value if hasattr(item.verdict, "value") else str(item.verdict),
                    _decimal_text(item.final_score),
                    _decimal_text(item.confidence),
                    _instant_text(item.as_of),
                    _instant_text(item.available_at),
                    _instant_text(item.computed_at),
                    item.algorithm_version,
                    item.summary,
                    item.quality.value if hasattr(item.quality, "value") else str(item.quality),
                ]
            )

            for comp_pos, comp in enumerate(item.components):
                comp_rows.append(
                    [
                        diag_id_str,
                        comp_pos,
                        comp.component_key,
                        _decimal_text(comp.score),
                        _decimal_text(comp.weight),
                        _decimal_text(comp.weighted_contribution),
                        comp.explanation,
                    ]
                )
                for link_pos, mid in enumerate(comp.metric_result_ids):
                    link_rows.append([diag_id_str, comp_pos, link_pos, str(mid)])

            for ev_pos, ev in enumerate(item.evidence):
                ev_rows.append(
                    [
                        diag_id_str,
                        ev_pos,
                        str(ev.metric_result_id),
                        ev.direction.value if hasattr(ev.direction, "value") else str(ev.direction),
                        _decimal_text(ev.contribution),
                        ev.reason,
                    ]
                )

            created_ids.append(item.diagnostic_id)

        in_tx = False
        try:
            self._connection.execute("BEGIN TRANSACTION")
            in_tx = True
        except Exception:
            pass

        try:
            if diag_rows:
                self._connection.executemany(
                    f"""
                    INSERT INTO {DIAGNOSTIC_V2_TABLE} (
                        diagnostic_id, asset_id, mode, verdict, final_score_text,
                        confidence_text, as_of, available_at, computed_at,
                        algorithm_version, summary, quality
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    diag_rows,
                )
            if comp_rows:
                self._connection.executemany(
                    f"""
                    INSERT INTO {DIAGNOSTIC_V2_COMPONENTS_TABLE} (
                        diagnostic_id, position, component_key, score_text,
                        weight_text, weighted_contribution_text, explanation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    comp_rows,
                )
            if link_rows:
                self._connection.executemany(
                    f"""
                    INSERT INTO {DIAGNOSTIC_V2_COMPONENT_METRIC_LINKS_TABLE} (
                        diagnostic_id, component_position, link_position, metric_result_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    link_rows,
                )
            if ev_rows:
                self._connection.executemany(
                    f"""
                    INSERT INTO {DIAGNOSTIC_V2_EVIDENCE_TABLE} (
                        diagnostic_id, position, metric_result_id, direction,
                        contribution_text, reason
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ev_rows,
                )
            if in_tx:
                self._connection.execute("COMMIT")
        except Exception:
            if in_tx:
                self._connection.execute("ROLLBACK")
            raise

        return BatchWriteReceipt(created_ids=tuple(created_ids), reused_ids=tuple(reused_ids))

    def get_diagnostics(self, diagnostic_ids: Collection[UUID]) -> dict[UUID, DiagnosticResult]:
        """Hydrate typed DiagnosticResults, verifying components, links and cited metrics."""
        try:
            return fetch_diagnostics_chunked(self._connection, diagnostic_ids)
        except (AnalyticalV2ValidationError, MetricV2Error) as error:
            raise DiagnosticV2Error(str(error)) from error

    def list_diagnostics(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of: datetime | None = None,
        available_to: datetime | None = None,
        cursor_at: datetime | str | None = None,
        cursor_id: UUID | str | None = None,
        limit: int | None = None,
    ) -> list[DiagnosticResult]:
        """List and hydrate diagnostic results in stable order without N+1 queries."""
        try:
            normalized_cursor = validate_keyset_cursor(cursor_at, cursor_id)
            if limit is not None:
                validate_keyset_limit(limit)
        except AnalyticalV2ValidationError as error:
            raise DiagnosticV2Error(str(error)) from error

        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if mode is not None:
            clauses.append("mode = ?")
            parameters.append(mode.value if hasattr(mode, "value") else str(mode))
        if as_of is not None:
            clauses.append("as_of <= ?")
            parameters.append(_instant_text(as_of))
        if available_to is not None:
            clauses.append("available_at <= ?")
            parameters.append(_instant_text(available_to))

        if limit is not None:
            query_clauses = list(clauses)
            query_params = list(parameters)
            if normalized_cursor is not None:
                query_clauses.append(
                    "(available_at > ? OR (available_at = ? AND diagnostic_id > ?))"
                )
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
                f"SELECT diagnostic_id FROM {DIAGNOSTIC_V2_TABLE}{where} "
                f"ORDER BY available_at, diagnostic_id{limit_clause}",
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
                    page_clauses.append(
                        "(available_at > ? OR (available_at = ? AND diagnostic_id > ?))"
                    )
                    page_params.extend([current_cursor[0], current_cursor[0], current_cursor[1]])
                where = f" WHERE {' AND '.join(page_clauses)}" if page_clauses else ""
                page_rows = self._connection.execute(
                    f"SELECT diagnostic_id, available_at FROM {DIAGNOSTIC_V2_TABLE}{where} "
                    f"ORDER BY available_at, diagnostic_id LIMIT {MAX_CHUNK_SIZE}",
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

        hydrated = self.get_diagnostics(ids)
        return [hydrated[did] for did in ids]


__all__ = [
    "DIAGNOSTIC_V2_COLUMNS",
    "DIAGNOSTIC_V2_COMPONENTS_TABLE",
    "DIAGNOSTIC_V2_COMPONENT_METRIC_LINKS_TABLE",
    "DIAGNOSTIC_V2_EVIDENCE_TABLE",
    "DIAGNOSTIC_V2_TABLE",
    "MAX_DIAGNOSTIC_V2_PAGE",
    "DiagnosticV2Error",
    "DiagnosticV2Store",
    "diagnostic_v2_tables_exist",
    "ensure_diagnostic_v2_tables",
]
