"""Content-bound dependency signatures for local read reuse.

A signature names exactly the workspace, code/contract/catalog versions, request
arguments, point-in-time cut and the visible dependency rows that can affect a
read result. It never infers equality from counts, database size, mtimes or
timestamps, and it never hydrates analytical models.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from duckdb import DuckDBPyConnection
from pydantic import AfterValidator, ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.observation_v2 import (
    OBSERVATION_V2_COLUMNS,
    observation_v2_table_exists,
)

READ_DEPENDENCY_SIGNATURE_SCHEMA = "read-dependency-signature-v1"
READ_DEPENDENCY_CODE_VERSION = "data-chassis-47-read-v1"
READ_DEPENDENCY_CONTRACT_VERSION = "read-dependency-signature-v1"

_SEC_DOCUMENT_SOURCE_ID = "sec-edgar:primary-documents"
_INSTITUTIONAL_SOURCE_IDS = (
    "sec-edgar:institutional-holdings",
    "sec-edgar:institutional-holdings:reports",
    "sec-edgar:institutional-holdings:positions",
)
_ABSENCE_DIGEST = hashlib.sha256(b"read-dependency-absence-v1").hexdigest()


class ReadDependencyError(RuntimeError):
    """Raised when a dependency signature cannot be proven."""


class ReadSignatureUnavailable(ReadDependencyError):
    """Raised when the storage layout cannot bind a signature to a snapshot."""


Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReadDependencyKind(StrEnum):
    """Closed dependency families recorded by a read signature."""

    OBSERVATION = "observation"
    RAW_RECORD = "raw-record"
    METRIC_RESULT = "metric-result"
    SEQUENCE = "sequence"
    PARAMETERS = "parameters"
    CATALOG = "catalog"
    ABSENCE = "absence"


class ReadDependencyFamily(StrEnum):
    """Analytical read families with a proven dependency projection."""

    MARKET_BARS = "market-bars"
    FUNDAMENTALS = "fundamentals"
    DOCUMENTS = "documents"
    INSTITUTIONAL = "institutional"
    METRICS = "metrics"
    VALUATION = "valuation"
    COVERAGE = "coverage"
    LISTED_COMPANY_REPORT = "listed-company-report"


class _InstantValidator:
    @staticmethod
    def _validate(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("read dependency timestamps must include timezone information")
        return value


def _validate_optional_instant(value: datetime | None) -> datetime | None:
    return _InstantValidator._validate(value)


OptionalUTCDateTime = Annotated[datetime | None, AfterValidator(_validate_optional_instant)]


class ReadDependency(ContractModel):
    """One visible row or explicit absence that participates in a result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: ReadDependencyKind
    dependency_id: NonEmptyStr
    content_sha256: Sha256Hex
    revision: NonEmptyStr | None = None


class ReadDependencyQuery(ContractModel):
    """Complete semantic selection requested from one read endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    family: ReadDependencyFamily
    endpoint: NonEmptyStr
    asset_ids: tuple[NonEmptyStr, ...]
    source_ids: tuple[NonEmptyStr, ...] = ()
    field_names: tuple[NonEmptyStr, ...] = ()
    metric_keys: tuple[NonEmptyStr, ...] = ()
    frequency: NonEmptyStr | None = None
    start: OptionalUTCDateTime = None
    end: OptionalUTCDateTime = None
    known_at: UTCDateTime
    selector: NonEmptyStr = "all"

    @model_validator(mode="after")
    def _validate_selection(self) -> ReadDependencyQuery:
        if self.asset_ids != tuple(sorted(set(self.asset_ids))):
            raise ValueError("asset_ids must be sorted and unique")
        if self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError("source_ids must be sorted and unique")
        if self.field_names != tuple(sorted(set(self.field_names))):
            raise ValueError("field_names must be sorted and unique")
        if self.metric_keys != tuple(sorted(set(self.metric_keys))):
            raise ValueError("metric_keys must be sorted and unique")
        if (self.start is None) != (self.end is None):
            raise ValueError("read dependency range requires both bounds")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("read dependency range must be non-empty")
        if self.family is not ReadDependencyFamily.COVERAGE and not self.asset_ids:
            raise ValueError("read dependency family requires at least one asset")
        return self


class ReadDependencySignature(ContractModel):
    """Exact inputs that make a cached read result reusable or stale."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["read-dependency-signature-v1"] = READ_DEPENDENCY_SIGNATURE_SCHEMA
    workspace_id: UUID
    code_version: NonEmptyStr
    contract_version: NonEmptyStr
    catalog_version: Sha256Hex
    endpoint: NonEmptyStr
    arguments_digest: Sha256Hex
    known_at: UTCDateTime
    asset_ids: tuple[NonEmptyStr, ...]
    semantic_selection: NonEmptyStr
    dependencies: tuple[ReadDependency, ...]

    @model_validator(mode="after")
    def _validate_dependencies(self) -> ReadDependencySignature:
        keys = tuple((item.kind.value, item.dependency_id) for item in self.dependencies)
        if keys != tuple(sorted(keys)):
            raise ValueError("read dependencies must be sorted")
        if len(keys) != len(set(keys)):
            raise ValueError("read dependencies must be unique")
        if self.asset_ids != tuple(sorted(set(self.asset_ids))):
            raise ValueError("signature asset ids must be sorted and unique")
        return self

    @property
    def digest(self) -> str:
        """Return the stable identity of this exact signature."""
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def matches(self, other: ReadDependencySignature) -> bool:
        """Return whether both signatures describe the same visible snapshot."""
        return self.digest == other.digest


def _canonical_row(row: tuple[object, ...]) -> str:
    values = [str(value) if value is not None else None for value in row]
    return json.dumps(values, allow_nan=False, separators=(",", ":"), ensure_ascii=False)


def _row_digest(row: tuple[object, ...]) -> str:
    return hashlib.sha256(_canonical_row(row).encode("utf-8")).hexdigest()


def _instant_text(value: datetime) -> str:
    return value.isoformat()


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)


def _catalog_version(connection: DuckDBPyConnection) -> str:
    digest = hashlib.sha256()
    for table in ("assets", "source_definitions", "metric_definitions"):
        rows = connection.execute(
            f"SELECT document_json FROM {table} ORDER BY document_json"  # noqa: S608
        ).fetchall()
        digest.update(table.encode("utf-8"))
        for row in rows:
            digest.update(str(row[0]).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _observation_dependencies(
    connection: DuckDBPyConnection,
    query: ReadDependencyQuery,
) -> tuple[ReadDependency, ...]:
    if not observation_v2_table_exists(connection):
        raise ReadSignatureUnavailable("workspace v2 observations are unavailable")
    clauses = ["asset_id IN (" + _placeholders(query.asset_ids) + ")"]
    parameters: list[object] = [*query.asset_ids]
    if query.source_ids:
        clauses.append("source_id IN (" + _placeholders(query.source_ids) + ")")
        parameters.extend(query.source_ids)
    if query.frequency is not None:
        clauses.append("frequency = ?")
        parameters.append(query.frequency)
    if query.field_names:
        clauses.append("field_name IN (" + _placeholders(query.field_names) + ")")
        parameters.extend(query.field_names)
    if query.start is not None and query.end is not None:
        clauses.append("observed_at >= ? AND observed_at < ?")
        parameters.extend((_instant_text(query.start), _instant_text(query.end)))
    clauses.append("available_at <= ?")
    parameters.append(_instant_text(query.known_at))
    timestamp_columns = {
        "observed_at",
        "period_start",
        "period_end",
        "available_at",
        "normalized_at",
        "source_retrieved_at",
    }
    columns = ", ".join(
        f"CAST({name} AS VARCHAR)" if name in timestamp_columns else name
        for name in OBSERVATION_V2_COLUMNS
    )
    rows = connection.execute(
        f"SELECT {columns} FROM normalized_observations_v2 "  # noqa: S608 - closed columns
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY asset_id, observation_id",
        parameters,
    ).fetchall()
    if not rows:
        return (_absence("observations", query),)
    dependencies: list[ReadDependency] = []
    for row in rows:
        observation_id = str(row[0])
        dependencies.append(
            ReadDependency(
                kind=ReadDependencyKind.OBSERVATION,
                dependency_id=observation_id,
                content_sha256=_row_digest(row),
                revision=str(row[10]) if row[10] is not None else None,
            )
        )
    return tuple(dependencies)


def _raw_dependencies(
    connection: DuckDBPyConnection,
    *,
    asset_ids: tuple[str, ...],
    source_ids: tuple[str, ...],
    known_at: datetime,
    selector: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[ReadDependency, ...]:
    clauses = ["asset_id IN (" + _placeholders(asset_ids) + ")"]
    parameters: list[object] = [*asset_ids]
    if source_ids:
        clauses.append("source_id IN (" + _placeholders(source_ids) + ")")
        parameters.extend(source_ids)
    if start is not None and end is not None:
        clauses.append("event_time >= ? AND event_time < ?")
        parameters.extend((_instant_text(start), _instant_text(end)))
    clauses.append("available_at <= ?")
    parameters.append(_instant_text(known_at))
    rows = connection.execute(
        "SELECT record_id, checksum_sha256, schema_version, "
        "CAST(event_time AS VARCHAR), CAST(available_at AS VARCHAR) "
        "FROM raw_v2_index "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY received_at, record_id",
        parameters,
    ).fetchall()
    if not rows:
        return (
            ReadDependency(
                kind=ReadDependencyKind.ABSENCE,
                dependency_id=f"raw:{selector}",
                content_sha256=hashlib.sha256(selector.encode("utf-8")).hexdigest(),
            ),
        )
    return tuple(
        ReadDependency(
            kind=ReadDependencyKind.RAW_RECORD,
            dependency_id=str(row[0]),
            content_sha256=str(row[1]),
            revision=str(row[2]),
        )
        for row in rows
    )


def _metric_dependencies(
    connection: DuckDBPyConnection,
    query: ReadDependencyQuery,
) -> tuple[ReadDependency, ...]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('workspace_metric_results_v2', 'workspace_analytical_sequences_v2', "
            "'workspace_analytical_content_v2')"
        ).fetchall()
    }
    if "workspace_metric_results_v2" not in tables:
        raise ReadSignatureUnavailable("workspace v2 analytical results are unavailable")
    clauses = ["m.asset_id IN (" + _placeholders(query.asset_ids) + ")"]
    parameters: list[object] = [*query.asset_ids]
    if query.metric_keys:
        clauses.append("m.metric_key IN (" + _placeholders(query.metric_keys) + ")")
        parameters.extend(query.metric_keys)
    clauses.append("m.available_at <= ?")
    parameters.append(_instant_text(query.known_at))
    if query.start is not None and query.end is not None:
        clauses.append("m.as_of >= ? AND m.as_of < ?")
        parameters.extend((_instant_text(query.start), _instant_text(query.end)))
    sequence_join = ""
    content_join = ""
    sequence_columns = "NULL AS observation_sequence_checksum, NULL AS metric_sequence_checksum"
    content_column = "NULL AS parameters_sha256"
    if "workspace_analytical_sequences_v2" in tables:
        sequence_join = (
            "LEFT JOIN workspace_analytical_sequences_v2 s "
            "ON s.sequence_id = m.observation_sequence_id "
            "LEFT JOIN workspace_analytical_sequences_v2 ms "
            "ON ms.sequence_id = m.metric_sequence_id "
        )
        sequence_columns = (
            "s.checksum_sha256 AS observation_sequence_checksum, "
            "ms.checksum_sha256 AS metric_sequence_checksum"
        )
    if "workspace_analytical_content_v2" in tables:
        content_join = (
            "LEFT JOIN workspace_analytical_content_v2 c ON c.content_id = m.parameters_content_id "
        )
        content_column = "c.sha256 AS parameters_sha256"
    cursor = connection.execute(
        "SELECT m.result_id, m.checksum_sha256, m.origin, m.metric_key, "
        "CAST(m.as_of AS VARCHAR), CAST(m.available_at AS VARCHAR), "
        "m.observation_sequence_id, m.metric_sequence_id, "
        f"m.parameters_content_id, {sequence_columns}, {content_column} "
        "FROM workspace_metric_results_v2 AS m "
        f"{sequence_join}{content_join}"
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY m.asset_id, m.metric_key, m.as_of, m.result_id",
        parameters,
    )
    rows = cursor.fetchall()
    if not rows:
        return (_absence("metrics", query),)
    names = tuple(item[0] for item in cursor.description)
    dependencies: list[ReadDependency] = []
    for raw_row in rows:
        row = dict(zip(names, raw_row, strict=True))
        dependencies.append(
            ReadDependency(
                kind=ReadDependencyKind.METRIC_RESULT,
                dependency_id=str(row["result_id"]),
                content_sha256=str(row["checksum_sha256"]),
                revision=str(row["origin"]),
            )
        )
        for identifier, checksum in (
            (row["observation_sequence_id"], row["observation_sequence_checksum"]),
            (row["metric_sequence_id"], row["metric_sequence_checksum"]),
        ):
            if identifier is not None:
                dependencies.append(
                    ReadDependency(
                        kind=ReadDependencyKind.SEQUENCE,
                        dependency_id=str(identifier),
                        content_sha256=(str(checksum) if checksum is not None else _ABSENCE_DIGEST),
                    )
                )
        if row["parameters_content_id"] is not None and row["parameters_sha256"] is not None:
            dependencies.append(
                ReadDependency(
                    kind=ReadDependencyKind.PARAMETERS,
                    dependency_id=str(row["parameters_content_id"]),
                    content_sha256=str(row["parameters_sha256"]),
                )
            )
    unique: dict[tuple[str, str], ReadDependency] = {}
    for dependency in dependencies:
        key = (dependency.kind.value, dependency.dependency_id)
        existing = unique.get(key)
        if existing is not None and existing.content_sha256 != dependency.content_sha256:
            raise ReadSignatureUnavailable("a dependency identity maps to conflicting content")
        unique[key] = dependency
    return tuple(unique[key] for key in sorted(unique))


def _absence(label: str, query: ReadDependencyQuery) -> ReadDependency:
    identity = (
        f"{label}:{query.asset_ids}:{query.source_ids}:{query.frequency}:"
        f"{query.field_names}:{query.metric_keys}:{query.start}:{query.end}:{query.selector}"
    )
    return ReadDependency(
        kind=ReadDependencyKind.ABSENCE,
        dependency_id=identity,
        content_sha256=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    )


def _semantic_selection(query: ReadDependencyQuery) -> str:
    return "|".join(
        (
            query.family.value,
            query.selector,
            ",".join(query.asset_ids) or "universe",
            ",".join(query.source_ids),
            ",".join(query.field_names),
            ",".join(query.metric_keys),
            query.frequency or "",
            query.start.isoformat() if query.start is not None else "",
            query.end.isoformat() if query.end is not None else "",
            query.known_at.isoformat(),
        )
    )


def _collect_dependencies(
    connection: DuckDBPyConnection,
    query: ReadDependencyQuery,
) -> tuple[ReadDependency, ...]:
    if query.family in {
        ReadDependencyFamily.MARKET_BARS,
        ReadDependencyFamily.FUNDAMENTALS,
        ReadDependencyFamily.VALUATION,
        ReadDependencyFamily.LISTED_COMPANY_REPORT,
    }:
        observation_query = query
        if query.family is ReadDependencyFamily.VALUATION:
            observation_query = query.model_copy(update={"field_names": ()})
        observations = _observation_dependencies(connection, observation_query)
        raw = _raw_dependencies(
            connection,
            asset_ids=query.asset_ids,
            source_ids=query.source_ids,
            known_at=query.known_at,
            selector=query.family.value,
        )
        return observations + raw
    if query.family is ReadDependencyFamily.DOCUMENTS:
        return _raw_dependencies(
            connection,
            asset_ids=query.asset_ids,
            source_ids=query.source_ids or (_SEC_DOCUMENT_SOURCE_ID,),
            known_at=query.known_at,
            selector="documents",
        )
    if query.family is ReadDependencyFamily.INSTITUTIONAL:
        observations = _observation_dependencies(connection, query)
        raw = _raw_dependencies(
            connection,
            asset_ids=query.asset_ids,
            source_ids=query.source_ids or _INSTITUTIONAL_SOURCE_IDS,
            known_at=query.known_at,
            selector="institutional",
        )
        return observations + raw
    if query.family is ReadDependencyFamily.METRICS:
        return _metric_dependencies(connection, query)
    if query.family is ReadDependencyFamily.COVERAGE:
        observations = _observation_dependencies(connection, query)
        raw = _raw_dependencies(
            connection,
            asset_ids=query.asset_ids or ("*",),
            source_ids=query.source_ids,
            known_at=query.known_at,
            selector="coverage",
            start=query.start,
            end=query.end,
        )
        return observations + raw
    raise ReadSignatureUnavailable("read dependency family is not supported")


def build_composite_read_dependency_signature(
    storage: LocalStorage,
    queries: tuple[ReadDependencyQuery, ...],
    *,
    code_version: str = READ_DEPENDENCY_CODE_VERSION,
    contract_version: str = READ_DEPENDENCY_CONTRACT_VERSION,
) -> ReadDependencySignature:
    """Build one signature from the union of several exact read selections."""
    if not queries:
        raise ReadSignatureUnavailable("a composite signature requires at least one query")
    if storage.paths.format_version != 2 or storage.paths.workspace_id is None:
        raise ReadSignatureUnavailable("a signature requires an identified workspace v2")
    endpoints = {query.endpoint for query in queries}
    known_ats = {query.known_at for query in queries}
    if len(endpoints) != 1:
        raise ReadSignatureUnavailable("composite queries require one endpoint")
    if len(known_ats) != 1:
        raise ReadSignatureUnavailable("composite queries require one known_at")
    connection = storage.store.connection
    merged: dict[tuple[str, str], ReadDependency] = {}
    for query in queries:
        for dependency in _collect_dependencies(connection, query):
            key = (dependency.kind.value, dependency.dependency_id)
            existing = merged.get(key)
            if existing is not None and existing != dependency:
                raise ReadSignatureUnavailable("composite read dependencies conflict")
            merged[key] = dependency
    catalog = ReadDependency(
        kind=ReadDependencyKind.CATALOG,
        dependency_id=str(storage.paths.workspace_id),
        content_sha256=_catalog_version(connection),
    )
    ordered = tuple(
        sorted(
            (*merged.values(), catalog),
            key=lambda item: (item.kind.value, item.dependency_id),
        )
    )
    arguments = [query.model_dump(mode="json") for query in queries]
    arguments_digest = hashlib.sha256(
        json.dumps(arguments, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()
    asset_ids = tuple(sorted({asset_id for query in queries for asset_id in query.asset_ids}))
    return ReadDependencySignature(
        workspace_id=storage.paths.workspace_id,
        code_version=code_version,
        contract_version=contract_version,
        catalog_version=catalog.content_sha256,
        endpoint=queries[0].endpoint,
        arguments_digest=arguments_digest,
        known_at=queries[0].known_at,
        asset_ids=asset_ids,
        semantic_selection=" | ".join(_semantic_selection(query) for query in queries),
        dependencies=ordered,
    )


def build_read_dependency_signature(
    storage: LocalStorage,
    query: ReadDependencyQuery,
    *,
    code_version: str = READ_DEPENDENCY_CODE_VERSION,
    contract_version: str = READ_DEPENDENCY_CONTRACT_VERSION,
) -> ReadDependencySignature:
    """Build one signature from index projections without hydrating models."""
    if storage.paths.format_version != 2 or storage.paths.workspace_id is None:
        raise ReadSignatureUnavailable("a signature requires an identified workspace v2")
    connection = storage.store.connection
    dependencies = _collect_dependencies(connection, query)
    catalog = ReadDependency(
        kind=ReadDependencyKind.CATALOG,
        dependency_id=str(storage.paths.workspace_id),
        content_sha256=_catalog_version(connection),
    )
    ordered = tuple(
        sorted(
            (*dependencies, catalog),
            key=lambda item: (item.kind.value, item.dependency_id),
        )
    )
    arguments = query.model_dump(mode="json")
    arguments_digest = hashlib.sha256(
        json.dumps(arguments, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()
    return ReadDependencySignature(
        workspace_id=storage.paths.workspace_id,
        code_version=code_version,
        contract_version=contract_version,
        catalog_version=catalog.content_sha256,
        endpoint=query.endpoint,
        arguments_digest=arguments_digest,
        known_at=query.known_at,
        asset_ids=query.asset_ids,
        semantic_selection=_semantic_selection(query),
        dependencies=ordered,
    )
