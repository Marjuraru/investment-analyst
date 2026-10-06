"""Typed repository contracts for core data models."""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from pydantic import model_validator

from investment_analyst.core.models import (
    Asset,
    ContractModel,
    DataFrequency,
    DataQuality,
    DiagnosticMode,
    DiagnosticResult,
    MetricDefinition,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
)


class BatchWriteReceipt(ContractModel):
    """Typed in-memory receipt for batch persistence operations."""

    created_ids: tuple[UUID, ...] = ()
    reused_ids: tuple[UUID, ...] = ()
    conflicting_ids: tuple[UUID, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_fields(cls, data: object) -> object:
        if isinstance(data, dict):
            payload = dict(data)
            for prefix in ("created", "reused", "conflicting"):
                if prefix in payload and f"{prefix}_ids" not in payload:
                    payload[f"{prefix}_ids"] = payload.pop(prefix)
            return payload
        return data

    @property
    def created(self) -> tuple[UUID, ...]:
        return self.created_ids

    @property
    def reused(self) -> tuple[UUID, ...]:
        return self.reused_ids

    @property
    def conflicting(self) -> tuple[UUID, ...]:
        return self.conflicting_ids

    @property
    def created_count(self) -> int:
        return len(self.created_ids)

    @property
    def reused_count(self) -> int:
        return len(self.reused_ids)

    @property
    def conflicting_count(self) -> int:
        return len(self.conflicting_ids)

    @property
    def total_count(self) -> int:
        return len(self.created_ids) + len(self.reused_ids) + len(self.conflicting_ids)


class AssetRepository(Protocol):
    """Persistence operations for mutable asset definitions."""

    def upsert(self, asset: Asset) -> Asset: ...

    def get(self, asset_id: str) -> Asset: ...

    def list_all(self) -> list[Asset]: ...


class SourceDefinitionRepository(Protocol):
    """Persistence operations for mutable source definitions."""

    def upsert(self, source: SourceDefinition) -> SourceDefinition: ...

    def get(self, source_id: str) -> SourceDefinition: ...

    def list_all(self) -> list[SourceDefinition]: ...


class RawRecordRepository(Protocol):
    """Append-only persistence operations for original records."""

    def save(self, record: RawRecord) -> RawRecord: ...

    def save_many(self, records: Collection[RawRecord]) -> BatchWriteReceipt: ...

    def save_batch(self, records: Collection[RawRecord]) -> BatchWriteReceipt: ...

    def get(self, record_id: UUID) -> RawRecord: ...

    def get_many(self, record_ids: Collection[UUID]) -> dict[UUID, RawRecord]: ...

    def list(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> list[RawRecord]: ...

    def list_record_ids(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> list[UUID]: ...

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
    ) -> list[UUID]: ...

    def count(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> int: ...

    def available_at_bounds(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
    ) -> tuple[datetime | None, datetime | None]: ...

    def select_record_ids_by_json_field(
        self,
        *,
        field: str,
        values: Collection[str],
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
    ) -> list[UUID]: ...

    def verify_index_integrity(self, record_ids: Collection[UUID]) -> int: ...


class ObservationRepository(Protocol):
    """Append-only persistence operations for normalized observations."""

    def save(self, observation: NormalizedObservation) -> NormalizedObservation: ...

    def save_many(self, observations: Collection[NormalizedObservation]) -> BatchWriteReceipt: ...

    def save_batch(self, observations: Collection[NormalizedObservation]) -> BatchWriteReceipt: ...

    def get(self, observation_id: UUID) -> NormalizedObservation: ...

    def get_many(self, observation_ids: Collection[UUID]) -> dict[UUID, NormalizedObservation]: ...

    def get_existing(
        self, observation_ids: Collection[UUID]
    ) -> dict[UUID, NormalizedObservation]: ...

    def list(
        self,
        *,
        asset_id: str | None = None,
        frequency: DataFrequency | None = None,
        observed_from: datetime | None = None,
        observed_before: datetime | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
        source_id: str | None = None,
        field_name: str | None = None,
        field_names: Collection[str] | None = None,
        quality: DataQuality | None = None,
        period_end_from: datetime | date | None = None,
        period_end_to: datetime | date | None = None,
    ) -> list[NormalizedObservation]: ...

    def count(
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
    ) -> int: ...

    def minimum_available_at(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        frequency: DataFrequency | None = None,
        quality: DataQuality | None = None,
        transformation_version: str | None = None,
    ) -> datetime | None: ...

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
    ) -> datetime | None: ...

    def observed_at_bounds(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        frequency: DataFrequency | None = None,
        field_name: str | None = None,
        field_names: Collection[str] | None = None,
        quality: DataQuality | None = None,
        observed_from: datetime | None = None,
        observed_before: datetime | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
        period_end_from: datetime | date | None = None,
        period_end_to: datetime | date | None = None,
    ) -> tuple[datetime | None, datetime | None]: ...

    def list_observation_import_page(
        self,
        *,
        limit: int,
        after_available_at: datetime | None = None,
        after_observation_id: UUID | None = None,
    ) -> list[UUID]: ...

    def list_ids_for_manager_observation_references(
        self, *, asset_id: str, available_to: datetime | None = None
    ) -> list[UUID]: ...

    def select_ids_for_manager_observation_references(
        self, candidate_ids: Collection[UUID], *, manager: str
    ) -> list[UUID]: ...

    def field_name(self, observation_id: UUID) -> str | None: ...


class MetricDefinitionRepository(Protocol):
    """Persistence operations for mutable metric definitions."""

    def upsert(self, definition: MetricDefinition) -> MetricDefinition: ...

    def get(self, metric_key: str) -> MetricDefinition: ...

    def list_all(self) -> list[MetricDefinition]: ...


class MetricResultRepository(Protocol):
    """Append-only persistence operations for calculated metric results."""

    def save(self, result: MetricResult) -> MetricResult: ...

    def save_many(self, results: Collection[MetricResult]) -> BatchWriteReceipt: ...

    def save_batch(self, results: Collection[MetricResult]) -> BatchWriteReceipt: ...

    def get(self, result_id: UUID) -> MetricResult: ...

    def get_many(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]: ...

    def get_existing(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]: ...

    def list(
        self,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        metric_keys: tuple[str, ...] | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[MetricResult]: ...

    def count(
        self,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        metric_keys: tuple[str, ...] | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> int: ...

    def list_import_page(
        self,
        *,
        limit: int,
        after_available_at: datetime | None = None,
        after_result_id: UUID | None = None,
    ) -> tuple[UUID, ...]: ...

    def list_ids(
        self,
        *,
        asset_id: str | None = None,
        metric_keys: Collection[str] | None = None,
        available_to: datetime | None = None,
    ) -> list[UUID]: ...


class DiagnosticResultRepository(Protocol):
    """Append-only persistence operations for diagnostic results."""

    def save(self, result: DiagnosticResult) -> DiagnosticResult: ...

    def save_many(self, results: Collection[DiagnosticResult]) -> BatchWriteReceipt: ...

    def save_batch(self, results: Collection[DiagnosticResult]) -> BatchWriteReceipt: ...

    def get(self, diagnostic_id: UUID) -> DiagnosticResult: ...

    def get_many(self, diagnostic_ids: Collection[UUID]) -> dict[UUID, DiagnosticResult]: ...

    def list(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[DiagnosticResult]: ...

    def count(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> int: ...

    def list_import_page(
        self,
        *,
        limit: int,
        after_available_at: datetime | None = None,
        after_diagnostic_id: UUID | None = None,
    ) -> tuple[UUID, ...]: ...
