"""Typed repository contracts for core data models."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from investment_analyst.core.models import (
    Asset,
    DiagnosticMode,
    DiagnosticResult,
    MetricDefinition,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
)


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

    def get(self, record_id: UUID) -> RawRecord: ...

    def list(
        self,
        *,
        source_id: str | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> list[RawRecord]: ...


class ObservationRepository(Protocol):
    """Append-only persistence operations for normalized observations."""

    def save(self, observation: NormalizedObservation) -> NormalizedObservation: ...

    def get(self, observation_id: UUID) -> NormalizedObservation: ...

    def list(
        self,
        *,
        asset_id: str | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
    ) -> list[NormalizedObservation]: ...


class MetricDefinitionRepository(Protocol):
    """Persistence operations for mutable metric definitions."""

    def upsert(self, definition: MetricDefinition) -> MetricDefinition: ...

    def get(self, metric_key: str) -> MetricDefinition: ...

    def list_all(self) -> list[MetricDefinition]: ...


class MetricResultRepository(Protocol):
    """Append-only persistence operations for calculated metric results."""

    def save(self, result: MetricResult) -> MetricResult: ...

    def get(self, result_id: UUID) -> MetricResult: ...

    def list(
        self,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[MetricResult]: ...


class DiagnosticResultRepository(Protocol):
    """Append-only persistence operations for diagnostic results."""

    def save(self, result: DiagnosticResult) -> DiagnosticResult: ...

    def get(self, diagnostic_id: UUID) -> DiagnosticResult: ...

    def list(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[DiagnosticResult]: ...
