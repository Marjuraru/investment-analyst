"""Typed, append-only archive for historical v1 metric and diagnostic results.

The archive is deliberately independent from the active v2 analytical stores.
It preserves original result IDs and complete model content while storing
repeated UUID link arrays once as content-addressed, ordered sequences.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from duckdb import DuckDBPyConnection

from investment_analyst.core.models import (
    DataQuality,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
)
from investment_analyst.core.models.base import ContractModel, UTCDateTime
from investment_analyst.storage.bounded_insert import write_transaction
from investment_analyst.storage.errors import RecordConflictError, RecordNotFoundError, StorageError
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

_BATCH = 256
_ARCHIVE_TABLES = (
    "historical_metric_results",
    "historical_diagnostic_results",
    "historical_analytical_components",
    "historical_analytical_evidence",
    "historical_analytical_sequences",
    "historical_analytical_sequence_segments",
    "historical_analytical_segments",
    "historical_analytical_segment_members",
)

if TYPE_CHECKING:
    from investment_analyst.storage.historical_analytical_validation import (
        HistoricalAnalyticalValidationSummary,
    )


class HistoricalAnalyticalArchiveError(StorageError):
    """Raised when the isolated historical analytical archive cannot be trusted."""


class HistoricalAnalyticalCursor(ContractModel):
    """Stable cursor over the available-at, identifier ordering."""

    model_config = {"extra": "forbid", "frozen": True, "strict": True}

    available_at: UTCDateTime
    identifier: UUID


class HistoricalAnalyticalSequence(ContractModel):
    """Verified ordered UUID sequence materialized from content-addressed segments."""

    model_config = {"extra": "forbid", "frozen": True, "strict": True}

    sequence_id: str
    link_type: str
    identifiers: tuple[UUID, ...]


class HistoricalAnalyticalRowsSummary(ContractModel):
    """Exact ordered inventory digests from bounded archive hydration."""

    model_config = {"extra": "forbid", "frozen": True, "strict": True}

    metric_count: int
    diagnostic_count: int
    metric_digest: str
    diagnostic_digest: str
    metric_cursor: HistoricalAnalyticalCursor | None = None
    diagnostic_cursor: HistoricalAnalyticalCursor | None = None


def _digest_parts(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(prefix.encode("ascii"))
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _sequence_hash(link_type: str, identifiers: Sequence[UUID]) -> str:
    return _digest_parts(
        "historical-analytical-sequence-v1", link_type, *(str(x) for x in identifiers)
    )


def _segment_hash(link_type: str, identifiers: Sequence[UUID]) -> str:
    return _digest_parts(
        "historical-analytical-segment-v1", link_type, *(str(x) for x in identifiers)
    )


def _sequence_id(link_type: str, identifiers: Sequence[UUID]) -> str:
    return _sequence_hash(link_type, identifiers)


def _instant_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HistoricalAnalyticalArchiveError("historical analytical time must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _instant(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise HistoricalAnalyticalArchiveError("historical analytical time is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalAnalyticalArchiveError("historical analytical time is not timezone-aware")
    return parsed.astimezone(UTC)


def _json_text(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
    except (TypeError, ValueError) as error:
        raise HistoricalAnalyticalArchiveError("historical analytical JSON is invalid") from error


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _BATCH:
        raise ValueError("historical analytical page limit must be an integer between 1 and 256")


def _validate_metric_identity(result: MetricResult) -> None:
    """Verify UUIDv8 through the public helper while preserving other UUID versions."""
    if result.result_id.version != 8:
        return
    from investment_analyst.storage.metric_v2 import recalculate_metric_result_id

    try:
        expected = recalculate_metric_result_id(result)
    except (TypeError, ValueError) as error:
        raise HistoricalAnalyticalArchiveError(
            "historical UUIDv8 metric identity cannot be validated"
        ) from error
    if expected != result.result_id:
        raise HistoricalAnalyticalArchiveError(
            "historical UUIDv8 metric identity does not match its source model"
        )


def _chunks[T](items: Sequence[T], size: int = _BATCH) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _executemany_bounded(
    connection: DuckDBPyConnection, sql: str, rows: Sequence[Sequence[object]]
) -> None:
    for chunk in _chunks(rows):
        connection.executemany(sql, chunk)


def _schema(connection: DuckDBPyConnection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()
    )


def historical_analytical_archive_exists(connection: DuckDBPyConnection) -> bool:
    """Return true when any archive table exists, rejecting partial schemas."""
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    found = {str(row[0]) for row in rows}
    present = found.intersection(_ARCHIVE_TABLES)
    if present and present != set(_ARCHIVE_TABLES):
        raise HistoricalAnalyticalArchiveError("historical analytical archive schema is partial")
    return bool(present)


def ensure_historical_analytical_archive_tables(
    connection: DuckDBPyConnection, *, create: bool
) -> None:
    """Create or validate every typed archive table without changing its schema."""
    ddl = (
        """CREATE TABLE IF NOT EXISTS historical_metric_results (
            result_id VARCHAR PRIMARY KEY, asset_id VARCHAR NOT NULL, metric_key VARCHAR NOT NULL,
            value_text VARCHAR NOT NULL, unit VARCHAR NOT NULL, as_of VARCHAR NOT NULL,
            available_at VARCHAR NOT NULL, computed_at VARCHAR NOT NULL,
            parameters_json VARCHAR NOT NULL,
            observation_sequence_id VARCHAR NOT NULL, metric_sequence_id VARCHAR NOT NULL,
            algorithm_version VARCHAR NOT NULL, quality VARCHAR NOT NULL,
            id_version INTEGER NOT NULL,
            checksum_sha256 VARCHAR NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS historical_diagnostic_results (
            diagnostic_id VARCHAR PRIMARY KEY, asset_id VARCHAR NOT NULL, mode VARCHAR NOT NULL,
            verdict VARCHAR NOT NULL, final_score_text VARCHAR NOT NULL,
            confidence_text VARCHAR NOT NULL,
            as_of VARCHAR NOT NULL, available_at VARCHAR NOT NULL, computed_at VARCHAR NOT NULL,
            algorithm_version VARCHAR NOT NULL, summary VARCHAR NOT NULL, quality VARCHAR NOT NULL,
            component_count INTEGER NOT NULL, evidence_count INTEGER NOT NULL,
            checksum_sha256 VARCHAR NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS historical_analytical_components (
            diagnostic_id VARCHAR NOT NULL, position INTEGER NOT NULL,
            component_key VARCHAR NOT NULL,
            score_text VARCHAR NOT NULL, weight_text VARCHAR NOT NULL,
            weighted_contribution_text VARCHAR NOT NULL,
            metric_sequence_id VARCHAR NOT NULL, explanation VARCHAR NOT NULL,
            PRIMARY KEY (diagnostic_id, position))""",
        """CREATE TABLE IF NOT EXISTS historical_analytical_evidence (
            diagnostic_id VARCHAR NOT NULL, position INTEGER NOT NULL,
            metric_result_id VARCHAR NOT NULL,
            direction VARCHAR NOT NULL, contribution_text VARCHAR NOT NULL, reason VARCHAR NOT NULL,
            PRIMARY KEY (diagnostic_id, position))""",
        """CREATE TABLE IF NOT EXISTS historical_analytical_sequences (
            sequence_id VARCHAR PRIMARY KEY, link_type VARCHAR NOT NULL, item_count BIGINT NOT NULL,
            sequence_hash VARCHAR NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS historical_analytical_sequence_segments (
            sequence_id VARCHAR NOT NULL, position INTEGER NOT NULL, segment_id VARCHAR NOT NULL,
            PRIMARY KEY (sequence_id, position))""",
        """CREATE TABLE IF NOT EXISTS historical_analytical_segments (
            segment_id VARCHAR PRIMARY KEY, link_type VARCHAR NOT NULL, item_count INTEGER NOT NULL,
            segment_hash VARCHAR NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS historical_analytical_segment_members (
            segment_id VARCHAR NOT NULL, position INTEGER NOT NULL, identifier VARCHAR NOT NULL,
            PRIMARY KEY (segment_id, position))""",
    )
    expected = {
        "historical_metric_results": (
            "result_id",
            "asset_id",
            "metric_key",
            "value_text",
            "unit",
            "as_of",
            "available_at",
            "computed_at",
            "parameters_json",
            "observation_sequence_id",
            "metric_sequence_id",
            "algorithm_version",
            "quality",
            "id_version",
            "checksum_sha256",
        ),
        "historical_diagnostic_results": (
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
            "component_count",
            "evidence_count",
            "checksum_sha256",
        ),
        "historical_analytical_components": (
            "diagnostic_id",
            "position",
            "component_key",
            "score_text",
            "weight_text",
            "weighted_contribution_text",
            "metric_sequence_id",
            "explanation",
        ),
        "historical_analytical_evidence": (
            "diagnostic_id",
            "position",
            "metric_result_id",
            "direction",
            "contribution_text",
            "reason",
        ),
        "historical_analytical_sequences": (
            "sequence_id",
            "link_type",
            "item_count",
            "sequence_hash",
        ),
        "historical_analytical_sequence_segments": ("sequence_id", "position", "segment_id"),
        "historical_analytical_segments": ("segment_id", "link_type", "item_count", "segment_hash"),
        "historical_analytical_segment_members": ("segment_id", "position", "identifier"),
    }
    if create:
        for statement in ddl:
            connection.execute(statement)
    for table, columns in expected.items():
        actual = _schema(connection, table)
        if actual != columns:
            raise HistoricalAnalyticalArchiveError(
                "historical analytical archive schema is incompatible"
            )


class HistoricalAnalyticalArchive:
    """Append-only API for the historical analytical archive on staging's connection."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection
        self._sequence_cache: dict[tuple[str, tuple[UUID, ...]], str] = {}

    def ensure(self, *, create: bool = False) -> None:
        ensure_historical_analytical_archive_tables(self._connection, create=create)

    def count_metrics(self) -> int:
        self.ensure()
        return int(
            self._connection.execute("SELECT count(*) FROM historical_metric_results").fetchone()[0]
        )

    def count_diagnostics(self) -> int:
        self.ensure()
        return int(
            self._connection.execute(
                "SELECT count(*) FROM historical_diagnostic_results"
            ).fetchone()[0]
        )

    def verify_complete(self) -> HistoricalAnalyticalValidationSummary:
        """Verify every row and sequence in bounded pages, then validate the reference graph."""
        from investment_analyst.storage.historical_analytical_validation import (
            verify_historical_analytical_archive,
        )

        self.ensure()
        self.verify_rows()
        return verify_historical_analytical_archive(self._connection)

    def verify_rows(self) -> HistoricalAnalyticalRowsSummary:
        """Hydrate and checksum every durable row in bounded pages."""
        from investment_analyst.storage.historical_analytical_validation import (
            verify_historical_analytical_archive_structure,
        )

        self.ensure()
        metric_count = 0
        metric_digest = hashlib.sha256(b"").hexdigest()
        metric_cursor: HistoricalAnalyticalCursor | None = None
        while True:
            metric_ids = self.list_metric_ids_page(limit=_BATCH, after=metric_cursor)
            if not metric_ids:
                break
            metrics = self.get_metrics(metric_ids)
            metric_count += len(metrics)
            for identifier in metric_ids:
                checksum = sha256_hex(canonical_json_bytes(metrics[identifier]))
                metric_digest = hashlib.sha256(f"{metric_digest}:{checksum}".encode()).hexdigest()
            last_metric = metrics[metric_ids[-1]]
            metric_cursor = HistoricalAnalyticalCursor(
                available_at=last_metric.available_at,
                identifier=last_metric.result_id,
            )
        diagnostic_count = 0
        diagnostic_digest = hashlib.sha256(b"").hexdigest()
        diagnostic_cursor: HistoricalAnalyticalCursor | None = None
        while True:
            diagnostic_ids = self.list_diagnostic_ids_page(limit=_BATCH, after=diagnostic_cursor)
            if not diagnostic_ids:
                break
            diagnostics = self.get_diagnostics(diagnostic_ids)
            diagnostic_count += len(diagnostics)
            for identifier in diagnostic_ids:
                checksum = sha256_hex(canonical_json_bytes(diagnostics[identifier]))
                diagnostic_digest = hashlib.sha256(
                    f"{diagnostic_digest}:{checksum}".encode()
                ).hexdigest()
            last_diagnostic = diagnostics[diagnostic_ids[-1]]
            diagnostic_cursor = HistoricalAnalyticalCursor(
                available_at=last_diagnostic.available_at,
                identifier=last_diagnostic.diagnostic_id,
            )
        verify_historical_analytical_archive_structure(self._connection)
        return HistoricalAnalyticalRowsSummary(
            metric_count=metric_count,
            diagnostic_count=diagnostic_count,
            metric_digest=metric_digest,
            diagnostic_digest=diagnostic_digest,
            metric_cursor=metric_cursor,
            diagnostic_cursor=diagnostic_cursor,
        )

    def save_metrics(
        self, results: Collection[MetricResult]
    ) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
        self.ensure(create=True)
        typed = tuple(results)
        if len(typed) > _BATCH or any(not isinstance(item, MetricResult) for item in typed):
            raise HistoricalAnalyticalArchiveError(
                "historical metric batch must contain at most 256 MetricResult rows"
            )
        if not typed:
            return (), ()
        checksums: dict[UUID, str] = {}
        for item in typed:
            _validate_metric_identity(item)
            checksum = sha256_hex(canonical_json_bytes(item))
            previous = checksums.get(item.result_id)
            if previous is not None and previous != checksum:
                raise RecordConflictError("historical metric identifier has conflicting content")
            checksums[item.result_id] = checksum
        unique = {item.result_id: item for item in typed}
        existing_ids = tuple(unique)
        existing_rows = self._select_by_ids("historical_metric_results", "result_id", existing_ids)
        existing_keys = {str(row[0]) for row in existing_rows}
        if existing_keys:
            current = self.get_metrics(tuple(UUID(value) for value in sorted(existing_keys)))
            for result_id, result in current.items():
                if result != unique[result_id]:
                    raise RecordConflictError(
                        "historical metric identifier has conflicting content"
                    )
        created: list[UUID] = []
        reused: list[UUID] = []
        insert_rows: list[tuple[object, ...]] = []
        existing_typed_ids = current_ids(existing_keys)
        with write_transaction(self._connection):
            new_items = [item for key, item in unique.items() if key not in existing_typed_ids]
            sequence_requests = [
                request
                for item in new_items
                for request in (
                    ("metric-input-observation-v1", item.input_observation_ids),
                    ("metric-input-result-v1", item.input_metric_result_ids),
                )
            ]
            sequence_ids = self._save_sequences_batch(sequence_requests)
            sequences_by_metric = {
                item.result_id: (sequence_ids[index * 2], sequence_ids[index * 2 + 1])
                for index, item in enumerate(new_items)
            }
            for result_id, item in unique.items():
                if result_id in existing_typed_ids:
                    reused.append(result_id)
                    continue
                observation_sequence, metric_sequence = sequences_by_metric[result_id]
                parameters = json.loads(canonical_json_bytes(item))["parameters"]
                insert_rows.append(
                    (
                        str(item.result_id),
                        item.asset_id,
                        item.metric_key,
                        str(item.value),
                        item.unit,
                        _instant_text(item.as_of),
                        _instant_text(item.available_at),
                        _instant_text(item.computed_at),
                        _json_text(parameters),
                        observation_sequence,
                        metric_sequence,
                        item.algorithm_version,
                        item.quality.value,
                        item.result_id.version or 0,
                        checksums[result_id],
                    )
                )
                created.append(result_id)
            if insert_rows:
                _executemany_bounded(
                    self._connection,
                    "INSERT INTO historical_metric_results VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    insert_rows,
                )
        return tuple(created), tuple(reused)

    def save_diagnostics(
        self, results: Collection[DiagnosticResult]
    ) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
        self.ensure(create=True)
        typed = tuple(results)
        if len(typed) > _BATCH or any(not isinstance(item, DiagnosticResult) for item in typed):
            raise HistoricalAnalyticalArchiveError(
                "historical diagnostic batch must contain at most 256 DiagnosticResult rows"
            )
        if not typed:
            return (), ()
        checksums: dict[UUID, str] = {}
        for item in typed:
            checksum = sha256_hex(canonical_json_bytes(item))
            previous = checksums.get(item.diagnostic_id)
            if previous is not None and previous != checksum:
                raise RecordConflictError(
                    "historical diagnostic identifier has conflicting content"
                )
            checksums[item.diagnostic_id] = checksum
        unique = {item.diagnostic_id: item for item in typed}
        existing_rows = self._select_by_ids(
            "historical_diagnostic_results", "diagnostic_id", tuple(unique)
        )
        existing_keys = {str(row[0]) for row in existing_rows}
        current: dict[UUID, DiagnosticResult] = {}
        if existing_keys:
            current = self.get_diagnostics(tuple(UUID(value) for value in sorted(existing_keys)))
            for diagnostic_id, result in current.items():
                if result != unique[diagnostic_id]:
                    raise RecordConflictError(
                        "historical diagnostic identifier has conflicting content"
                    )
        created: list[UUID] = []
        reused: list[UUID] = []
        parents: list[tuple[object, ...]] = []
        components: list[tuple[object, ...]] = []
        evidence: list[tuple[object, ...]] = []
        with write_transaction(self._connection):
            new_items = [item for key, item in unique.items() if key not in current]
            component_requests = [
                ("diagnostic-component-metric-result-v1", component.metric_result_ids)
                for item in new_items
                for component in item.components
            ]
            component_sequence_ids = self._save_sequences_batch(component_requests)
            sequence_cursor = iter(component_sequence_ids)
            sequences_by_component = {
                (item.diagnostic_id, position): next(sequence_cursor)
                for item in new_items
                for position, _component in enumerate(item.components)
            }
            for diagnostic_id, item in unique.items():
                if diagnostic_id in current:
                    reused.append(diagnostic_id)
                    continue
                parents.append(
                    (
                        str(item.diagnostic_id),
                        item.asset_id,
                        item.mode.value,
                        item.verdict.value,
                        str(item.final_score),
                        str(item.confidence),
                        _instant_text(item.as_of),
                        _instant_text(item.available_at),
                        _instant_text(item.computed_at),
                        item.algorithm_version,
                        item.summary,
                        item.quality.value,
                        len(item.components),
                        len(item.evidence),
                        checksums[diagnostic_id],
                    )
                )
                for position, component in enumerate(item.components):
                    sequence = sequences_by_component[(diagnostic_id, position)]
                    components.append(
                        (
                            str(diagnostic_id),
                            position,
                            component.component_key,
                            str(component.score),
                            str(component.weight),
                            str(component.weighted_contribution),
                            sequence,
                            component.explanation,
                        )
                    )
                for position, proof in enumerate(item.evidence):
                    evidence.append(
                        (
                            str(diagnostic_id),
                            position,
                            str(proof.metric_result_id),
                            proof.direction.value,
                            str(proof.contribution),
                            proof.reason,
                        )
                    )
                created.append(diagnostic_id)
            if parents:
                _executemany_bounded(
                    self._connection,
                    "INSERT INTO historical_diagnostic_results VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    parents,
                )
            if components:
                _executemany_bounded(
                    self._connection,
                    "INSERT INTO historical_analytical_components VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    components,
                )
            if evidence:
                _executemany_bounded(
                    self._connection,
                    "INSERT INTO historical_analytical_evidence VALUES (?, ?, ?, ?, ?, ?)",
                    evidence,
                )
        return tuple(created), tuple(reused)

    def get_metrics(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]:
        self.ensure()
        ordered = tuple(sorted(set(result_ids), key=str))
        if not ordered:
            return {}
        if len(ordered) > _BATCH:
            raise ValueError("historical metric lookup is limited to 256 IDs")
        rows = self._select_by_ids("historical_metric_results", "result_id", ordered)
        indexed = {UUID(str(row[0])): row for row in rows}
        missing = [item for item in ordered if item not in indexed]
        if missing:
            raise RecordNotFoundError(f"historical metric {missing[0]} was not found")
        sequences = self._load_sequences(
            tuple(str(indexed[item][9]) for item in ordered)
            + tuple(str(indexed[item][10]) for item in ordered)
        )
        results: dict[UUID, MetricResult] = {}
        for result_id in ordered:
            row = indexed[result_id]
            try:
                params = json.loads(str(row[8]))
                result = MetricResult(
                    result_id=result_id,
                    asset_id=str(row[1]),
                    metric_key=str(row[2]),
                    value=Decimal(str(row[3])),
                    unit=str(row[4]),
                    as_of=_instant(row[5]),
                    available_at=_instant(row[6]),
                    computed_at=_instant(row[7]),
                    parameters=params,
                    input_observation_ids=list(sequences[str(row[9])].identifiers),
                    input_metric_result_ids=list(sequences[str(row[10])].identifiers),
                    algorithm_version=str(row[11]),
                    quality=DataQuality(str(row[12])),
                )
            except (ValueError, TypeError) as error:
                raise HistoricalAnalyticalArchiveError(
                    "historical metric row is invalid"
                ) from error
            if (result.result_id.version or 0) != int(row[13]):
                raise HistoricalAnalyticalArchiveError("historical metric UUID version was altered")
            _validate_metric_identity(result)
            if sha256_hex(canonical_json_bytes(result)) != str(row[14]):
                raise HistoricalAnalyticalArchiveError("historical metric checksum does not match")
            results[result_id] = result
        return results

    def get_diagnostics(self, diagnostic_ids: Collection[UUID]) -> dict[UUID, DiagnosticResult]:
        self.ensure()
        ordered = tuple(sorted(set(diagnostic_ids), key=str))
        if not ordered:
            return {}
        if len(ordered) > _BATCH:
            raise ValueError("historical diagnostic lookup is limited to 256 IDs")
        rows = self._select_by_ids("historical_diagnostic_results", "diagnostic_id", ordered)
        indexed = {UUID(str(row[0])): row for row in rows}
        missing = [item for item in ordered if item not in indexed]
        if missing:
            raise RecordNotFoundError(f"historical diagnostic {missing[0]} was not found")
        key_values = [str(item) for item in ordered]
        component_rows = self._list_diagnostic_children(
            "historical_analytical_components",
            "diagnostic_id, position, component_key, score_text, weight_text, "
            "weighted_contribution_text, metric_sequence_id, explanation",
            key_values,
        )
        evidence_rows = self._list_diagnostic_children(
            "historical_analytical_evidence",
            "diagnostic_id, position, metric_result_id, direction, contribution_text, reason",
            key_values,
        )
        seqs = self._load_sequences(tuple(str(row[6]) for row in component_rows))
        components_by_id: dict[UUID, list[dict[str, object]]] = {item: [] for item in ordered}
        evidence_by_id: dict[UUID, list[dict[str, object]]] = {item: [] for item in ordered}
        for row in component_rows:
            identifier = UUID(str(row[0]))
            components_by_id[identifier].append(
                {
                    "position": int(row[1]),
                    "component_key": str(row[2]),
                    "score": Decimal(str(row[3])),
                    "weight": Decimal(str(row[4])),
                    "weighted_contribution": Decimal(str(row[5])),
                    "metric_result_ids": list(seqs[str(row[6])].identifiers),
                    "explanation": str(row[7]),
                }
            )
        for row in evidence_rows:
            identifier = UUID(str(row[0]))
            evidence_by_id[identifier].append(
                {
                    "position": int(row[1]),
                    "metric_result_id": str(row[2]),
                    "direction": EvidenceDirection(str(row[3])),
                    "contribution": Decimal(str(row[4])),
                    "reason": str(row[5]),
                }
            )
        results: dict[UUID, DiagnosticResult] = {}
        for diagnostic_id in ordered:
            row = indexed[diagnostic_id]
            component_items = components_by_id[diagnostic_id]
            evidence_items = evidence_by_id[diagnostic_id]
            if len(component_items) != int(row[12]) or len(evidence_items) != int(row[13]):
                raise HistoricalAnalyticalArchiveError(
                    "historical diagnostic child inventory is incomplete"
                )
            if [int(item["position"]) for item in component_items] != list(
                range(len(component_items))
            ):
                raise HistoricalAnalyticalArchiveError(
                    "historical diagnostic component order is corrupt"
                )
            if [int(item["position"]) for item in evidence_items] != list(
                range(len(evidence_items))
            ):
                raise HistoricalAnalyticalArchiveError(
                    "historical diagnostic evidence order is corrupt"
                )
            try:
                result = DiagnosticResult(
                    diagnostic_id=diagnostic_id,
                    asset_id=str(row[1]),
                    mode=DiagnosticMode(str(row[2])),
                    verdict=DiagnosticVerdict(str(row[3])),
                    final_score=Decimal(str(row[4])),
                    confidence=Decimal(str(row[5])),
                    as_of=_instant(row[6]),
                    available_at=_instant(row[7]),
                    computed_at=_instant(row[8]),
                    components=[
                        {key: value for key, value in item.items() if key != "position"}
                        for item in component_items
                    ],
                    evidence=[
                        {key: value for key, value in item.items() if key != "position"}
                        for item in evidence_items
                    ],
                    algorithm_version=str(row[9]),
                    summary=str(row[10]),
                    quality=DataQuality(str(row[11])),
                )
            except (ValueError, TypeError) as error:
                raise HistoricalAnalyticalArchiveError(
                    "historical diagnostic row is invalid"
                ) from error
            if sha256_hex(canonical_json_bytes(result)) != str(row[14]):
                raise HistoricalAnalyticalArchiveError(
                    "historical diagnostic checksum does not match"
                )
            results[diagnostic_id] = result
        return results

    def list_metric_ids_page(
        self, *, limit: int, after: HistoricalAnalyticalCursor | None = None
    ) -> tuple[UUID, ...]:
        self.ensure()
        _validate_limit(limit)
        return self._list_ids_page("historical_metric_results", "result_id", limit, after)

    def list_diagnostic_ids_page(
        self, *, limit: int, after: HistoricalAnalyticalCursor | None = None
    ) -> tuple[UUID, ...]:
        self.ensure()
        _validate_limit(limit)
        return self._list_ids_page("historical_diagnostic_results", "diagnostic_id", limit, after)

    def list_metrics_page(
        self,
        *,
        limit: int,
        asset_id: str | None = None,
        metric_key: str | None = None,
        known_to: datetime | None = None,
        after: HistoricalAnalyticalCursor | None = None,
    ) -> tuple[MetricResult, ...]:
        self.ensure()
        _validate_limit(limit)
        ids = self._select_page_ids(
            "historical_metric_results",
            "result_id",
            limit,
            after,
            (("asset_id", asset_id), ("metric_key", metric_key)),
            known_to,
        )
        if not ids:
            return ()
        models = self.get_metrics(ids)
        return tuple(models[item] for item in ids)

    def list_diagnostics_page(
        self,
        *,
        limit: int,
        asset_id: str | None = None,
        mode: str | None = None,
        known_to: datetime | None = None,
        after: HistoricalAnalyticalCursor | None = None,
    ) -> tuple[DiagnosticResult, ...]:
        self.ensure()
        _validate_limit(limit)
        ids = self._select_page_ids(
            "historical_diagnostic_results",
            "diagnostic_id",
            limit,
            after,
            (("asset_id", asset_id), ("mode", mode)),
            known_to,
        )
        if not ids:
            return ()
        models = self.get_diagnostics(ids)
        return tuple(models[item] for item in ids)

    def _select_page_ids(
        self,
        table: str,
        identifier_column: str,
        limit: int,
        after: HistoricalAnalyticalCursor | None,
        filters: Sequence[tuple[str, str | None]],
        known_to: datetime | None,
    ) -> tuple[UUID, ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in filters:
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if known_to is not None:
            clauses.append("available_at <= ?")
            parameters.append(_instant_text(known_to))
        if after is not None:
            clauses.append(
                "(available_at > ? OR (available_at = ? AND " + identifier_column + " > ?))"
            )
            at = _instant_text(after.available_at)
            parameters.extend((at, at, str(after.identifier)))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._connection.execute(
            f"SELECT {identifier_column} FROM {table}{where} "
            f"ORDER BY available_at, {identifier_column} LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return tuple(UUID(str(row[0])) for row in rows)

    def _list_ids_page(
        self,
        table: str,
        identifier_column: str,
        limit: int,
        after: HistoricalAnalyticalCursor | None,
    ) -> tuple[UUID, ...]:
        return self._select_page_ids(table, identifier_column, limit, after, (), None)

    def _select_by_ids(
        self, table: str, key: str, identifiers: Collection[UUID]
    ) -> list[tuple[object, ...]]:
        ordered = tuple(sorted(set(identifiers), key=str))
        rows: list[tuple[object, ...]] = []
        for chunk in _chunks(ordered, _BATCH - 3):
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                self._connection.execute(
                    f"SELECT * FROM {table} WHERE {key} IN ({placeholders}) ORDER BY {key}",
                    [str(item) for item in chunk],
                ).fetchall()
            )
        return rows

    def _list_diagnostic_children(
        self, table: str, projection: str, diagnostic_ids: Sequence[str]
    ) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        for chunk in _chunks(diagnostic_ids, _BATCH - 3):
            placeholders = ",".join("?" for _ in chunk)
            cursor: tuple[str, int] | None = None
            while True:
                predicate = f"diagnostic_id IN ({placeholders})"
                parameters: list[object] = list(chunk)
                if cursor is not None:
                    predicate += " AND (diagnostic_id > ? OR (diagnostic_id = ? AND position > ?))"
                    parameters.extend((cursor[0], cursor[0], cursor[1]))
                page = self._connection.execute(
                    f"SELECT {projection} FROM {table} WHERE {predicate} "
                    "ORDER BY diagnostic_id, position LIMIT 256",
                    parameters,
                ).fetchall()
                if not page:
                    break
                rows.extend(page)
                cursor = (str(page[-1][0]), int(page[-1][1]))
        return rows

    def _save_sequences_batch(
        self, requests: Sequence[tuple[str, Sequence[UUID]]]
    ) -> tuple[str, ...]:
        """Intern many shared link sequences with chunked metadata and member queries."""
        request_ids: list[str] = []
        sequence_values: dict[str, tuple[str, tuple[UUID, ...]]] = {}
        for link_type, identifiers in requests:
            values = tuple(identifiers)
            sequence_id = _sequence_id(link_type, values)
            previous = sequence_values.get(sequence_id)
            if previous is not None and previous != (link_type, values):
                raise HistoricalAnalyticalArchiveError(
                    "historical analytical sequence conflicts with its hash"
                )
            sequence_values[sequence_id] = (link_type, values)
            request_ids.append(sequence_id)
        if not request_ids:
            return ()

        all_sequence_ids = tuple(sorted(sequence_values))
        existing_sequence_rows = self._select_by_ids(
            "historical_analytical_sequences", "sequence_id", all_sequence_ids
        )
        existing_sequence_ids = {str(row[0]) for row in existing_sequence_rows}
        if existing_sequence_ids:
            loaded = self._load_sequences(tuple(sorted(existing_sequence_ids)))
            for sequence_id in existing_sequence_ids:
                link_type, identifiers = sequence_values[sequence_id]
                if (
                    loaded[sequence_id].link_type != link_type
                    or loaded[sequence_id].identifiers != identifiers
                ):
                    raise HistoricalAnalyticalArchiveError(
                        "historical analytical sequence conflicts with its hash"
                    )

        missing_sequence_ids = tuple(
            identifier for identifier in all_sequence_ids if identifier not in existing_sequence_ids
        )
        if missing_sequence_ids:
            segment_values: dict[str, tuple[str, tuple[UUID, ...]]] = {}
            sequence_rows: list[tuple[object, ...]] = []
            reference_rows: list[tuple[object, ...]] = []
            for sequence_id in missing_sequence_ids:
                link_type, identifiers = sequence_values[sequence_id]
                sequence_rows.append(
                    (
                        sequence_id,
                        link_type,
                        len(identifiers),
                        _sequence_hash(link_type, identifiers),
                    )
                )
                for position, start in enumerate(range(0, len(identifiers), _BATCH)):
                    items = tuple(identifiers[start : start + _BATCH])
                    segment_id = _segment_hash(link_type, items)
                    previous = segment_values.get(segment_id)
                    if previous is not None and previous != (link_type, items):
                        raise HistoricalAnalyticalArchiveError(
                            "historical analytical segment conflicts with its hash"
                        )
                    segment_values[segment_id] = (link_type, items)
                    reference_rows.append((sequence_id, position, segment_id))

            segment_ids = tuple(sorted(segment_values))
            existing_segment_rows = self._select_by_ids(
                "historical_analytical_segments", "segment_id", segment_ids
            )
            existing_segment_ids = {str(row[0]) for row in existing_segment_rows}
            if existing_segment_ids:
                loaded_segments = self._load_segments(tuple(sorted(existing_segment_ids)))
                for segment_id in existing_segment_ids:
                    link_type, identifiers = segment_values[segment_id]
                    if loaded_segments[segment_id] != (link_type, identifiers):
                        raise HistoricalAnalyticalArchiveError(
                            "historical analytical segment conflicts with its hash"
                        )

            new_segment_ids = tuple(
                identifier for identifier in segment_ids if identifier not in existing_segment_ids
            )
            segment_rows = [
                (
                    segment_id,
                    segment_values[segment_id][0],
                    len(segment_values[segment_id][1]),
                    segment_id,
                )
                for segment_id in new_segment_ids
            ]
            member_rows = [
                (segment_id, position, str(identifier))
                for segment_id in new_segment_ids
                for position, identifier in enumerate(segment_values[segment_id][1])
            ]
            if segment_rows:
                _executemany_bounded(
                    self._connection,
                    "INSERT INTO historical_analytical_segments VALUES (?, ?, ?, ?) ",
                    segment_rows,
                )
            if member_rows:
                _executemany_bounded(
                    self._connection,
                    "INSERT INTO historical_analytical_segment_members VALUES (?, ?, ?)",
                    member_rows,
                )
            _executemany_bounded(
                self._connection,
                "INSERT INTO historical_analytical_sequences VALUES (?, ?, ?, ?)",
                sequence_rows,
            )
            if reference_rows:
                _executemany_bounded(
                    self._connection,
                    "INSERT INTO historical_analytical_sequence_segments VALUES (?, ?, ?)",
                    reference_rows,
                )
            loaded_sequences = self._load_sequences(missing_sequence_ids)
            for sequence_id in missing_sequence_ids:
                link_type, identifiers = sequence_values[sequence_id]
                if (
                    loaded_sequences[sequence_id].link_type != link_type
                    or loaded_sequences[sequence_id].identifiers != identifiers
                ):
                    raise HistoricalAnalyticalArchiveError(
                        "historical analytical sequence conflicts with its hash"
                    )

        for sequence_id, (link_type, identifiers) in sequence_values.items():
            self._sequence_cache[(link_type, identifiers)] = sequence_id
        return tuple(request_ids)

    def _load_segments(self, segment_ids: Sequence[str]) -> dict[str, tuple[str, tuple[UUID, ...]]]:
        """Hydrate and checksum segment metadata and ordered members in bounded pages."""
        ordered = tuple(sorted(set(segment_ids)))
        if not ordered:
            return {}
        if len(ordered) > _BATCH:
            output: dict[str, tuple[str, tuple[UUID, ...]]] = {}
            for chunk in _chunks(ordered):
                output.update(self._load_segments(chunk))
            return output
        rows: list[tuple[object, ...]] = []
        for chunk in _chunks(ordered, _BATCH - 3):
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                self._connection.execute(
                    "SELECT segment_id, link_type, item_count, segment_hash "
                    "FROM historical_analytical_segments "
                    f"WHERE segment_id IN ({placeholders})",
                    list(chunk),
                ).fetchall()
            )
        metadata = {str(row[0]): (str(row[1]), int(row[2]), str(row[3])) for row in rows}
        if set(metadata) != set(ordered):
            raise HistoricalAnalyticalArchiveError("historical analytical segment is missing")
        members: dict[str, list[UUID]] = {identifier: [] for identifier in ordered}
        for chunk in _chunks(ordered, _BATCH - 3):
            placeholders = ",".join("?" for _ in chunk)
            cursor: tuple[str, int] | None = None
            while True:
                predicate = f"segment_id IN ({placeholders})"
                parameters: list[object] = list(chunk)
                if cursor is not None:
                    predicate += " AND (segment_id > ? OR (segment_id = ? AND position > ?))"
                    parameters.extend((cursor[0], cursor[0], cursor[1]))
                page = self._connection.execute(
                    "SELECT segment_id, position, identifier "
                    "FROM historical_analytical_segment_members "
                    f"WHERE {predicate} ORDER BY segment_id, position LIMIT 256",
                    parameters,
                ).fetchall()
                if not page:
                    break
                for row in page:
                    segment_id = str(row[0])
                    position = int(row[1])
                    if position != len(members[segment_id]):
                        raise HistoricalAnalyticalArchiveError(
                            "historical analytical segment order is corrupt"
                        )
                    try:
                        members[segment_id].append(UUID(str(row[2])))
                    except ValueError as error:
                        raise HistoricalAnalyticalArchiveError(
                            "historical analytical UUID link is malformed"
                        ) from error
                cursor = (str(page[-1][0]), int(page[-1][1]))
        output: dict[str, tuple[str, tuple[UUID, ...]]] = {}
        for segment_id, values in members.items():
            link_type, count, digest = metadata[segment_id]
            if (
                not 1 <= count <= _BATCH
                or len(values) != count
                or _segment_hash(link_type, values) != digest
                or digest != segment_id
            ):
                raise HistoricalAnalyticalArchiveError(
                    "historical analytical segment checksum does not match"
                )
            output[segment_id] = (link_type, tuple(values))
        return output

    def _load_sequences(
        self, sequence_ids: Collection[str]
    ) -> dict[str, HistoricalAnalyticalSequence]:
        ordered = tuple(sorted(set(sequence_ids)))
        if not ordered:
            return {}
        if len(ordered) > _BATCH:
            output: dict[str, HistoricalAnalyticalSequence] = {}
            for chunk in _chunks(ordered):
                output.update(self._load_sequences(chunk))
            return output
        metadata: dict[str, tuple[str, int, str]] = {}
        for chunk in _chunks(ordered):
            placeholders = ",".join("?" for _ in chunk)
            rows = self._connection.execute(
                "SELECT sequence_id, link_type, item_count, sequence_hash "
                "FROM historical_analytical_sequences "
                f"WHERE sequence_id IN ({placeholders})",
                list(chunk),
            ).fetchall()
            metadata.update({str(row[0]): (str(row[1]), int(row[2]), str(row[3])) for row in rows})
        if set(metadata) != set(ordered):
            raise HistoricalAnalyticalArchiveError("historical analytical sequence is missing")
        refs: dict[str, list[str]] = {item: [] for item in ordered}
        for chunk in _chunks(ordered, _BATCH - 3):
            placeholders = ",".join("?" for _ in chunk)
            cursor: tuple[str, int] | None = None
            while True:
                predicate = f"sequence_id IN ({placeholders})"
                parameters: list[object] = list(chunk)
                if cursor is not None:
                    predicate += " AND (sequence_id > ? OR (sequence_id = ? AND position > ?))"
                    parameters.extend((cursor[0], cursor[0], cursor[1]))
                page = self._connection.execute(
                    "SELECT sequence_id, position, segment_id "
                    "FROM historical_analytical_sequence_segments "
                    f"WHERE {predicate} ORDER BY sequence_id, position LIMIT 256",
                    parameters,
                ).fetchall()
                if not page:
                    break
                for row in page:
                    sequence_id = str(row[0])
                    position = int(row[1])
                    if position != len(refs[sequence_id]):
                        raise HistoricalAnalyticalArchiveError(
                            "historical analytical sequence order is corrupt"
                        )
                    refs[sequence_id].append(str(row[2]))
                cursor = (str(page[-1][0]), int(page[-1][1]))
        segment_ids = tuple(sorted({segment for group in refs.values() for segment in group}))
        segment_values = self._load_segments(segment_ids)
        output: dict[str, HistoricalAnalyticalSequence] = {}
        for sequence_id in ordered:
            link_type, count, digest = metadata[sequence_id]
            values: list[UUID] = []
            for segment_id in refs[sequence_id]:
                segment_type, identifiers = segment_values[segment_id]
                if segment_type != link_type:
                    raise HistoricalAnalyticalArchiveError(
                        "historical analytical link class changed"
                    )
                values.extend(identifiers)
            if (
                len(values) != count
                or _sequence_hash(link_type, values) != digest
                or digest != sequence_id
            ):
                raise HistoricalAnalyticalArchiveError(
                    "historical analytical sequence checksum does not match"
                )
            output[sequence_id] = HistoricalAnalyticalSequence(
                sequence_id=sequence_id, link_type=link_type, identifiers=tuple(values)
            )
        return output


def current_ids(values: Collection[str]) -> set[UUID]:
    """Convert already selected archive primary keys to typed IDs."""
    return {UUID(value) for value in values}


__all__ = [
    "HistoricalAnalyticalArchive",
    "HistoricalAnalyticalArchiveError",
    "HistoricalAnalyticalCursor",
    "HistoricalAnalyticalSequence",
    "HistoricalAnalyticalRowsSummary",
    "ensure_historical_analytical_archive_tables",
    "historical_analytical_archive_exists",
]
