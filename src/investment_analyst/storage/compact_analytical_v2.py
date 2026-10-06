"""Content-addressed, typed analytical persistence for workspace format v2."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from duckdb import DuckDBPyConnection

from investment_analyst.analytics.analysis_domain import (
    DomainMembershipError,
    validate_diagnostic_internal_consistency,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import (
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
)
from investment_analyst.core.models.base import ContractModel, UTCDateTime
from investment_analyst.storage.bounded_insert import write_transaction
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
    StorageSchemaError,
)
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

_BATCH = 256
_QUERY_CHUNK = 240
_ORIGINS = ("HISTORICAL", "LIVE")
_METRIC_TABLE = "workspace_metric_results_v2"
_DIAGNOSTIC_TABLE = "workspace_diagnostic_results_v2"
_CONTENT_TABLE = "workspace_analytical_content_v2"
_SEQUENCE_TABLE = "workspace_analytical_sequences_v2"
_SEGMENT_TABLE = "workspace_analytical_segments_v2"
_SEQUENCE_SEGMENT_TABLE = "workspace_analytical_sequence_segments_v2"
_SEGMENT_MEMBER_TABLE = "workspace_analytical_segment_members_v2"
_COMPONENT_TABLE = "workspace_analytical_components_v2"
_EVIDENCE_TABLE = "workspace_analytical_evidence_v2"
_SEAL_TABLE = "workspace_v2_historical_seal"
_COMPACT_TABLES = {
    _CONTENT_TABLE: {
        "content_id",
        "content_kind",
        "sha256",
        "byte_length",
        "value_bytes",
    },
    _SEQUENCE_TABLE: {"sequence_id", "link_type", "member_count", "checksum_sha256"},
    _SEGMENT_TABLE: {"segment_id", "link_type", "member_count", "checksum_sha256"},
    _SEQUENCE_SEGMENT_TABLE: {"sequence_id", "segment_position", "segment_id"},
    _SEGMENT_MEMBER_TABLE: {"segment_id", "member_position", "member_id"},
    _METRIC_TABLE: {
        "result_id",
        "origin",
        "asset_id",
        "metric_key",
        "value_text",
        "unit",
        "as_of",
        "available_at",
        "computed_at",
        "parameters_content_id",
        "observation_sequence_id",
        "metric_sequence_id",
        "algorithm_version",
        "quality",
        "id_version",
        "checksum_sha256",
        "inserted_at",
    },
    _DIAGNOSTIC_TABLE: {
        "diagnostic_id",
        "origin",
        "asset_id",
        "mode",
        "verdict",
        "final_score_text",
        "confidence_text",
        "as_of",
        "available_at",
        "computed_at",
        "algorithm_version",
        "summary_content_id",
        "quality",
        "component_count",
        "evidence_count",
        "checksum_sha256",
        "inserted_at",
    },
    _COMPONENT_TABLE: {
        "diagnostic_id",
        "position",
        "component_key",
        "score_text",
        "weight_text",
        "weighted_contribution_text",
        "metric_sequence_id",
        "explanation_content_id",
    },
    _EVIDENCE_TABLE: {
        "diagnostic_id",
        "position",
        "metric_result_id",
        "direction",
        "contribution_text",
        "reason_content_id",
    },
    _SEAL_TABLE: {
        "seal_key",
        "source_fingerprint",
        "metric_count",
        "metric_digest",
        "diagnostic_count",
        "diagnostic_digest",
        "sealed_at",
    },
}

_METRIC_COLUMNS = (
    "result_id",
    "origin",
    "asset_id",
    "metric_key",
    "value_text",
    "unit",
    "as_of",
    "available_at",
    "computed_at",
    "parameters_content_id",
    "observation_sequence_id",
    "metric_sequence_id",
    "algorithm_version",
    "quality",
    "id_version",
    "checksum_sha256",
)
_DIAGNOSTIC_COLUMNS = (
    "diagnostic_id",
    "origin",
    "asset_id",
    "mode",
    "verdict",
    "final_score_text",
    "confidence_text",
    "as_of",
    "available_at",
    "computed_at",
    "algorithm_version",
    "summary_content_id",
    "quality",
    "component_count",
    "evidence_count",
    "checksum_sha256",
)
_METRIC_SELECT_COLUMNS = (
    "result_id, origin, asset_id, metric_key, value_text, unit, "
    "CAST(as_of AS VARCHAR), CAST(available_at AS VARCHAR), CAST(computed_at AS VARCHAR), "
    "parameters_content_id, observation_sequence_id, metric_sequence_id, "
    "algorithm_version, quality, id_version, checksum_sha256"
)
_DIAGNOSTIC_SELECT_COLUMNS = (
    "diagnostic_id, origin, asset_id, mode, verdict, final_score_text, confidence_text, "
    "CAST(as_of AS VARCHAR), CAST(available_at AS VARCHAR), CAST(computed_at AS VARCHAR), "
    "algorithm_version, summary_content_id, quality, component_count, evidence_count, "
    "checksum_sha256"
)


class CompactAnalyticalError(StorageError):
    """A compact analytical row, link, content object or seal is invalid."""


class HistoricalAnalyticalSeal(ContractModel):
    """Immutable source inventory recorded after a complete historical import."""

    model_config = {"extra": "forbid", "frozen": True, "strict": True}

    source_fingerprint: str
    metric_count: int
    metric_digest: str
    diagnostic_count: int
    diagnostic_digest: str
    sealed_at: UTCDateTime


def _table_columns(connection: DuckDBPyConnection, table: str) -> set[str]:
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ?",
        [table],
    ).fetchall()
    return {str(row[0]) for row in rows}


def compact_analytical_tables_exist(connection: DuckDBPyConnection) -> bool:
    """Return whether the complete compact analytical schema is present."""
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    present = {str(row[0]) for row in rows}.intersection(_COMPACT_TABLES)
    if present and present != set(_COMPACT_TABLES):
        raise CompactAnalyticalError("workspace v2 analytical schema is partial")
    return bool(present)


def ensure_compact_analytical_tables(connection: DuckDBPyConnection, *, create: bool) -> None:
    """Validate every typed table and reject document-shaped analytical rows."""
    present = compact_analytical_tables_exist(connection)
    if not present:
        if not create:
            raise StorageSchemaError("workspace v2 analytical tables are missing")
        raise StorageSchemaError("workspace v2 migration did not create analytical tables")
    for table, expected in _COMPACT_TABLES.items():
        columns = _table_columns(connection, table)
        if columns != expected:
            raise StorageSchemaError(f"workspace v2 table {table!r} is incompatible")
        if "document_json" in columns:
            raise StorageSchemaError("workspace v2 analytical tables cannot store document_json")


def _digest_parts(prefix: str, parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    digest.update(prefix.encode("ascii"))
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _sequence_identity(link_type: str, identifiers: Sequence[UUID]) -> str:
    return _digest_parts(
        "workspace-analytical-sequence-v2",
        (link_type, *(str(identifier) for identifier in identifiers)),
    )


def _segment_identity(link_type: str, identifiers: Sequence[UUID]) -> str:
    return _digest_parts(
        "workspace-analytical-segment-v2",
        (link_type, *(str(identifier) for identifier in identifiers)),
    )


def _sequence_checksum(link_type: str, identifiers: Sequence[UUID]) -> str:
    return _digest_parts(
        "workspace-analytical-sequence-members-v2",
        (link_type, *(str(identifier) for identifier in identifiers)),
    )


def _content_identity(kind: str, payload: bytes) -> tuple[str, str]:
    digest = sha256_hex(payload)
    content_id = sha256_hex(f"workspace-analytical-content-v2:{kind}:{digest}".encode("ascii"))
    return content_id, digest


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CompactAnalyticalError("analytical Decimal must be finite")
    return str(value)


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CompactAnalyticalError("stored analytical Decimal is invalid") from error
    if not result.is_finite():
        raise CompactAnalyticalError("stored analytical Decimal is not finite")
    return result


def _instant(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as error:
            raise CompactAnalyticalError("stored analytical instant is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CompactAnalyticalError("stored analytical instant is not timezone-aware")
    return parsed.astimezone(UTC)


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CompactAnalyticalError("analytical JSON content is invalid") from error


def _chunks[T](values: Sequence[T], size: int = _BATCH) -> Iterable[Sequence[T]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _executemany_bounded(
    connection: DuckDBPyConnection,
    statement: str,
    rows: Sequence[Sequence[object]],
) -> None:
    for chunk in _chunks(rows):
        connection.executemany(statement, chunk)


def _id_query(
    connection: DuckDBPyConnection,
    *,
    table: str,
    key_column: str,
    columns: str,
    identifiers: Sequence[str],
) -> list[tuple[object, ...]]:
    output: list[tuple[object, ...]] = []
    for chunk in _chunks(identifiers, _QUERY_CHUNK):
        placeholders = ", ".join("?" for _ in chunk)
        output.extend(
            connection.execute(
                f"SELECT {columns} FROM {table} WHERE {key_column} IN ({placeholders})",
                list(chunk),
            ).fetchall()
        )
    return output


class CompactAnalyticalStore:
    """Store full-fidelity results as typed columns and shared content/links."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def ensure(self, *, create: bool) -> None:
        ensure_compact_analytical_tables(self._connection, create=create)

    def count_metrics(self, *, origin: Literal["HISTORICAL", "LIVE"] | None = None) -> int:
        self.ensure(create=False)
        if origin is None:
            row = self._connection.execute(f"SELECT count(*) FROM {_METRIC_TABLE}").fetchone()
        else:
            row = self._connection.execute(
                f"SELECT count(*) FROM {_METRIC_TABLE} WHERE origin = ?", [origin]
            ).fetchone()
        if row is None:
            raise CompactAnalyticalError("workspace v2 metric count returned no row")
        return int(row[0])

    def select_metric_ids(
        self,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        metric_keys: Collection[str] | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
        origin: Literal["HISTORICAL", "LIVE"] | None = None,
    ) -> list[UUID]:
        self.ensure(create=False)
        if metric_key is not None and metric_keys is not None:
            raise ValueError("metric_key and metric_keys cannot be used together")
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if metric_key is not None:
            clauses.append("metric_key = ?")
            parameters.append(metric_key)
        if metric_keys is not None:
            ordered_keys = tuple(sorted(set(metric_keys)))
            if not ordered_keys:
                return []
            clauses.append(f"metric_key IN ({', '.join('?' for _ in ordered_keys)})")
            parameters.extend(ordered_keys)
        for column, value, operator in (
            ("as_of", as_of_from, ">="),
            ("as_of", as_of_to, "<="),
            ("available_at", available_from, ">="),
            ("available_at", available_to, "<="),
        ):
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("metric query instants must be timezone-aware")
                clauses.append(f"{column} {operator} CAST(? AS TIMESTAMPTZ)")
                parameters.append(value.astimezone(UTC).isoformat())
        if origin is not None:
            clauses.append("origin = ?")
            parameters.append(origin)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT result_id FROM {_METRIC_TABLE}{where} ORDER BY as_of, result_id",
            parameters,
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def count_metric_results(
        self,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        metric_keys: Collection[str] | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
        origin: Literal["HISTORICAL", "LIVE"] | None = None,
    ) -> int:
        """Count filtered results from typed index columns without hydration."""
        self.ensure(create=False)
        if metric_key is not None and metric_keys is not None:
            raise ValueError("metric_key and metric_keys cannot be used together")
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if metric_key is not None:
            clauses.append("metric_key = ?")
            parameters.append(metric_key)
        if metric_keys is not None:
            values = tuple(sorted(set(metric_keys)))
            if not values:
                return 0
            clauses.append(f"metric_key IN ({', '.join('?' for _ in values)})")
            parameters.extend(values)
        for column, value, operator in (
            ("as_of", as_of_from, ">="),
            ("as_of", as_of_to, "<="),
            ("available_at", available_from, ">="),
            ("available_at", available_to, "<="),
        ):
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("metric query instants must be timezone-aware")
                clauses.append(f"{column} {operator} CAST(? AS TIMESTAMPTZ)")
                parameters.append(value.astimezone(UTC).isoformat())
        if origin is not None:
            clauses.append("origin = ?")
            parameters.append(origin)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._connection.execute(
            f"SELECT count(*) FROM {_METRIC_TABLE}{where}", parameters
        ).fetchone()
        if row is None:
            raise CompactAnalyticalError("workspace v2 metric count returned no row")
        return int(row[0])

    def count_diagnostics(self, *, origin: Literal["HISTORICAL", "LIVE"] | None = None) -> int:
        self.ensure(create=False)
        if origin is None:
            row = self._connection.execute(f"SELECT count(*) FROM {_DIAGNOSTIC_TABLE}").fetchone()
        else:
            row = self._connection.execute(
                f"SELECT count(*) FROM {_DIAGNOSTIC_TABLE} WHERE origin = ?", [origin]
            ).fetchone()
        if row is None:
            raise CompactAnalyticalError("workspace v2 diagnostic count returned no row")
        return int(row[0])

    def select_diagnostic_ids(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
        origin: Literal["HISTORICAL", "LIVE"] | None = None,
    ) -> list[UUID]:
        self.ensure(create=False)
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if mode is not None:
            clauses.append("mode = ?")
            parameters.append(mode.value)
        for column, value, operator in (
            ("as_of", as_of_from, ">="),
            ("as_of", as_of_to, "<="),
            ("available_at", available_from, ">="),
            ("available_at", available_to, "<="),
        ):
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("diagnostic query instants must be timezone-aware")
                clauses.append(f"{column} {operator} CAST(? AS TIMESTAMPTZ)")
                parameters.append(value.astimezone(UTC).isoformat())
        if origin is not None:
            clauses.append("origin = ?")
            parameters.append(origin)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT diagnostic_id FROM {_DIAGNOSTIC_TABLE}{where} "
            "ORDER BY available_at, diagnostic_id",
            parameters,
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def count_diagnostic_results(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
        origin: Literal["HISTORICAL", "LIVE"] | None = None,
    ) -> int:
        """Count filtered diagnostics from typed index columns without hydration."""
        self.ensure(create=False)
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if mode is not None:
            clauses.append("mode = ?")
            parameters.append(mode.value)
        for column, value, operator in (
            ("as_of", as_of_from, ">="),
            ("as_of", as_of_to, "<="),
            ("available_at", available_from, ">="),
            ("available_at", available_to, "<="),
        ):
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("diagnostic query instants must be timezone-aware")
                clauses.append(f"{column} {operator} CAST(? AS TIMESTAMPTZ)")
                parameters.append(value.astimezone(UTC).isoformat())
        if origin is not None:
            clauses.append("origin = ?")
            parameters.append(origin)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._connection.execute(
            f"SELECT count(*) FROM {_DIAGNOSTIC_TABLE}{where}", parameters
        ).fetchone()
        if row is None:
            raise CompactAnalyticalError("workspace v2 diagnostic count returned no row")
        return int(row[0])

    def list_metric_ids_page(
        self,
        *,
        limit: int = _BATCH,
        after_available_at: datetime | None = None,
        after_result_id: UUID | None = None,
        origin: Literal["HISTORICAL", "LIVE"] | None = None,
    ) -> list[UUID]:
        self._validate_page(limit, after_available_at, after_result_id)
        clauses: list[str] = []
        parameters: list[object] = []
        if (after_available_at is None) != (after_result_id is None):
            raise ValueError("metric page cursor requires both fields")
        if after_available_at is not None and after_result_id is not None:
            clauses.append("(available_at, result_id) > (CAST(? AS TIMESTAMPTZ), ?)")
            parameters.extend(
                [after_available_at.astimezone(UTC).isoformat(), str(after_result_id)]
            )
        if origin is not None:
            clauses.append("origin = ?")
            parameters.append(origin)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT result_id FROM {_METRIC_TABLE}{where} "
            "ORDER BY available_at, result_id LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def list_diagnostic_ids_page(
        self,
        *,
        limit: int = _BATCH,
        after_available_at: datetime | None = None,
        after_diagnostic_id: UUID | None = None,
        origin: Literal["HISTORICAL", "LIVE"] | None = None,
    ) -> list[UUID]:
        self._validate_page(limit, after_available_at, after_diagnostic_id)
        clauses: list[str] = []
        parameters: list[object] = []
        if (after_available_at is None) != (after_diagnostic_id is None):
            raise ValueError("diagnostic page cursor requires both fields")
        if after_available_at is not None and after_diagnostic_id is not None:
            clauses.append("(available_at, diagnostic_id) > (CAST(? AS TIMESTAMPTZ), ?)")
            parameters.extend(
                [after_available_at.astimezone(UTC).isoformat(), str(after_diagnostic_id)]
            )
        if origin is not None:
            clauses.append("origin = ?")
            parameters.append(origin)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT diagnostic_id FROM {_DIAGNOSTIC_TABLE}{where} "
            "ORDER BY available_at, diagnostic_id LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    @staticmethod
    def _validate_page(
        limit: int, after_available_at: datetime | None, identifier: UUID | None
    ) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _BATCH:
            raise ValueError("workspace v2 analytical page limit must be from 1 to 256")
        if after_available_at is not None and (
            after_available_at.tzinfo is None or after_available_at.utcoffset() is None
        ):
            raise ValueError("workspace v2 cursor must be timezone-aware")

    def save_metrics(
        self,
        results: Collection[MetricResult],
        *,
        origin: Literal["HISTORICAL", "LIVE"] = "LIVE",
    ) -> BatchWriteReceipt:
        typed = tuple(results)
        if origin not in _ORIGINS:
            raise ValueError("workspace v2 metric origin is invalid")
        if len(typed) > _BATCH or any(not isinstance(item, MetricResult) for item in typed):
            raise CompactAnalyticalError("metric writes are limited to 256 typed rows")
        if not typed:
            return BatchWriteReceipt()
        checksums: dict[UUID, str] = {}
        unique: dict[UUID, MetricResult] = {}
        for item in typed:
            checksum = sha256_hex(canonical_json_bytes(item))
            if item.result_id in checksums and checksums[item.result_id] != checksum:
                raise RecordConflictError("metric identifier has conflicting content in one batch")
            checksums[item.result_id] = checksum
            unique[item.result_id] = item
            self._validate_metric_identity(item)

        existing_rows = _id_query(
            self._connection,
            table=_METRIC_TABLE,
            key_column="result_id",
            columns="result_id",
            identifiers=[str(identifier) for identifier in unique],
        )
        existing_ids = {UUID(str(row[0])) for row in existing_rows}
        existing_models = self.get_metrics(existing_ids) if existing_ids else {}
        for identifier, existing in existing_models.items():
            if existing != unique[identifier]:
                raise RecordConflictError(f"metric identifier {identifier} has conflicting content")
        new_items = [item for identifier, item in unique.items() if identifier not in existing_ids]
        if origin == "HISTORICAL" and new_items and self.historical_seal() is not None:
            raise CompactAnalyticalError("sealed historical analytical rows are immutable")
        if origin == "LIVE" and new_items:
            self._require_metric_inputs_visible(new_items)

        if new_items:
            content_requests: list[tuple[str, bytes]] = [
                ("parameters", _json_bytes(item.parameters)) for item in new_items
            ]
            sequence_requests: list[tuple[str, tuple[UUID, ...]]] = []
            for item in new_items:
                sequence_requests.append(
                    ("metric-input-observation-v1", tuple(item.input_observation_ids))
                )
                sequence_requests.append(
                    ("metric-input-result-v1", tuple(item.input_metric_result_ids))
                )
            with write_transaction(self._connection):
                content_ids = self._ensure_content_batch(content_requests)
                sequence_ids = self._ensure_sequences_batch(sequence_requests)
                content_cursor = iter(content_ids)
                sequence_cursor = iter(sequence_ids)
                rows = [
                    (
                        str(item.result_id),
                        origin,
                        item.asset_id,
                        item.metric_key,
                        _decimal_text(item.value),
                        item.unit,
                        item.as_of.astimezone(UTC),
                        item.available_at.astimezone(UTC),
                        item.computed_at.astimezone(UTC),
                        next(content_cursor),
                        next(sequence_cursor),
                        next(sequence_cursor),
                        item.algorithm_version,
                        item.quality.value,
                        item.result_id.version or 0,
                        checksums[item.result_id],
                    )
                    for item in new_items
                ]
                _executemany_bounded(
                    self._connection,
                    f"INSERT INTO {_METRIC_TABLE} ({', '.join(_METRIC_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _METRIC_COLUMNS)})",
                    rows,
                )
        created_ids = tuple(item.result_id for item in new_items)
        reused_ids = tuple(identifier for identifier in unique if identifier in existing_ids)
        return BatchWriteReceipt(created_ids=created_ids, reused_ids=reused_ids)

    def get_metrics(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]:
        ordered = tuple(sorted(set(result_ids), key=str))
        if not ordered:
            return {}
        if len(ordered) > _BATCH:
            raise ValueError("workspace v2 metric lookup is limited to 256 IDs")
        rows = _id_query(
            self._connection,
            table=_METRIC_TABLE,
            key_column="result_id",
            columns=_METRIC_SELECT_COLUMNS,
            identifiers=[str(identifier) for identifier in ordered],
        )
        indexed = {UUID(str(row[0])): row for row in rows}
        missing = [identifier for identifier in ordered if identifier not in indexed]
        if missing:
            raise RecordNotFoundError(f"workspace v2 metric {missing[0]} was not found")
        content_ids = [str(indexed[identifier][9]) for identifier in ordered]
        content = self._load_content_batch(content_ids, expected_kind="parameters")
        sequence_ids = [str(indexed[identifier][10]) for identifier in ordered] + [
            str(indexed[identifier][11]) for identifier in ordered
        ]
        sequences = self._load_sequences(sequence_ids)
        output: dict[UUID, MetricResult] = {}
        for identifier in ordered:
            row = indexed[identifier]
            parameter_bytes = content[str(row[9])]
            observation_sequence = sequences[str(row[10])]
            metric_sequence = sequences[str(row[11])]
            if observation_sequence[0] != "metric-input-observation-v1":
                raise CompactAnalyticalError("metric observation link type is corrupt")
            if metric_sequence[0] != "metric-input-result-v1":
                raise CompactAnalyticalError("metric result link type is corrupt")
            try:
                parameters = json.loads(parameter_bytes.decode("utf-8"))
                if not isinstance(parameters, dict):
                    raise ValueError("metric parameters must be an object")
                result = MetricResult(
                    result_id=identifier,
                    asset_id=str(row[2]),
                    metric_key=str(row[3]),
                    value=_decimal(row[4]),
                    unit=str(row[5]),
                    as_of=_instant(row[6]),
                    available_at=_instant(row[7]),
                    computed_at=_instant(row[8]),
                    parameters=parameters,
                    input_observation_ids=list(observation_sequence[1]),
                    input_metric_result_ids=list(metric_sequence[1]),
                    algorithm_version=str(row[12]),
                    quality=DataQuality(str(row[13])),
                )
            except (UnicodeDecodeError, ValueError, TypeError) as error:
                raise CompactAnalyticalError("workspace v2 metric row is invalid") from error
            if (identifier.version or 0) != int(row[14]):
                raise CompactAnalyticalError("workspace v2 metric UUID version was altered")
            self._validate_metric_identity(result)
            if sha256_hex(canonical_json_bytes(result)) != str(row[15]):
                raise CompactAnalyticalError("workspace v2 metric checksum does not match")
            output[identifier] = result
        return output

    def get_existing_metrics(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]:
        ordered = tuple(sorted(set(result_ids), key=str))
        output: dict[UUID, MetricResult] = {}
        for chunk in _chunks(ordered):
            rows = _id_query(
                self._connection,
                table=_METRIC_TABLE,
                key_column="result_id",
                columns="result_id",
                identifiers=[str(identifier) for identifier in chunk],
            )
            present = tuple(UUID(str(row[0])) for row in rows)
            if present:
                output.update(self.get_metrics(present))
        return output

    def save_diagnostics(
        self,
        results: Collection[DiagnosticResult],
        *,
        origin: Literal["HISTORICAL", "LIVE"] = "LIVE",
    ) -> BatchWriteReceipt:
        typed = tuple(results)
        if origin not in _ORIGINS:
            raise ValueError("workspace v2 diagnostic origin is invalid")
        if len(typed) > _BATCH or any(not isinstance(item, DiagnosticResult) for item in typed):
            raise CompactAnalyticalError("diagnostic writes are limited to 256 typed rows")
        if not typed:
            return BatchWriteReceipt()
        checksums: dict[UUID, str] = {}
        unique: dict[UUID, DiagnosticResult] = {}
        for item in typed:
            checksum = sha256_hex(canonical_json_bytes(item))
            if item.diagnostic_id in checksums and checksums[item.diagnostic_id] != checksum:
                raise RecordConflictError(
                    "diagnostic identifier has conflicting content in one batch"
                )
            checksums[item.diagnostic_id] = checksum
            unique[item.diagnostic_id] = item

        existing_rows = _id_query(
            self._connection,
            table=_DIAGNOSTIC_TABLE,
            key_column="diagnostic_id",
            columns="diagnostic_id",
            identifiers=[str(identifier) for identifier in unique],
        )
        existing_ids = {UUID(str(row[0])) for row in existing_rows}
        existing_models = self.get_diagnostics(existing_ids) if existing_ids else {}
        for identifier, existing in existing_models.items():
            if existing != unique[identifier]:
                raise RecordConflictError(
                    f"diagnostic identifier {identifier} has conflicting content"
                )
        new_items = [item for identifier, item in unique.items() if identifier not in existing_ids]
        if origin == "HISTORICAL" and new_items and self.historical_seal() is not None:
            raise CompactAnalyticalError("sealed historical analytical rows are immutable")
        if origin == "LIVE" and new_items:
            self._require_diagnostic_metrics_visible(new_items, validate_domain=True)

        if new_items:
            content_requests: list[tuple[str, bytes]] = []
            sequence_requests: list[tuple[str, tuple[UUID, ...]]] = []
            for item in new_items:
                content_requests.append(("summary", item.summary.encode("utf-8")))
                for component in item.components:
                    content_requests.append(
                        ("component-explanation", component.explanation.encode("utf-8"))
                    )
                    sequence_requests.append(
                        (
                            "diagnostic-component-metric-result-v1",
                            tuple(component.metric_result_ids),
                        )
                    )
                for evidence in item.evidence:
                    content_requests.append(("evidence-reason", evidence.reason.encode("utf-8")))
            with write_transaction(self._connection):
                content_ids = self._ensure_content_batch(content_requests)
                sequence_ids = self._ensure_sequences_batch(sequence_requests)
                content_cursor = iter(content_ids)
                sequence_cursor = iter(sequence_ids)
                parent_rows: list[tuple[object, ...]] = []
                component_rows: list[tuple[object, ...]] = []
                evidence_rows: list[tuple[object, ...]] = []
                for item in new_items:
                    parent_rows.append(
                        (
                            str(item.diagnostic_id),
                            origin,
                            item.asset_id,
                            item.mode.value,
                            item.verdict.value,
                            _decimal_text(item.final_score),
                            _decimal_text(item.confidence),
                            item.as_of.astimezone(UTC),
                            item.available_at.astimezone(UTC),
                            item.computed_at.astimezone(UTC),
                            item.algorithm_version,
                            next(content_cursor),
                            item.quality.value,
                            len(item.components),
                            len(item.evidence),
                            checksums[item.diagnostic_id],
                        )
                    )
                    for position, component in enumerate(item.components):
                        component_rows.append(
                            (
                                str(item.diagnostic_id),
                                position,
                                component.component_key,
                                _decimal_text(component.score),
                                _decimal_text(component.weight),
                                _decimal_text(component.weighted_contribution),
                                next(sequence_cursor),
                                next(content_cursor),
                            )
                        )
                    for position, evidence in enumerate(item.evidence):
                        evidence_rows.append(
                            (
                                str(item.diagnostic_id),
                                position,
                                str(evidence.metric_result_id),
                                evidence.direction.value,
                                _decimal_text(evidence.contribution),
                                next(content_cursor),
                            )
                        )
                _executemany_bounded(
                    self._connection,
                    f"INSERT INTO {_DIAGNOSTIC_TABLE} ({', '.join(_DIAGNOSTIC_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _DIAGNOSTIC_COLUMNS)})",
                    parent_rows,
                )
                _executemany_bounded(
                    self._connection,
                    f"INSERT INTO {_COMPONENT_TABLE} VALUES ({', '.join('?' for _ in range(8))})",
                    component_rows,
                )
                _executemany_bounded(
                    self._connection,
                    f"INSERT INTO {_EVIDENCE_TABLE} VALUES ({', '.join('?' for _ in range(6))})",
                    evidence_rows,
                )
        created_ids = tuple(item.diagnostic_id for item in new_items)
        reused_ids = tuple(identifier for identifier in unique if identifier in existing_ids)
        return BatchWriteReceipt(created_ids=created_ids, reused_ids=reused_ids)

    def get_diagnostics(self, diagnostic_ids: Collection[UUID]) -> dict[UUID, DiagnosticResult]:
        ordered = tuple(sorted(set(diagnostic_ids), key=str))
        if not ordered:
            return {}
        if len(ordered) > _BATCH:
            raise ValueError("workspace v2 diagnostic lookup is limited to 256 IDs")
        rows = _id_query(
            self._connection,
            table=_DIAGNOSTIC_TABLE,
            key_column="diagnostic_id",
            columns=_DIAGNOSTIC_SELECT_COLUMNS,
            identifiers=[str(identifier) for identifier in ordered],
        )
        indexed = {UUID(str(row[0])): row for row in rows}
        missing = [identifier for identifier in ordered if identifier not in indexed]
        if missing:
            raise RecordNotFoundError(f"workspace v2 diagnostic {missing[0]} was not found")

        keys = [str(identifier) for identifier in ordered]
        component_rows = self._children(_COMPONENT_TABLE, keys)
        evidence_rows = self._children(_EVIDENCE_TABLE, keys)
        if len(component_rows) != sum(int(indexed[item][13]) for item in ordered):
            raise CompactAnalyticalError("workspace v2 diagnostic components are incomplete")
        if len(evidence_rows) != sum(int(indexed[item][14]) for item in ordered):
            raise CompactAnalyticalError("workspace v2 diagnostic evidence is incomplete")

        component_sequence_ids = [str(row[6]) for row in component_rows]
        sequences = self._load_sequences(component_sequence_ids)
        summaries = self._load_content_batch(
            [str(indexed[item][11]) for item in ordered],
            expected_kind="summary",
        )
        explanations = self._load_content_batch(
            [str(row[7]) for row in component_rows],
            expected_kind="component-explanation",
        )
        reasons = self._load_content_batch(
            [str(row[5]) for row in evidence_rows],
            expected_kind="evidence-reason",
        )

        components_by_id: dict[UUID, list[DiagnosticComponent]] = defaultdict(list)
        evidence_by_id: dict[UUID, list[DiagnosticEvidence]] = defaultdict(list)
        component_positions: dict[UUID, list[int]] = defaultdict(list)
        evidence_positions: dict[UUID, list[int]] = defaultdict(list)
        for row in component_rows:
            diagnostic_id = UUID(str(row[0]))
            position = int(row[1])
            component_positions[diagnostic_id].append(position)
            sequence = sequences[str(row[6])]
            if sequence[0] != "diagnostic-component-metric-result-v1":
                raise CompactAnalyticalError("diagnostic component link type is corrupt")
            explanation = explanations[str(row[7])]
            try:
                components_by_id[diagnostic_id].append(
                    DiagnosticComponent(
                        component_key=str(row[2]),
                        score=_decimal(row[3]),
                        weight=_decimal(row[4]),
                        weighted_contribution=_decimal(row[5]),
                        metric_result_ids=list(sequence[1]),
                        explanation=explanation.decode("utf-8"),
                    )
                )
            except (UnicodeDecodeError, ValueError, TypeError) as error:
                raise CompactAnalyticalError(
                    "workspace v2 diagnostic component is invalid"
                ) from error
        for row in evidence_rows:
            diagnostic_id = UUID(str(row[0]))
            position = int(row[1])
            evidence_positions[diagnostic_id].append(position)
            try:
                evidence_by_id[diagnostic_id].append(
                    DiagnosticEvidence(
                        metric_result_id=UUID(str(row[2])),
                        direction=EvidenceDirection(str(row[3])),
                        contribution=_decimal(row[4]),
                        reason=reasons[str(row[5])].decode("utf-8"),
                    )
                )
            except (UnicodeDecodeError, ValueError, TypeError) as error:
                raise CompactAnalyticalError(
                    "workspace v2 diagnostic evidence is invalid"
                ) from error

        output: dict[UUID, DiagnosticResult] = {}
        for identifier in ordered:
            row = indexed[identifier]
            if component_positions[identifier] != list(range(int(row[13]))):
                raise CompactAnalyticalError("workspace v2 diagnostic component order is corrupt")
            if evidence_positions[identifier] != list(range(int(row[14]))):
                raise CompactAnalyticalError("workspace v2 diagnostic evidence order is corrupt")
            try:
                result = DiagnosticResult(
                    diagnostic_id=identifier,
                    asset_id=str(row[2]),
                    mode=DiagnosticMode(str(row[3])),
                    verdict=DiagnosticVerdict(str(row[4])),
                    final_score=_decimal(row[5]),
                    confidence=_decimal(row[6]),
                    as_of=_instant(row[7]),
                    available_at=_instant(row[8]),
                    computed_at=_instant(row[9]),
                    algorithm_version=str(row[10]),
                    summary=summaries[str(row[11])].decode("utf-8"),
                    quality=DataQuality(str(row[12])),
                    components=components_by_id[identifier],
                    evidence=evidence_by_id[identifier],
                )
            except (UnicodeDecodeError, ValueError, TypeError) as error:
                raise CompactAnalyticalError("workspace v2 diagnostic row is invalid") from error
            if sha256_hex(canonical_json_bytes(result)) != str(row[15]):
                raise CompactAnalyticalError("workspace v2 diagnostic checksum does not match")
            output[identifier] = result
        return output

    def get_existing_diagnostics(
        self, diagnostic_ids: Collection[UUID]
    ) -> dict[UUID, DiagnosticResult]:
        ordered = tuple(sorted(set(diagnostic_ids), key=str))
        output: dict[UUID, DiagnosticResult] = {}
        for chunk in _chunks(ordered):
            rows = _id_query(
                self._connection,
                table=_DIAGNOSTIC_TABLE,
                key_column="diagnostic_id",
                columns="diagnostic_id",
                identifiers=[str(identifier) for identifier in chunk],
            )
            present = tuple(UUID(str(row[0])) for row in rows)
            if present:
                output.update(self.get_diagnostics(present))
        return output

    def _children(self, table: str, diagnostic_ids: Sequence[str]) -> list[tuple[object, ...]]:
        output: list[tuple[object, ...]] = []
        columns = (
            "diagnostic_id, position, component_key, score_text, weight_text, "
            "weighted_contribution_text, metric_sequence_id, explanation_content_id"
            if table == _COMPONENT_TABLE
            else "diagnostic_id, position, metric_result_id, direction, "
            "contribution_text, reason_content_id"
        )
        for chunk in _chunks(diagnostic_ids, _QUERY_CHUNK):
            placeholders = ", ".join("?" for _ in chunk)
            output.extend(
                self._connection.execute(
                    f"SELECT {columns} FROM {table} "
                    f"WHERE diagnostic_id IN ({placeholders}) ORDER BY diagnostic_id, position",
                    list(chunk),
                ).fetchall()
            )
        return output

    @staticmethod
    def _validate_metric_identity(result: MetricResult) -> None:
        if result.result_id.version != 8:
            return
        from investment_analyst.storage.metric_v2 import recalculate_metric_result_id

        try:
            expected = recalculate_metric_result_id(result)
        except (TypeError, ValueError) as error:
            raise CompactAnalyticalError("UUIDv8 metric identity cannot be verified") from error
        if expected != result.result_id:
            raise CompactAnalyticalError("UUIDv8 metric identity does not match its content")

    def _ensure_content_batch(self, requests: Sequence[tuple[str, bytes]]) -> list[str]:
        entries: dict[str, tuple[str, str, int, bytes]] = {}
        request_ids: list[str] = []
        for kind, payload in requests:
            content_id, digest = _content_identity(kind, payload)
            row = (kind, digest, len(payload), payload)
            previous = entries.get(content_id)
            if previous is not None and previous != row:
                raise CompactAnalyticalError("content address collision detected")
            entries[content_id] = row
            request_ids.append(content_id)
        existing_rows = _id_query(
            self._connection,
            table=_CONTENT_TABLE,
            key_column="content_id",
            columns="content_id, content_kind, sha256, byte_length, value_bytes",
            identifiers=list(entries),
        )
        existing = {str(row[0]): row[1:] for row in existing_rows}
        inserts: list[tuple[object, ...]] = []
        for content_id, (kind, digest, length, payload) in entries.items():
            if content_id in existing:
                previous = existing[content_id]
                if (
                    str(previous[0]) != kind
                    or str(previous[1]) != digest
                    or int(previous[2]) != length
                    or bytes(previous[3]) != payload
                ):
                    raise CompactAnalyticalError("content-addressed bytes do not match their key")
            else:
                inserts.append((content_id, kind, digest, length, payload))
        if inserts:
            _executemany_bounded(
                self._connection,
                f"INSERT INTO {_CONTENT_TABLE} VALUES (?, ?, ?, ?, ?)",
                inserts,
            )
        return request_ids

    def _load_content_batch(
        self,
        content_ids: Sequence[str],
        *,
        expected_kind: str | None = None,
    ) -> dict[str, bytes]:
        unique_ids = tuple(dict.fromkeys(content_ids))
        if not unique_ids:
            return {}
        rows = _id_query(
            self._connection,
            table=_CONTENT_TABLE,
            key_column="content_id",
            columns="content_id, content_kind, sha256, byte_length, value_bytes",
            identifiers=list(unique_ids),
        )
        indexed = {str(row[0]): row[1:] for row in rows}
        missing = [identifier for identifier in unique_ids if identifier not in indexed]
        if missing:
            raise CompactAnalyticalError("analytical content reference is missing")
        output: dict[str, bytes] = {}
        for content_id in unique_ids:
            kind, digest, length, raw = indexed[content_id]
            payload = bytes(raw)
            actual_id, actual_digest = _content_identity(str(kind), payload)
            if (
                actual_id != content_id
                or actual_digest != str(digest)
                or len(payload) != int(length)
                or (expected_kind is not None and str(kind) != expected_kind)
            ):
                raise CompactAnalyticalError("analytical content failed digest or type validation")
            output[content_id] = payload
        return output

    def _ensure_sequences_batch(
        self, requests: Sequence[tuple[str, tuple[UUID, ...]]]
    ) -> list[str]:
        sequences: dict[str, tuple[str, tuple[UUID, ...], str]] = {}
        request_ids: list[str] = []
        for link_type, identifiers in requests:
            key = _sequence_identity(link_type, identifiers)
            checksum = _sequence_checksum(link_type, identifiers)
            value = (link_type, tuple(identifiers), checksum)
            previous = sequences.get(key)
            if previous is not None and previous != value:
                raise CompactAnalyticalError("sequence address collision detected")
            sequences[key] = value
            request_ids.append(key)

        existing_rows = _id_query(
            self._connection,
            table=_SEQUENCE_TABLE,
            key_column="sequence_id",
            columns="sequence_id",
            identifiers=list(sequences),
        )
        existing_ids = {str(row[0]) for row in existing_rows}
        if existing_ids:
            stored = self._load_sequences(tuple(existing_ids))
            for sequence_id in existing_ids:
                link_type, identifiers, _checksum = sequences[sequence_id]
                if stored[sequence_id] != (link_type, identifiers):
                    raise CompactAnalyticalError("stored sequence differs from its digest key")

        new_sequences = {key: value for key, value in sequences.items() if key not in existing_ids}
        segment_values: dict[str, tuple[str, tuple[UUID, ...], str]] = {}
        segment_links: dict[str, tuple[str, ...]] = {}
        for sequence_id, (link_type, identifiers, _checksum) in new_sequences.items():
            segment_ids: list[str] = []
            for offset in range(0, len(identifiers), _BATCH):
                members = identifiers[offset : offset + _BATCH]
                segment_id = _segment_identity(link_type, members)
                segment_checksum = _sequence_checksum(link_type, members)
                segment_value = (link_type, tuple(members), segment_checksum)
                previous = segment_values.get(segment_id)
                if previous is not None and previous != segment_value:
                    raise CompactAnalyticalError("segment address collision detected")
                segment_values[segment_id] = segment_value
                segment_ids.append(segment_id)
            segment_links[sequence_id] = tuple(segment_ids)

        existing_segment_rows = _id_query(
            self._connection,
            table=_SEGMENT_TABLE,
            key_column="segment_id",
            columns="segment_id",
            identifiers=list(segment_values),
        )
        existing_segment_ids = {str(row[0]) for row in existing_segment_rows}
        if existing_segment_ids:
            stored_segments = self._load_segments(tuple(existing_segment_ids))
            for segment_id in existing_segment_ids:
                link_type, members, _checksum = segment_values[segment_id]
                if stored_segments[segment_id] != (link_type, members):
                    raise CompactAnalyticalError("stored segment differs from its digest key")

        missing_segments = {
            key: value for key, value in segment_values.items() if key not in existing_segment_ids
        }
        segment_rows: list[tuple[object, ...]] = []
        member_rows: list[tuple[object, ...]] = []
        for segment_id, (link_type, members, checksum) in missing_segments.items():
            segment_rows.append((segment_id, link_type, len(members), checksum))
            member_rows.extend(
                (segment_id, position, str(member)) for position, member in enumerate(members)
            )
        if segment_rows:
            _executemany_bounded(
                self._connection,
                f"INSERT INTO {_SEGMENT_TABLE} VALUES (?, ?, ?, ?)",
                segment_rows,
            )
            _executemany_bounded(
                self._connection,
                f"INSERT INTO {_SEGMENT_MEMBER_TABLE} VALUES (?, ?, ?)",
                member_rows,
            )

        sequence_rows: list[tuple[object, ...]] = []
        relation_rows: list[tuple[object, ...]] = []
        for sequence_id, (link_type, identifiers, checksum) in new_sequences.items():
            sequence_rows.append((sequence_id, link_type, len(identifiers), checksum))
            relation_rows.extend(
                (sequence_id, position, segment_id)
                for position, segment_id in enumerate(segment_links[sequence_id])
            )
        if sequence_rows:
            _executemany_bounded(
                self._connection,
                f"INSERT INTO {_SEQUENCE_TABLE} VALUES (?, ?, ?, ?)",
                sequence_rows,
            )
            _executemany_bounded(
                self._connection,
                f"INSERT INTO {_SEQUENCE_SEGMENT_TABLE} VALUES (?, ?, ?)",
                relation_rows,
            )
        return request_ids

    def _load_sequences(
        self, sequence_ids: Sequence[str]
    ) -> dict[str, tuple[str, tuple[UUID, ...]]]:
        unique_ids = tuple(dict.fromkeys(sequence_ids))
        if not unique_ids:
            return {}
        rows = _id_query(
            self._connection,
            table=_SEQUENCE_TABLE,
            key_column="sequence_id",
            columns="sequence_id, link_type, member_count, checksum_sha256",
            identifiers=list(unique_ids),
        )
        indexed = {str(row[0]): row[1:] for row in rows}
        if any(identifier not in indexed for identifier in unique_ids):
            raise CompactAnalyticalError("analytical sequence reference is missing")
        relation_rows = _id_query(
            self._connection,
            table=_SEQUENCE_SEGMENT_TABLE,
            key_column="sequence_id",
            columns="sequence_id, segment_position, segment_id",
            identifiers=list(unique_ids),
        )
        relations: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for row in relation_rows:
            relations[str(row[0])].append((int(row[1]), str(row[2])))
        segment_ids = tuple(
            dict.fromkeys(
                segment_id
                for rows_for_sequence in relations.values()
                for _, segment_id in rows_for_sequence
            )
        )
        segments = self._load_segments(segment_ids)
        output: dict[str, tuple[str, tuple[UUID, ...]]] = {}
        for sequence_id in unique_ids:
            link_type, count, checksum = indexed[sequence_id]
            ordered_segments = sorted(relations[sequence_id])
            if [position for position, _ in ordered_segments] != list(range(len(ordered_segments))):
                raise CompactAnalyticalError("analytical sequence segment order is corrupt")
            identifiers: list[UUID] = []
            for _, segment_id in ordered_segments:
                segment_link_type, members = segments[segment_id]
                if segment_link_type != str(link_type):
                    raise CompactAnalyticalError("analytical sequence mixes link types")
                identifiers.extend(members)
            if len(identifiers) != int(count):
                raise CompactAnalyticalError("analytical sequence member count is corrupt")
            if _sequence_identity(str(link_type), identifiers) != sequence_id:
                raise CompactAnalyticalError("analytical sequence checksum does not match")
            if _sequence_checksum(str(link_type), identifiers) != str(checksum):
                raise CompactAnalyticalError("analytical sequence member digest does not match")
            output[sequence_id] = (str(link_type), tuple(identifiers))
        return output

    def _load_segments(self, segment_ids: Sequence[str]) -> dict[str, tuple[str, tuple[UUID, ...]]]:
        unique_ids = tuple(dict.fromkeys(segment_ids))
        if not unique_ids:
            return {}
        rows = _id_query(
            self._connection,
            table=_SEGMENT_TABLE,
            key_column="segment_id",
            columns="segment_id, link_type, member_count, checksum_sha256",
            identifiers=list(unique_ids),
        )
        indexed = {str(row[0]): row[1:] for row in rows}
        if any(identifier not in indexed for identifier in unique_ids):
            raise CompactAnalyticalError("analytical sequence segment is missing")
        member_rows = _id_query(
            self._connection,
            table=_SEGMENT_MEMBER_TABLE,
            key_column="segment_id",
            columns="segment_id, member_position, member_id",
            identifiers=list(unique_ids),
        )
        members_by_id: dict[str, list[tuple[int, UUID]]] = defaultdict(list)
        for row in member_rows:
            members_by_id[str(row[0])].append((int(row[1]), UUID(str(row[2]))))
        output: dict[str, tuple[str, tuple[UUID, ...]]] = {}
        for segment_id in unique_ids:
            link_type, count, checksum = indexed[segment_id]
            ordered = sorted(members_by_id[segment_id])
            if len(ordered) > _BATCH or len(ordered) != int(count):
                raise CompactAnalyticalError("analytical segment member count is corrupt")
            if [position for position, _ in ordered] != list(range(len(ordered))):
                raise CompactAnalyticalError("analytical segment member order is corrupt")
            identifiers = tuple(identifier for _, identifier in ordered)
            if _segment_identity(str(link_type), identifiers) != segment_id:
                raise CompactAnalyticalError("analytical segment checksum does not match")
            if _sequence_checksum(str(link_type), identifiers) != str(checksum):
                raise CompactAnalyticalError("analytical segment member digest does not match")
            output[segment_id] = (str(link_type), identifiers)
        return output

    def _require_metric_inputs_visible(self, results: Sequence[MetricResult]) -> None:
        """Validate input asset and PIT coordinates using bounded typed projections."""
        observation_owners: dict[str, tuple[str, datetime]] = {}
        metric_owners: dict[str, tuple[str, datetime, str]] = {}
        candidate_metrics = {str(item.result_id): item for item in results}
        observation_ids = tuple(
            dict.fromkeys(
                str(identifier) for item in results for identifier in item.input_observation_ids
            )
        )
        for chunk in _chunks(observation_ids, _QUERY_CHUNK):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                "SELECT observation_id, asset_id, available_at "
                "FROM normalized_observations_v2 "
                f"WHERE observation_id IN ({placeholders})",
                list(chunk),
            ).fetchall()
            observation_owners.update(
                {str(row[0]): (str(row[1]), _instant(row[2])) for row in rows}
            )
        metric_ids = tuple(
            dict.fromkeys(
                str(identifier) for item in results for identifier in item.input_metric_result_ids
            )
        )
        for identifier in metric_ids:
            candidate = candidate_metrics.get(identifier)
            if candidate is not None:
                metric_owners[identifier] = (
                    candidate.asset_id,
                    candidate.available_at,
                    candidate.metric_key,
                )
        persisted = tuple(
            identifier for identifier in metric_ids if identifier not in metric_owners
        )
        for chunk in _chunks(persisted, _QUERY_CHUNK):
            if not chunk:
                continue
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                "SELECT result_id, asset_id, CAST(available_at AS VARCHAR), metric_key "
                f"FROM {_METRIC_TABLE} "
                f"WHERE result_id IN ({placeholders})",
                list(chunk),
            ).fetchall()
            metric_owners.update(
                {str(row[0]): (str(row[1]), _instant(row[2]), str(row[3])) for row in rows}
            )
            if self._legacy_metric_table_exists():
                missing = [identifier for identifier in chunk if identifier not in metric_owners]
                if missing:
                    legacy_rows = self._connection.execute(
                        "SELECT result_id, asset_id, CAST(available_at AS VARCHAR), metric_key "
                        "FROM metric_results_v2 "
                        f"WHERE result_id IN ({', '.join('?' for _ in missing)})",
                        missing,
                    ).fetchall()
                    metric_owners.update(
                        {
                            str(row[0]): (str(row[1]), _instant(row[2]), str(row[3]))
                            for row in legacy_rows
                        }
                    )
        for result in results:
            for identifier in result.input_observation_ids:
                owner = observation_owners.get(str(identifier))
                if owner is None:
                    raise CompactAnalyticalError(
                        f"metric references missing observation {identifier}"
                    )
                asset_id, available_at = owner
                if asset_id != result.asset_id or available_at > result.available_at:
                    raise CompactAnalyticalError(
                        "metric observation reference is foreign or future"
                    )
            for identifier in result.input_metric_result_ids:
                if identifier == result.result_id:
                    raise CompactAnalyticalError("metric dependency cycle is not allowed")
                owner = metric_owners.get(str(identifier))
                if owner is None:
                    raise CompactAnalyticalError(f"metric references missing metric {identifier}")
                asset_id, available_at, _metric_key = owner
                if asset_id != result.asset_id or available_at > result.available_at:
                    raise CompactAnalyticalError("metric result reference is foreign or future")
        self._require_acyclic_batch(results)

    @staticmethod
    def _require_acyclic_batch(results: Sequence[MetricResult]) -> None:
        identifiers = {str(item.result_id) for item in results}
        dependents: dict[str, list[str]] = defaultdict(list)
        indegree = {identifier: 0 for identifier in identifiers}
        for item in results:
            current = str(item.result_id)
            for dependency in item.input_metric_result_ids:
                dependency_key = str(dependency)
                if dependency_key in identifiers:
                    dependents[dependency_key].append(current)
                    indegree[current] += 1
        ready = deque(key for key, count in indegree.items() if count == 0)
        visited = 0
        while ready:
            identifier = ready.popleft()
            visited += 1
            for dependent in dependents[identifier]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        if visited != len(identifiers):
            raise CompactAnalyticalError("metric dependency cycle is not allowed")

    def _require_diagnostic_metrics_visible(
        self,
        diagnostics: Sequence[DiagnosticResult],
        *,
        validate_domain: bool,
    ) -> None:
        metric_ids = tuple(
            dict.fromkeys(
                str(metric_id)
                for item in diagnostics
                for metric_id in (
                    [
                        metric_id
                        for component in item.components
                        for metric_id in component.metric_result_ids
                    ]
                    + [evidence.metric_result_id for evidence in item.evidence]
                )
            )
        )
        metadata: dict[str, tuple[str, datetime, str]] = {}
        for chunk in _chunks(metric_ids, _QUERY_CHUNK):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                "SELECT result_id, asset_id, CAST(available_at AS VARCHAR), metric_key "
                f"FROM {_METRIC_TABLE} "
                f"WHERE result_id IN ({placeholders})",
                list(chunk),
            ).fetchall()
            metadata.update(
                {str(row[0]): (str(row[1]), _instant(row[2]), str(row[3])) for row in rows}
            )
            missing = [identifier for identifier in chunk if identifier not in metadata]
            if missing and self._legacy_metric_table_exists():
                legacy_rows = self._connection.execute(
                    "SELECT result_id, asset_id, CAST(available_at AS VARCHAR), metric_key "
                    "FROM metric_results_v2 "
                    f"WHERE result_id IN ({', '.join('?' for _ in missing)})",
                    missing,
                ).fetchall()
                metadata.update(
                    {
                        str(row[0]): (str(row[1]), _instant(row[2]), str(row[3]))
                        for row in legacy_rows
                    }
                )
        for diagnostic in diagnostics:
            cited = {
                metric_id
                for component in diagnostic.components
                for metric_id in component.metric_result_ids
            } | {item.metric_result_id for item in diagnostic.evidence}
            metric_keys: dict[UUID, str] = {}
            for identifier in cited:
                owner = metadata.get(str(identifier))
                if owner is None:
                    raise CompactAnalyticalError(
                        f"diagnostic references missing metric {identifier}"
                    )
                asset_id, available_at, metric_key = owner
                if asset_id != diagnostic.asset_id or available_at > diagnostic.available_at:
                    raise CompactAnalyticalError("diagnostic metric reference is foreign or future")
                metric_keys[identifier] = metric_key
            if validate_domain:
                try:
                    validate_diagnostic_internal_consistency(diagnostic, metric_keys)
                except DomainMembershipError as error:
                    raise CompactAnalyticalError(str(error)) from error

    def _legacy_metric_table_exists(self) -> bool:
        row = self._connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = 'metric_results_v2'"
        ).fetchone()
        return row is not None and int(row[0]) == 1

    def historical_seal(self) -> HistoricalAnalyticalSeal | None:
        rows = self._connection.execute(
            f"SELECT source_fingerprint, metric_count, metric_digest, diagnostic_count, "
            f"diagnostic_digest, CAST(sealed_at AS VARCHAR) FROM {_SEAL_TABLE}"
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise CompactAnalyticalError("workspace v2 historical seal is ambiguous")
        row = rows[0]
        try:
            return HistoricalAnalyticalSeal(
                source_fingerprint=str(row[0]),
                metric_count=int(row[1]),
                metric_digest=str(row[2]),
                diagnostic_count=int(row[3]),
                diagnostic_digest=str(row[4]),
                sealed_at=_instant(row[5]),
            )
        except (ValueError, TypeError) as error:
            raise CompactAnalyticalError("workspace v2 historical seal is malformed") from error

    def seal_historical_inventory(
        self,
        *,
        source_fingerprint: str,
        metric_count: int,
        metric_digest: str,
        diagnostic_count: int,
        diagnostic_digest: str,
        sealed_at: datetime,
    ) -> HistoricalAnalyticalSeal:
        """Seal only a complete archive whose ordered model digests match source v1."""
        if len(source_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in source_fingerprint
        ):
            raise CompactAnalyticalError("historical source fingerprint is not SHA-256")
        for value in (metric_digest, diagnostic_digest):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise CompactAnalyticalError("historical inventory digest is not SHA-256")
        actual_metric_count, actual_metric_digest = self._historical_metric_digest()
        actual_diagnostic_count, actual_diagnostic_digest = self._historical_diagnostic_digest()
        expected = (
            metric_count,
            metric_digest,
            diagnostic_count,
            diagnostic_digest,
        )
        actual = (
            actual_metric_count,
            actual_metric_digest,
            actual_diagnostic_count,
            actual_diagnostic_digest,
        )
        if actual != expected:
            raise CompactAnalyticalError("historical rows do not match the source inventory")
        existing = self.historical_seal()
        if existing is not None:
            if (
                existing.source_fingerprint != source_fingerprint
                or existing.metric_count != metric_count
                or existing.metric_digest != metric_digest
                or existing.diagnostic_count != diagnostic_count
                or existing.diagnostic_digest != diagnostic_digest
            ):
                raise CompactAnalyticalError("historical seal conflicts with the source inventory")
            return existing
        if sealed_at.tzinfo is None or sealed_at.utcoffset() is None:
            raise CompactAnalyticalError("historical seal timestamp must be timezone-aware")
        with write_transaction(self._connection):
            self._connection.execute(
                f"INSERT INTO {_SEAL_TABLE} VALUES ('historical-analytical-v1', ?, ?, ?, ?, ?, ?)",
                [
                    source_fingerprint,
                    metric_count,
                    metric_digest,
                    diagnostic_count,
                    diagnostic_digest,
                    sealed_at.astimezone(UTC),
                ],
            )
        result = self.historical_seal()
        if result is None:
            raise CompactAnalyticalError("historical seal was not persisted")
        return result

    def verify_historical_seal(self) -> HistoricalAnalyticalSeal | None:
        seal = self.historical_seal()
        metric_count = self.count_metrics(origin="HISTORICAL")
        diagnostic_count = self.count_diagnostics(origin="HISTORICAL")
        if seal is None:
            if metric_count or diagnostic_count:
                raise CompactAnalyticalError("historical analytical rows have no inventory seal")
            return None
        actual = (
            *self._historical_metric_digest(),
            *self._historical_diagnostic_digest(),
        )
        expected = (
            seal.metric_count,
            seal.metric_digest,
            seal.diagnostic_count,
            seal.diagnostic_digest,
        )
        if actual != expected:
            raise CompactAnalyticalError("sealed historical analytical inventory changed")
        return seal

    def historical_inventory_digests(self) -> tuple[int, str, int, str]:
        """Return exact ordered digests for historical rows without changing the seal."""
        return (*self._historical_metric_digest(), *self._historical_diagnostic_digest())

    def _historical_metric_digest(self) -> tuple[int, str]:
        digest = hashlib.sha256(b"").hexdigest()
        count = 0
        cursor_at: datetime | None = None
        cursor_id: UUID | None = None
        while True:
            identifiers = self.list_metric_ids_page(
                limit=_BATCH,
                after_available_at=cursor_at,
                after_result_id=cursor_id,
                origin="HISTORICAL",
            )
            if not identifiers:
                return count, digest
            results = self.get_metrics(identifiers)
            for identifier in identifiers:
                checksum = sha256_hex(canonical_json_bytes(results[identifier]))
                digest = hashlib.sha256(f"{digest}:{checksum}".encode("ascii")).hexdigest()
                count += 1
            last = results[identifiers[-1]]
            cursor_at = last.available_at
            cursor_id = last.result_id

    def _historical_diagnostic_digest(self) -> tuple[int, str]:
        digest = hashlib.sha256(b"").hexdigest()
        count = 0
        cursor_at: datetime | None = None
        cursor_id: UUID | None = None
        while True:
            identifiers = self.list_diagnostic_ids_page(
                limit=_BATCH,
                after_available_at=cursor_at,
                after_diagnostic_id=cursor_id,
                origin="HISTORICAL",
            )
            if not identifiers:
                return count, digest
            results = self.get_diagnostics(identifiers)
            for identifier in identifiers:
                checksum = sha256_hex(canonical_json_bytes(results[identifier]))
                digest = hashlib.sha256(f"{digest}:{checksum}".encode("ascii")).hexdigest()
                count += 1
            last = results[identifiers[-1]]
            cursor_at = last.available_at
            cursor_id = last.diagnostic_id

    def verify_complete(self) -> dict[str, int | str]:
        """Walk each family by bounded pages and verify content, PIT links and seals."""
        self.ensure(create=False)
        raw_orphan = self._connection.execute(
            "SELECT o.observation_id FROM normalized_observations_v2 o "
            "LEFT JOIN raw_v2_index r ON r.record_id = o.raw_record_id "
            "WHERE r.record_id IS NULL LIMIT 1"
        ).fetchone()
        if raw_orphan is not None:
            raise CompactAnalyticalError("workspace v2 observation has no raw record")

        metric_count = 0
        metric_cursor_at: datetime | None = None
        metric_cursor_id: UUID | None = None
        while True:
            identifiers = self.list_metric_ids_page(
                limit=_BATCH,
                after_available_at=metric_cursor_at,
                after_result_id=metric_cursor_id,
            )
            if not identifiers:
                break
            metrics = self.get_metrics(identifiers)
            self._require_metric_inputs_visible(tuple(metrics.values()))
            metric_count += len(metrics)
            last = metrics[identifiers[-1]]
            metric_cursor_at = last.available_at
            metric_cursor_id = last.result_id

        diagnostic_count = 0
        diagnostic_cursor_at: datetime | None = None
        diagnostic_cursor_id: UUID | None = None
        while True:
            identifiers = self.list_diagnostic_ids_page(
                limit=_BATCH,
                after_available_at=diagnostic_cursor_at,
                after_diagnostic_id=diagnostic_cursor_id,
            )
            if not identifiers:
                break
            diagnostics = self.get_diagnostics(identifiers)
            self._require_diagnostic_metrics_visible(
                tuple(diagnostics.values()),
                validate_domain=False,
            )
            diagnostic_count += len(diagnostics)
            last = diagnostics[identifiers[-1]]
            diagnostic_cursor_at = last.available_at
            diagnostic_cursor_id = last.diagnostic_id

        self._require_acyclic_inventory()
        self.verify_historical_seal()
        return {
            "metric_count": metric_count,
            "diagnostic_count": diagnostic_count,
            "metric_rows_verified": metric_count,
            "diagnostic_rows_verified": diagnostic_count,
            "historical_metric_count": self.count_metrics(origin="HISTORICAL"),
            "historical_diagnostic_count": self.count_diagnostics(origin="HISTORICAL"),
        }

    def _require_acyclic_inventory(self) -> None:
        row = self._connection.execute(
            """
            WITH RECURSIVE metric_edges(parent_id, child_id) AS (
                SELECT m.result_id, members.member_id
                FROM workspace_metric_results_v2 m
                JOIN workspace_analytical_sequences_v2 s
                  ON s.sequence_id = m.metric_sequence_id
                 AND s.link_type = 'metric-input-result-v1'
                JOIN workspace_analytical_sequence_segments_v2 links
                  ON links.sequence_id = s.sequence_id
                JOIN workspace_analytical_segment_members_v2 members
                  ON members.segment_id = links.segment_id
            ), walks(root_id, node_id, path, cycle) AS (
                SELECT parent_id, child_id, [parent_id, child_id], parent_id = child_id
                FROM metric_edges
                UNION ALL
                SELECT walks.root_id,
                       metric_edges.child_id,
                       list_append(walks.path, metric_edges.child_id),
                       list_contains(walks.path, metric_edges.child_id)
                FROM walks
                JOIN metric_edges ON metric_edges.parent_id = walks.node_id
                WHERE NOT walks.cycle
            )
            SELECT root_id FROM walks WHERE cycle LIMIT 1
            """
        ).fetchone()
        if row is not None:
            raise CompactAnalyticalError("workspace v2 metric dependency graph contains a cycle")

    def logical_table_bytes(self) -> dict[str, int]:
        """Measure stored logical bytes with UTF-8 text and fixed scalar widths."""
        tables = tuple(_COMPACT_TABLES)
        output: dict[str, int] = {}
        for table in tables:
            rows = self._connection.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
            expressions: list[str] = []
            for column, data_type in rows:
                name = str(column)
                kind = str(data_type).upper()
                if kind == "VARCHAR":
                    expressions.append(f"coalesce(sum(octet_length(encode({name}))), 0)")
                elif kind == "BLOB":
                    expressions.append(f"coalesce(sum(octet_length({name})), 0)")
                elif kind == "INTEGER":
                    expressions.append("count(*) * 4")
                elif kind in {"BIGINT", "TIMESTAMP WITH TIME ZONE"}:
                    expressions.append("count(*) * 8")
            if not expressions:
                output[table] = 0
                continue
            row = self._connection.execute(
                f"SELECT {' + '.join(expressions)} FROM {table}"
            ).fetchone()
            output[table] = int(row[0]) if row is not None and row[0] is not None else 0
        return output


__all__ = [
    "CompactAnalyticalError",
    "CompactAnalyticalStore",
    "HistoricalAnalyticalSeal",
    "compact_analytical_tables_exist",
    "ensure_compact_analytical_tables",
]
