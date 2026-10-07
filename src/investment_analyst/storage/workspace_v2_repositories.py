"""Typed repository facades over the workspace v2 physical tables."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from datetime import UTC, date, datetime, time
from uuid import UUID

from duckdb import DuckDBPyConnection

from investment_analyst.analytics.analytical_access_models import (
    MetricIndexEntry,
    MetricSeriesQuery,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticMode,
    DiagnosticResult,
    MetricResult,
    NormalizedObservation,
    RawRecord,
)
from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.errors import RecordNotFoundError, StorageError
from investment_analyst.storage.observation_v2 import OBSERVATION_V2_TABLE
from investment_analyst.storage.raw_v2 import (
    WORKSPACE_RAW_JSON_PROJECTIONS_TABLE,
    RawV2Staging,
)

_PAGE = 256
_RAW_JSON_PATHS = {
    "report_manager": ("report", "manager_cik"),
    "outcome_filer": ("outcome", "filing", "filer_cik"),
    "position_report": ("position", "report_id"),
    "semantics_manager": ("artifact", "manager_cik"),
    "correspondence_artifact": ("correspondence", "artifact_id"),
    "correspondence_manager": ("correspondence", "manager_cik"),
}


def _instant_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("query instant must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _period_bound(value: datetime | date, *, end: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    end_time = time.max.replace(microsecond=0) if end else time.min
    return datetime.combine(value, end_time, tzinfo=UTC)


class WorkspaceV2RawRecordRepository:
    """Raw repository preserving the v1 facade over hashed v2 blobs and index."""

    def __init__(self, staging: RawV2Staging, connection: DuckDBPyConnection) -> None:
        self._staging = staging
        self._connection = connection

    def save(self, record: RawRecord) -> RawRecord:
        return self._staging.save(record)

    def save_many(self, records: Collection[RawRecord]) -> BatchWriteReceipt:
        return self._staging.save_many(records)

    save_batch = save_many

    def get(self, record_id: UUID) -> RawRecord:
        return self._staging.get(record_id)

    def get_many(self, record_ids: Collection[UUID]) -> dict[UUID, RawRecord]:
        ordered = tuple(sorted(set(record_ids), key=str))
        output: dict[UUID, RawRecord] = {}
        for start in range(0, len(ordered), _PAGE):
            output.update(self._staging.get_many(ordered[start : start + _PAGE]))
        return output

    def list_record_ids(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> list[UUID]:
        return self._staging.list_record_ids(
            asset_id=asset_id,
            source_id=source_id,
            schema_version=schema_version,
            available_to=available_to,
            received_from=received_from,
            received_to=received_to,
        )

    def list(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> list[RawRecord]:
        identifiers = self.list_record_ids(
            asset_id=asset_id,
            source_id=source_id,
            schema_version=schema_version,
            available_to=available_to,
            received_from=received_from,
            received_to=received_to,
        )
        records = self.get_many(identifiers)
        return [records[identifier] for identifier in identifiers]

    def count(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> int:
        return self._staging.count_records(
            asset_id=asset_id,
            source_id=source_id,
            schema_version=schema_version,
            available_to=available_to,
            received_from=received_from,
            received_to=received_to,
        )

    def available_at_bounds(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
    ) -> tuple[datetime | None, datetime | None]:
        return self._staging.available_at_bounds(
            asset_id=asset_id, source_id=source_id, schema_version=schema_version
        )

    def list_import_page(
        self,
        *,
        limit: int,
        after_received_at: datetime | None = None,
        after_record_id: UUID | None = None,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
    ) -> list[UUID]:
        return self._staging.list_inventory_page(
            limit=limit,
            after_received_at=after_received_at,
            after_record_id=after_record_id,
            asset_id=asset_id,
            source_id=source_id,
            schema_version=schema_version,
            available_to=available_to,
        )

    def select_record_ids_by_json_field(
        self,
        *,
        field: str,
        values: Collection[str],
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
    ) -> list[UUID]:
        """Select indexed payload fields in SQL without hydrating unrelated blobs."""
        if field not in _RAW_JSON_PATHS:
            raise StorageError("raw record JSON field selection is not supported")
        selected_values = tuple(sorted(set(values)))
        if not selected_values:
            return []
        clauses = [
            "p.field_name = ?",
            f"p.field_value IN ({', '.join('?' for _ in selected_values)})",
        ]
        parameters: list[object] = [field, *selected_values]
        if source_id is not None:
            clauses.append("r.source_id = ?")
            parameters.append(source_id)
        if schema_version is not None:
            clauses.append("r.schema_version = ?")
            parameters.append(schema_version)
        if available_to is not None:
            clauses.append("r.available_at <= ?")
            parameters.append(_instant_text(available_to))
        rows = self._connection.execute(
            f"SELECT r.record_id FROM raw_v2_index AS r "
            f"JOIN {WORKSPACE_RAW_JSON_PROJECTIONS_TABLE} AS p ON p.record_id = r.record_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY r.received_at, r.record_id",
            parameters,
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def verify_index_integrity(self, record_ids: Collection[UUID]) -> int:
        ordered = tuple(sorted(set(record_ids), key=str))
        if len(ordered) > 1_000:
            raise StorageError(
                "raw record index integrity verification exceeds the bounded record limit"
            )
        for start in range(0, len(ordered), _PAGE):
            self._staging.get_many(ordered[start : start + _PAGE])
        return len(ordered)


class WorkspaceV2ObservationRepository:
    """Observation facade with SQL pushdown and bounded model hydration."""

    def __init__(self, staging: RawV2Staging, connection: DuckDBPyConnection) -> None:
        self._staging = staging
        self._connection = connection

    def save(self, observation: NormalizedObservation) -> NormalizedObservation:
        self._staging.save_observations([observation])
        return observation

    def save_many(self, observations: Collection[NormalizedObservation]) -> BatchWriteReceipt:
        typed = tuple(observations)
        created: list[UUID] = []
        reused: list[UUID] = []
        for start in range(0, len(typed), _PAGE):
            receipt = self._staging.save_observations(typed[start : start + _PAGE])
            created.extend(receipt.created_ids)
            reused.extend(receipt.reused_ids)
        return BatchWriteReceipt(created_ids=tuple(created), reused_ids=tuple(reused))

    save_batch = save_many

    def get(self, observation_id: UUID) -> NormalizedObservation:
        return self._staging.get_observations([observation_id])[observation_id]

    def get_many(self, observation_ids: Collection[UUID]) -> dict[UUID, NormalizedObservation]:
        ordered = tuple(sorted(set(observation_ids), key=str))
        output: dict[UUID, NormalizedObservation] = {}
        for start in range(0, len(ordered), _PAGE):
            output.update(self._staging.get_observations(ordered[start : start + _PAGE]))
        return output

    def get_existing(self, observation_ids: Collection[UUID]) -> dict[UUID, NormalizedObservation]:
        ordered = tuple(sorted(set(observation_ids), key=str))
        found: set[UUID] = set()
        for start in range(0, len(ordered), _PAGE):
            chunk = ordered[start : start + _PAGE]
            if not chunk:
                continue
            rows = self._connection.execute(
                f"SELECT observation_id FROM {OBSERVATION_V2_TABLE} "
                f"WHERE observation_id IN ({', '.join('?' for _ in chunk)})",
                [str(identifier) for identifier in chunk],
            ).fetchall()
            found.update(UUID(str(row[0])) for row in rows)
        return self.get_many(found) if found else {}

    def _filter_clauses(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        field_name: str | None = None,
        field_names: Collection[str] | None = None,
        frequency: DataFrequency | None = None,
        quality: DataQuality | None = None,
        observed_from: datetime | None = None,
        observed_before: datetime | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
        period_end_from: datetime | date | None = None,
        period_end_to: datetime | date | None = None,
    ) -> tuple[list[str], list[object]]:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("asset_id", asset_id),
            ("source_id", source_id),
            ("field_name", field_name),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if field_names is not None:
            values = tuple(sorted(set(field_names)))
            if not values:
                clauses.append("1 = 0")
            else:
                clauses.append(f"field_name IN ({', '.join('?' for _ in values)})")
                parameters.extend(values)
        if frequency is not None:
            clauses.append("frequency = ?")
            parameters.append(frequency.value)
        if quality is not None:
            clauses.append("quality = ?")
            parameters.append(quality.value)
        for column, value, operator in (
            ("observed_at", observed_from, ">="),
            ("observed_at", observed_before, "<"),
            ("available_at", available_from, ">="),
            ("available_at", available_to, "<="),
        ):
            if value is not None:
                clauses.append(f"{column} {operator} ?")
                parameters.append(_instant_text(value))
        if period_end_from is not None:
            clauses.append("period_end >= ?")
            parameters.append(_instant_text(_period_bound(period_end_from)))
        if period_end_to is not None:
            clauses.append("period_end <= ?")
            parameters.append(_instant_text(_period_bound(period_end_to, end=True)))
        return clauses, parameters

    def _matching_id_page(
        self,
        clauses: list[str],
        parameters: list[object],
        *,
        after_available_at: datetime | None,
        after_observation_id: UUID | None,
    ) -> list[UUID]:
        page_clauses = list(clauses)
        page_parameters = list(parameters)
        if after_available_at is not None and after_observation_id is not None:
            instant = _instant_text(after_available_at)
            page_clauses.append("(available_at, observation_id) > (?, ?)")
            page_parameters.extend([instant, str(after_observation_id)])
        where = f" WHERE {' AND '.join(page_clauses)}" if page_clauses else ""
        rows = self._connection.execute(
            f"SELECT observation_id FROM {OBSERVATION_V2_TABLE}{where} "
            "ORDER BY available_at, observation_id LIMIT ?",
            [*page_parameters, _PAGE],
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def list(self, **filters: object) -> list[NormalizedObservation]:
        clauses, parameters = self._filter_clauses(**filters)
        output: list[NormalizedObservation] = []
        after_available_at: datetime | None = None
        after_observation_id: UUID | None = None
        while True:
            identifiers = self._matching_id_page(
                clauses,
                parameters,
                after_available_at=after_available_at,
                after_observation_id=after_observation_id,
            )
            if not identifiers:
                return output
            models = self.get_many(identifiers)
            output.extend(models[identifier] for identifier in identifiers)
            last = models[identifiers[-1]]
            after_available_at = last.available_at
            after_observation_id = last.observation_id

    def count(self, **filters: object) -> int:
        clauses, parameters = self._filter_clauses(**filters)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._connection.execute(
            f"SELECT count(*) FROM {OBSERVATION_V2_TABLE}{where}", parameters
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_observation_import_page(
        self,
        *,
        limit: int,
        after_available_at: datetime | None = None,
        after_observation_id: UUID | None = None,
    ) -> list[UUID]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _PAGE:
            raise StorageError("observation import page limit must be between 1 and 256")
        if (after_available_at is None) != (after_observation_id is None):
            raise StorageError("observation import cursor requires both fields together")
        clauses: list[str] = []
        parameters: list[object] = []
        if after_available_at is not None and after_observation_id is not None:
            clauses.append("(available_at, observation_id) > (?, ?)")
            parameters.extend([_instant_text(after_available_at), str(after_observation_id)])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT observation_id FROM {OBSERVATION_V2_TABLE}{where} "
            "ORDER BY available_at, observation_id LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def minimum_available_at(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        frequency: DataFrequency | None = None,
        quality: DataQuality | None = None,
        transformation_version: str | None = None,
    ) -> datetime | None:
        clauses, parameters = self._filter_clauses(
            asset_id=asset_id, source_id=source_id, frequency=frequency, quality=quality
        )
        if transformation_version is not None:
            clauses.append("transformation_version = ?")
            parameters.append(transformation_version)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._connection.execute(
            f"SELECT min(available_at) FROM {OBSERVATION_V2_TABLE}{where}", parameters
        ).fetchone()
        return datetime.fromisoformat(str(row[0])).astimezone(UTC) if row and row[0] else None

    def maximum_available_at(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        frequency: DataFrequency | None = None,
        quality: DataQuality | None = None,
        transformation_version: str | None = None,
        observed_from: datetime | None = None,
        observed_before: datetime | None = None,
        available_to: datetime | None = None,
    ) -> datetime | None:
        clauses, parameters = self._filter_clauses(
            asset_id=asset_id,
            source_id=source_id,
            frequency=frequency,
            quality=quality,
            observed_from=observed_from,
            observed_before=observed_before,
            available_to=available_to,
        )
        if transformation_version is not None:
            clauses.append("transformation_version = ?")
            parameters.append(transformation_version)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._connection.execute(
            f"SELECT max(available_at) FROM {OBSERVATION_V2_TABLE}{where}", parameters
        ).fetchone()
        return datetime.fromisoformat(str(row[0])).astimezone(UTC) if row and row[0] else None

    def observed_at_bounds(self, **filters: object) -> tuple[datetime | None, datetime | None]:
        clauses, parameters = self._filter_clauses(**filters)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._connection.execute(
            f"SELECT min(observed_at), max(observed_at) FROM {OBSERVATION_V2_TABLE}{where}",
            parameters,
        ).fetchone()
        if row is None:
            return None, None
        values = [
            datetime.fromisoformat(str(value)).astimezone(UTC) if value else None for value in row
        ]
        return values[0], values[1]

    def list_ids_for_manager_observation_references(
        self, *, asset_id: str, available_to: datetime | None = None
    ) -> list[UUID]:
        from investment_analyst.evidence.sec_institutional_observations.definitions import (
            SOURCE_ID,
        )

        clauses = ["asset_id = ?", "source_id = ?"]
        parameters: list[object] = [asset_id, SOURCE_ID]
        if available_to is not None:
            clauses.append("available_at <= ?")
            parameters.append(_instant_text(available_to))
        rows = self._connection.execute(
            f"SELECT observation_id FROM {OBSERVATION_V2_TABLE} "
            f"WHERE {' AND '.join(clauses)} ORDER BY available_at, observation_id",
            parameters,
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def select_ids_for_manager_observation_references(
        self, candidate_ids: Collection[UUID], *, manager: str
    ) -> list[UUID]:
        ordered = tuple(dict.fromkeys(candidate_ids))
        selected: list[UUID] = []
        found: set[UUID] = set()
        for start in range(0, len(ordered), _PAGE):
            chunk = ordered[start : start + _PAGE]
            if not chunk:
                continue
            rows = self._connection.execute(
                f"SELECT observation_id, source_record_key FROM {OBSERVATION_V2_TABLE} "
                f"WHERE observation_id IN ({', '.join('?' for _ in chunk)})",
                [str(identifier) for identifier in chunk],
            ).fetchall()
            indexed = {UUID(str(row[0])): row[1] for row in rows}
            found.update(indexed)
            for identifier in chunk:
                if identifier not in indexed:
                    continue
                key = indexed[identifier]
                if key is None:
                    continue
                try:
                    parsed = json.loads(str(key))
                    if isinstance(parsed, str):
                        parsed = json.loads(parsed)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict) and parsed.get("manager_cik") == manager:
                    selected.append(identifier)
        missing = [identifier for identifier in ordered if identifier not in found]
        if missing:
            raise RecordNotFoundError(f"selected institutional observation {missing[0]} is absent")
        return selected

    def field_name(self, observation_id: UUID) -> str | None:
        row = self._connection.execute(
            f"SELECT field_name FROM {OBSERVATION_V2_TABLE} WHERE observation_id = ?",
            [str(observation_id)],
        ).fetchone()
        return str(row[0]) if row is not None else None


class WorkspaceV2MetricResultRepository:
    """Compact metric repository with v1 query and paging behavior."""

    def __init__(self, compact: CompactAnalyticalStore) -> None:
        self._compact = compact

    def save(self, result: MetricResult) -> MetricResult:
        self._compact.save_metrics([result])
        return result

    def save_many(self, results: Collection[MetricResult]) -> BatchWriteReceipt:
        typed = tuple(results)
        created: list[UUID] = []
        reused: list[UUID] = []
        for start in range(0, len(typed), _PAGE):
            receipt = self._compact.save_metrics(typed[start : start + _PAGE])
            created.extend(receipt.created_ids)
            reused.extend(receipt.reused_ids)
        return BatchWriteReceipt(created_ids=tuple(created), reused_ids=tuple(reused))

    save_batch = save_many

    def get(self, result_id: UUID) -> MetricResult:
        return self._compact.get_metrics([result_id])[result_id]

    def get_many(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]:
        ordered = tuple(sorted(set(result_ids), key=str))
        output: dict[UUID, MetricResult] = {}
        for start in range(0, len(ordered), _PAGE):
            output.update(self._compact.get_metrics(ordered[start : start + _PAGE]))
        return output

    def get_existing(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]:
        return self._compact.get_existing_metrics(result_ids)

    def find_market_metric_candidates(
        self,
        *,
        asset_id: str,
        source_id: str,
        known_at: datetime,
        metric_keys: Collection[str],
        timestamps: Collection[datetime],
    ) -> dict[UUID, MetricResult]:
        """Hydrate only exact, source-scoped market results visible at the requested cut."""
        identifiers = self._compact.select_market_metric_ids(
            asset_id=asset_id,
            source_id=source_id,
            known_at=known_at,
            metric_keys=metric_keys,
            timestamps=timestamps,
        )
        output: dict[UUID, MetricResult] = {}
        for start in range(0, len(identifiers), _PAGE):
            output.update(self._compact.get_metrics(identifiers[start : start + _PAGE]))
        return output

    def list_metric_index_page(self, query: MetricSeriesQuery) -> tuple[MetricIndexEntry, ...]:
        return self._compact.list_metric_index_page(query)

    def list(
        self,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        metric_keys: tuple[str, ...] | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[MetricResult]:
        identifiers = self._compact.select_metric_ids(
            asset_id=asset_id,
            metric_key=metric_key,
            metric_keys=metric_keys,
            as_of_from=as_of_from,
            as_of_to=as_of_to,
        )
        models = self.get_many(identifiers)
        return sorted(models.values(), key=lambda item: (item.as_of, item.result_id))

    def count(
        self,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        metric_keys: tuple[str, ...] | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> int:
        return self._compact.count_metric_results(
            asset_id=asset_id,
            metric_key=metric_key,
            metric_keys=metric_keys,
            as_of_from=as_of_from,
            as_of_to=as_of_to,
        )

    def list_ids(
        self,
        *,
        asset_id: str | None = None,
        metric_keys: Collection[str] | None = None,
        available_to: datetime | None = None,
        as_of_from: datetime | None = None,
        as_of_before: datetime | None = None,
        parameter_equals: Mapping[str, str] | None = None,
        parameter_date_range: tuple[str, str] | None = None,
        cut_known_at: datetime | None = None,
        legacy_known_at_to: datetime | None = None,
    ) -> list[UUID]:
        return self._compact.select_metric_ids(
            asset_id=asset_id,
            metric_keys=metric_keys,
            available_to=available_to,
            as_of_from=as_of_from,
            as_of_before=as_of_before,
            parameter_equals=parameter_equals,
            parameter_date_range=parameter_date_range,
            cut_known_at=cut_known_at,
            legacy_known_at_to=legacy_known_at_to,
        )

    def list_import_page(
        self,
        *,
        limit: int,
        after_available_at: datetime | None = None,
        after_result_id: UUID | None = None,
    ) -> tuple[UUID, ...]:
        return tuple(
            self._compact.list_metric_ids_page(
                limit=limit,
                after_available_at=after_available_at,
                after_result_id=after_result_id,
            )
        )


class WorkspaceV2DiagnosticResultRepository:
    """Compact diagnostic repository with v1 query and paging behavior."""

    def __init__(self, compact: CompactAnalyticalStore) -> None:
        self._compact = compact

    def save(self, result: DiagnosticResult) -> DiagnosticResult:
        self._compact.save_diagnostics([result])
        return result

    def save_many(self, results: Collection[DiagnosticResult]) -> BatchWriteReceipt:
        typed = tuple(results)
        created: list[UUID] = []
        reused: list[UUID] = []
        for start in range(0, len(typed), _PAGE):
            receipt = self._compact.save_diagnostics(typed[start : start + _PAGE])
            created.extend(receipt.created_ids)
            reused.extend(receipt.reused_ids)
        return BatchWriteReceipt(created_ids=tuple(created), reused_ids=tuple(reused))

    save_batch = save_many

    def get(self, diagnostic_id: UUID) -> DiagnosticResult:
        return self._compact.get_diagnostics([diagnostic_id])[diagnostic_id]

    def get_many(self, diagnostic_ids: Collection[UUID]) -> dict[UUID, DiagnosticResult]:
        ordered = tuple(sorted(set(diagnostic_ids), key=str))
        output: dict[UUID, DiagnosticResult] = {}
        for start in range(0, len(ordered), _PAGE):
            output.update(self._compact.get_diagnostics(ordered[start : start + _PAGE]))
        return output

    def list(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[DiagnosticResult]:
        identifiers = self._compact.select_diagnostic_ids(
            asset_id=asset_id, mode=mode, as_of_from=as_of_from, as_of_to=as_of_to
        )
        models = self.get_many(identifiers)
        return sorted(models.values(), key=lambda item: (item.as_of, item.diagnostic_id))

    def count(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> int:
        return self._compact.count_diagnostic_results(
            asset_id=asset_id, mode=mode, as_of_from=as_of_from, as_of_to=as_of_to
        )

    def list_import_page(
        self,
        *,
        limit: int,
        after_available_at: datetime | None = None,
        after_diagnostic_id: UUID | None = None,
    ) -> tuple[UUID, ...]:
        return tuple(
            self._compact.list_diagnostic_ids_page(
                limit=limit,
                after_available_at=after_available_at,
                after_diagnostic_id=after_diagnostic_id,
            )
        )
