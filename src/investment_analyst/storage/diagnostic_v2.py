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

from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models.diagnostic import (
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticResult,
)
from investment_analyst.core.models.enums import (
    DataQuality,
    DiagnosticMode,
    DiagnosticVerdict,
    EvidenceDirection,
)
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)

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
            return BatchWriteReceipt(created_count=0, reused_count=0)

        # Verify all cited metrics first
        for item in diagnostics:
            if not isinstance(item, DiagnosticResult):
                raise DiagnosticV2Error("save_diagnostics requires DiagnosticResult instances")
            avail = item.available_at.astimezone(UTC)
            for comp in item.components:
                for mid in comp.metric_result_ids:
                    self._verify_metric_reference(mid, asset_id=item.asset_id, available_at=avail)
            for ev in item.evidence:
                self._verify_metric_reference(
                    ev.metric_result_id, asset_id=item.asset_id, available_at=avail
                )

        created_ids: list[UUID] = []
        reused_ids: list[UUID] = []

        for item in diagnostics:
            diag_id_str = str(item.diagnostic_id)
            existing_rows = self._connection.execute(
                f"SELECT {', '.join(DIAGNOSTIC_V2_COLUMNS)} "
                f"FROM {DIAGNOSTIC_V2_TABLE} WHERE diagnostic_id = ?",
                [diag_id_str],
            ).fetchall()

            if existing_rows:
                # Rehydrate and compare
                existing_diag = self.get_diagnostics([item.diagnostic_id])[item.diagnostic_id]
                # Compare semantic content exactly
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
                    continue
                raise RecordConflictError(f"diagnostic content conflict for {item.diagnostic_id}")

            # Insert diagnostic row
            self._connection.execute(
                f"""
                INSERT INTO {DIAGNOSTIC_V2_TABLE} (
                    diagnostic_id, asset_id, mode, verdict, final_score_text,
                    confidence_text, as_of, available_at, computed_at,
                    algorithm_version, summary, quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
                ],
            )

            # Insert components and component metric links
            for comp_pos, comp in enumerate(item.components):
                self._connection.execute(
                    f"""
                    INSERT INTO {DIAGNOSTIC_V2_COMPONENTS_TABLE} (
                        diagnostic_id, position, component_key, score_text,
                        weight_text, weighted_contribution_text, explanation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        diag_id_str,
                        comp_pos,
                        comp.component_key,
                        _decimal_text(comp.score),
                        _decimal_text(comp.weight),
                        _decimal_text(comp.weighted_contribution),
                        comp.explanation,
                    ],
                )
                for link_pos, mid in enumerate(comp.metric_result_ids):
                    self._connection.execute(
                        f"""
                        INSERT INTO {DIAGNOSTIC_V2_COMPONENT_METRIC_LINKS_TABLE} (
                            diagnostic_id, component_position, link_position, metric_result_id
                        ) VALUES (?, ?, ?, ?)
                        """,
                        [diag_id_str, comp_pos, link_pos, str(mid)],
                    )

            # Insert evidence
            for ev_pos, ev in enumerate(item.evidence):
                self._connection.execute(
                    f"""
                    INSERT INTO {DIAGNOSTIC_V2_EVIDENCE_TABLE} (
                        diagnostic_id, position, metric_result_id, direction,
                        contribution_text, reason
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        diag_id_str,
                        ev_pos,
                        str(ev.metric_result_id),
                        ev.direction.value if hasattr(ev.direction, "value") else str(ev.direction),
                        _decimal_text(ev.contribution),
                        ev.reason,
                    ],
                )

            created_ids.append(item.diagnostic_id)

        return BatchWriteReceipt(created_ids=tuple(created_ids), reused_ids=tuple(reused_ids))

    def get_diagnostics(self, diagnostic_ids: Collection[UUID]) -> dict[UUID, DiagnosticResult]:
        """Hydrate typed DiagnosticResults, verifying components, links and cited metrics."""
        if not diagnostic_ids:
            return {}
        results: dict[UUID, DiagnosticResult] = {}
        for did in diagnostic_ids:
            did_str = str(did)
            rows = self._connection.execute(
                f"SELECT {', '.join(DIAGNOSTIC_V2_COLUMNS)} "
                f"FROM {DIAGNOSTIC_V2_TABLE} WHERE diagnostic_id = ?",
                [did_str],
            ).fetchall()
            if not rows:
                raise RecordNotFoundError(f"diagnostic result {did} not found")
            row = rows[0]
            asset_id = str(row[1])
            mode = DiagnosticMode(str(row[2]))
            verdict = DiagnosticVerdict(str(row[3]))
            final_score = _parse_decimal_text(row[4])
            confidence = _parse_decimal_text(row[5])
            as_of = _parse_instant_text(row[6])
            available_at = _parse_instant_text(row[7])
            computed_at = _parse_instant_text(row[8])
            algorithm_version = str(row[9])
            summary = str(row[10])
            quality = DataQuality(str(row[11]))

            # Hydrate components in position order
            comp_rows = self._connection.execute(
                f"""
                SELECT position, component_key, score_text, weight_text,
                       weighted_contribution_text, explanation
                FROM {DIAGNOSTIC_V2_COMPONENTS_TABLE}
                WHERE diagnostic_id = ? ORDER BY position
                """,
                [did_str],
            ).fetchall()

            components: list[DiagnosticComponent] = []
            for comp_row in comp_rows:
                c_pos = int(comp_row[0])
                c_key = str(comp_row[1])
                c_score = _parse_decimal_text(comp_row[2])
                c_weight = _parse_decimal_text(comp_row[3])
                c_contrib = _parse_decimal_text(comp_row[4])
                c_expl = str(comp_row[5])

                # Get metric links for component
                link_rows = self._connection.execute(
                    f"""
                    SELECT link_position, metric_result_id
                    FROM {DIAGNOSTIC_V2_COMPONENT_METRIC_LINKS_TABLE}
                    WHERE diagnostic_id = ? AND component_position = ?
                    ORDER BY link_position
                    """,
                    [did_str, c_pos],
                ).fetchall()
                c_metric_ids: list[UUID] = []
                for l_pos, l_mid in link_rows:
                    if int(l_pos) != len(c_metric_ids):
                        raise DiagnosticV2Error("component metric link positions are corrupt")
                    c_metric_ids.append(UUID(str(l_mid)))
                    self._verify_metric_reference(
                        c_metric_ids[-1], asset_id=asset_id, available_at=available_at
                    )

                components.append(
                    DiagnosticComponent(
                        component_key=c_key,
                        score=c_score,
                        weight=c_weight,
                        weighted_contribution=c_contrib,
                        metric_result_ids=c_metric_ids,
                        explanation=c_expl,
                    )
                )

            # Hydrate evidence in position order
            ev_rows = self._connection.execute(
                f"""
                SELECT position, metric_result_id, direction, contribution_text, reason
                FROM {DIAGNOSTIC_V2_EVIDENCE_TABLE}
                WHERE diagnostic_id = ? ORDER BY position
                """,
                [did_str],
            ).fetchall()

            evidence: list[DiagnosticEvidence] = []
            for ev_pos, ev_mid, ev_dir, ev_contrib, ev_reason in ev_rows:
                if int(ev_pos) != len(evidence):
                    raise DiagnosticV2Error("diagnostic evidence positions are corrupt")
                ev_metric_id = UUID(str(ev_mid))
                self._verify_metric_reference(
                    ev_metric_id, asset_id=asset_id, available_at=available_at
                )
                evidence.append(
                    DiagnosticEvidence(
                        metric_result_id=ev_metric_id,
                        direction=EvidenceDirection(str(ev_dir)),
                        contribution=_parse_decimal_text(ev_contrib),
                        reason=str(ev_reason),
                    )
                )

            diag = DiagnosticResult(
                diagnostic_id=did,
                asset_id=asset_id,
                mode=mode,
                verdict=verdict,
                final_score=final_score,
                confidence=confidence,
                as_of=as_of,
                available_at=available_at,
                computed_at=computed_at,
                components=components,
                evidence=evidence,
                algorithm_version=algorithm_version,
                summary=summary,
                quality=quality,
            )
            results[did] = diag

        return results

    def list_diagnostics(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of: datetime | None = None,
        available_to: datetime | None = None,
    ) -> list[DiagnosticResult]:
        """List and hydrate diagnostic results in stable order."""
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
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT diagnostic_id FROM {DIAGNOSTIC_V2_TABLE}{where} "
            "ORDER BY available_at, diagnostic_id",
            parameters,
        ).fetchall()
        ids = [UUID(str(row[0])) for row in rows]
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
