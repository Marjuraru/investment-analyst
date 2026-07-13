"""Concrete DuckDB repositories for typed project models."""

from datetime import datetime
from uuid import UUID

from duckdb import DuckDBPyConnection
from pydantic import BaseModel

from investment_analyst.core.models import (
    Asset,
    DiagnosticMode,
    DiagnosticResult,
    MetricDefinition,
    MetricResult,
    NormalizedObservation,
    SourceDefinition,
)
from investment_analyst.storage.errors import RecordConflictError, RecordNotFoundError
from investment_analyst.storage.serialization import canonical_json_text, model_from_json


def _get_document[ModelT: BaseModel](
    connection: DuckDBPyConnection,
    *,
    table: str,
    key_column: str,
    identifier: str,
    model_type: type[ModelT],
) -> ModelT:
    row = connection.execute(
        f"SELECT document_json FROM {table} WHERE {key_column} = ?",  # noqa: S608
        [identifier],
    ).fetchone()
    if row is None:
        raise RecordNotFoundError(f"{table} record {identifier!r} was not found")
    return model_from_json(model_type, row[0])


def _list_documents[ModelT: BaseModel](
    connection: DuckDBPyConnection,
    *,
    sql: str,
    parameters: list[object],
    model_type: type[ModelT],
) -> list[ModelT]:
    rows = connection.execute(sql, parameters).fetchall()
    return [model_from_json(model_type, row[0]) for row in rows]


def _ensure_append_only(
    connection: DuckDBPyConnection,
    *,
    table: str,
    key_column: str,
    identifier: str,
    document_json: str,
) -> bool:
    row = connection.execute(
        f"SELECT document_json FROM {table} WHERE {key_column} = ?",  # noqa: S608
        [identifier],
    ).fetchone()
    if row is None:
        return True
    if row[0] == document_json:
        return False
    raise RecordConflictError(f"{table} identifier {identifier!r} already has different content")


class DuckDBAssetRepository:
    """DuckDB repository for explicitly updatable assets."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def upsert(self, asset: Asset) -> Asset:
        document = canonical_json_text(asset)
        self._connection.execute(
            """
            INSERT INTO assets (
                asset_id, symbol, asset_class, quote_currency, is_active, document_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (asset_id) DO UPDATE SET
                symbol = excluded.symbol,
                asset_class = excluded.asset_class,
                quote_currency = excluded.quote_currency,
                is_active = excluded.is_active,
                document_json = excluded.document_json
            """,
            [
                asset.asset_id,
                asset.symbol,
                asset.asset_class.value,
                asset.quote_currency,
                asset.is_active,
                document,
            ],
        )
        return asset

    def get(self, asset_id: str) -> Asset:
        return _get_document(
            self._connection,
            table="assets",
            key_column="asset_id",
            identifier=asset_id,
            model_type=Asset,
        )

    def list_all(self) -> list[Asset]:
        return _list_documents(
            self._connection,
            sql="SELECT document_json FROM assets ORDER BY asset_id",
            parameters=[],
            model_type=Asset,
        )


class DuckDBSourceDefinitionRepository:
    """DuckDB repository for explicitly updatable source definitions."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def upsert(self, source: SourceDefinition) -> SourceDefinition:
        document = canonical_json_text(source)
        self._connection.execute(
            """
            INSERT INTO source_definitions (
                source_id, provider_name, dataset_name, source_type, is_official, document_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id) DO UPDATE SET
                provider_name = excluded.provider_name,
                dataset_name = excluded.dataset_name,
                source_type = excluded.source_type,
                is_official = excluded.is_official,
                document_json = excluded.document_json
            """,
            [
                source.source_id,
                source.provider_name,
                source.dataset_name,
                source.source_type.value,
                source.is_official,
                document,
            ],
        )
        return source

    def get(self, source_id: str) -> SourceDefinition:
        return _get_document(
            self._connection,
            table="source_definitions",
            key_column="source_id",
            identifier=source_id,
            model_type=SourceDefinition,
        )

    def list_all(self) -> list[SourceDefinition]:
        return _list_documents(
            self._connection,
            sql="SELECT document_json FROM source_definitions ORDER BY source_id",
            parameters=[],
            model_type=SourceDefinition,
        )


class DuckDBObservationRepository:
    """Append-only DuckDB repository for normalized observations."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def save(self, observation: NormalizedObservation) -> NormalizedObservation:
        document = canonical_json_text(observation)
        identifier = str(observation.observation_id)
        if not _ensure_append_only(
            self._connection,
            table="normalized_observations",
            key_column="observation_id",
            identifier=identifier,
            document_json=document,
        ):
            return observation
        self._connection.execute(
            """
            INSERT INTO normalized_observations (
                observation_id, raw_record_id, asset_id, field_name, frequency,
                observed_at, period_end, available_at, quality, document_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                identifier,
                str(observation.raw_record_id),
                observation.asset_id,
                observation.field_name,
                observation.frequency.value,
                observation.observed_at,
                observation.period_end,
                observation.available_at,
                observation.quality.value,
                document,
            ],
        )
        return observation

    def get(self, observation_id: UUID) -> NormalizedObservation:
        return _get_document(
            self._connection,
            table="normalized_observations",
            key_column="observation_id",
            identifier=str(observation_id),
            model_type=NormalizedObservation,
        )

    def list(
        self,
        *,
        asset_id: str | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
    ) -> list[NormalizedObservation]:
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if available_from is not None:
            clauses.append("available_at >= ?")
            parameters.append(available_from)
        if available_to is not None:
            clauses.append("available_at <= ?")
            parameters.append(available_to)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return _list_documents(
            self._connection,
            sql=(
                "SELECT document_json FROM normalized_observations"
                f"{where} ORDER BY available_at, observation_id"
            ),
            parameters=parameters,
            model_type=NormalizedObservation,
        )


class DuckDBMetricDefinitionRepository:
    """DuckDB repository for explicitly updatable metric definitions."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def upsert(self, definition: MetricDefinition) -> MetricDefinition:
        document = canonical_json_text(definition)
        self._connection.execute(
            """
            INSERT INTO metric_definitions (
                metric_key, display_name, category, definition_version, document_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (metric_key) DO UPDATE SET
                display_name = excluded.display_name,
                category = excluded.category,
                definition_version = excluded.definition_version,
                document_json = excluded.document_json
            """,
            [
                definition.metric_key,
                definition.display_name,
                definition.category.value,
                definition.definition_version,
                document,
            ],
        )
        return definition

    def get(self, metric_key: str) -> MetricDefinition:
        return _get_document(
            self._connection,
            table="metric_definitions",
            key_column="metric_key",
            identifier=metric_key,
            model_type=MetricDefinition,
        )

    def list_all(self) -> list[MetricDefinition]:
        return _list_documents(
            self._connection,
            sql="SELECT document_json FROM metric_definitions ORDER BY metric_key",
            parameters=[],
            model_type=MetricDefinition,
        )


class DuckDBMetricResultRepository:
    """Append-only DuckDB repository for metric results."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def save(self, result: MetricResult) -> MetricResult:
        document = canonical_json_text(result)
        identifier = str(result.result_id)
        if not _ensure_append_only(
            self._connection,
            table="metric_results",
            key_column="result_id",
            identifier=identifier,
            document_json=document,
        ):
            return result
        self._connection.execute(
            """
            INSERT INTO metric_results (
                result_id, asset_id, metric_key, as_of, available_at,
                computed_at, quality, document_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                identifier,
                result.asset_id,
                result.metric_key,
                result.as_of,
                result.available_at,
                result.computed_at,
                result.quality.value,
                document,
            ],
        )
        return result

    def get(self, result_id: UUID) -> MetricResult:
        return _get_document(
            self._connection,
            table="metric_results",
            key_column="result_id",
            identifier=str(result_id),
            model_type=MetricResult,
        )

    def list(
        self,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[MetricResult]:
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if metric_key is not None:
            clauses.append("metric_key = ?")
            parameters.append(metric_key)
        if as_of_from is not None:
            clauses.append("as_of >= ?")
            parameters.append(as_of_from)
        if as_of_to is not None:
            clauses.append("as_of <= ?")
            parameters.append(as_of_to)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return _list_documents(
            self._connection,
            sql=(f"SELECT document_json FROM metric_results{where} ORDER BY as_of, result_id"),
            parameters=parameters,
            model_type=MetricResult,
        )


class DuckDBDiagnosticResultRepository:
    """Append-only DuckDB repository for diagnostic results."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def save(self, result: DiagnosticResult) -> DiagnosticResult:
        document = canonical_json_text(result)
        identifier = str(result.diagnostic_id)
        if not _ensure_append_only(
            self._connection,
            table="diagnostic_results",
            key_column="diagnostic_id",
            identifier=identifier,
            document_json=document,
        ):
            return result
        self._connection.execute(
            """
            INSERT INTO diagnostic_results (
                diagnostic_id, asset_id, mode, verdict, as_of, available_at,
                computed_at, quality, document_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                identifier,
                result.asset_id,
                result.mode.value,
                result.verdict.value,
                result.as_of,
                result.available_at,
                result.computed_at,
                result.quality.value,
                document,
            ],
        )
        return result

    def get(self, diagnostic_id: UUID) -> DiagnosticResult:
        return _get_document(
            self._connection,
            table="diagnostic_results",
            key_column="diagnostic_id",
            identifier=str(diagnostic_id),
            model_type=DiagnosticResult,
        )

    def list(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[DiagnosticResult]:
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if mode is not None:
            clauses.append("mode = ?")
            parameters.append(mode.value)
        if as_of_from is not None:
            clauses.append("as_of >= ?")
            parameters.append(as_of_from)
        if as_of_to is not None:
            clauses.append("as_of <= ?")
            parameters.append(as_of_to)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return _list_documents(
            self._connection,
            sql=(
                f"SELECT document_json FROM diagnostic_results{where} ORDER BY as_of, diagnostic_id"
            ),
            parameters=parameters,
            model_type=DiagnosticResult,
        )
