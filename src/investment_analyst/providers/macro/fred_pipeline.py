"""Append-only persistence pipeline for one FRED/ALFRED vintage snapshot."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from investment_analyst.core.models import RawRecord
from investment_analyst.providers.macro.fred_alfred import FredVintageFetch
from investment_analyst.providers.macro.fred_raw_records import (
    StoredFredVintage,
    create_fred_source,
    fred_source_id,
    fred_vintage_to_raw_record,
    stored_fred_vintage_from_raw_record,
)
from investment_analyst.storage import LocalStorage, RecordNotFoundError, StorageError


class FredVintageClient(Protocol):
    """Provider operation required by the persistence pipeline."""

    def fetch_vintage_snapshot(
        self,
        series_id: str,
        *,
        vintage_date: date,
        observation_start: date,
        observation_end: date,
    ) -> FredVintageFetch: ...


@dataclass(frozen=True, slots=True)
class FredVintageImportSummary:
    """Compact auditable outcome of one explicit vintage import."""

    series_id: str
    source_id: str
    vintage_date: date
    observation_start: date
    observation_end: date
    retrieved_at: datetime
    observations_received: int
    values_received: int
    missing_values_received: int
    raw_records_created: int
    raw_records_reused: int
    availability_precision: str
    traceability_verified: bool

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "series_id": self.series_id,
            "source_id": self.source_id,
            "vintage_date": self.vintage_date.isoformat(),
            "observation_start": self.observation_start.isoformat(),
            "observation_end": self.observation_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "observations_received": self.observations_received,
            "values_received": self.values_received,
            "missing_values_received": self.missing_values_received,
            "raw_records_created": self.raw_records_created,
            "raw_records_reused": self.raw_records_reused,
            "availability_precision": self.availability_precision,
            "traceability_verified": self.traceability_verified,
        }


class FredVintagePipeline:
    """Persist one official macro snapshot without creating asset analytics."""

    def __init__(self, storage: LocalStorage, client: FredVintageClient) -> None:
        self._storage = storage
        self._client = client

    def run(
        self,
        series_id: str,
        *,
        vintage_date: date,
        observation_start: date,
        observation_end: date,
    ) -> FredVintageImportSummary:
        """Fetch, persist, round-trip, and isolate one vintage snapshot."""
        self._storage.require_open()
        assets_before = tuple(self._storage.assets.list_all())
        observations_before = self._storage.observations.count()
        metric_definitions_before = tuple(self._storage.metric_definitions.list_all())
        metric_results_before = self._storage.metric_results.count()
        diagnostics_before = self._storage.diagnostics.count()

        fetch = self._client.fetch_vintage_snapshot(
            series_id,
            vintage_date=vintage_date,
            observation_start=observation_start,
            observation_end=observation_end,
        )
        if (
            fetch.series_id != series_id
            or fetch.vintage_date != vintage_date
            or fetch.requested_observation_start != observation_start
            or fetch.requested_observation_end != observation_end
        ):
            raise StorageError("FRED/ALFRED fetch result does not match the requested scope")

        candidate = fred_vintage_to_raw_record(fetch)
        source = create_fred_source(series_id)
        self._storage.sources.upsert(source)
        created = 0
        reused = 0
        try:
            stored = self._storage.raw_records.get(candidate.record_id)
            reused = 1
        except RecordNotFoundError:
            self._storage.raw_records.save(candidate)
            stored = self._storage.raw_records.get(candidate.record_id)
            created = 1

        verified = self._verify_isolation(
            stored,
            source_id=source.source_id,
            assets_before=assets_before,
            observations_before=observations_before,
            metric_definitions_before=metric_definitions_before,
            metric_results_before=metric_results_before,
            diagnostics_before=diagnostics_before,
        )
        values = sum(item.value is not None for item in verified.response.observations)
        missing = len(verified.response.observations) - values
        return FredVintageImportSummary(
            series_id=series_id,
            source_id=fred_source_id(series_id),
            vintage_date=vintage_date,
            observation_start=observation_start,
            observation_end=observation_end,
            retrieved_at=stored.received_at,
            observations_received=len(verified.response.observations),
            values_received=values,
            missing_values_received=missing,
            raw_records_created=created,
            raw_records_reused=reused,
            availability_precision="day-conservative",
            traceability_verified=True,
        )

    def _verify_isolation(
        self,
        stored: RawRecord,
        *,
        source_id: str,
        assets_before: tuple[object, ...],
        observations_before: int,
        metric_definitions_before: tuple[object, ...],
        metric_results_before: int,
        diagnostics_before: int,
    ) -> StoredFredVintage:
        if self._storage.raw_records.get(stored.record_id) != stored:
            raise StorageError("FRED/ALFRED raw record round-trip verification failed")
        verified = stored_fred_vintage_from_raw_record(stored)
        if stored.source.source_id != source_id:
            raise StorageError("FRED/ALFRED stored source identity is inconsistent")
        if self._storage.sources.get(source_id) != create_fred_source(verified.metadata.series_id):
            raise StorageError("FRED/ALFRED source round-trip verification failed")
        if tuple(self._storage.assets.list_all()) != assets_before:
            raise StorageError("FRED/ALFRED import must not mutate the asset catalog")
        if self._storage.observations.count() != observations_before:
            raise StorageError("FRED/ALFRED import must not create asset observations")
        if tuple(self._storage.metric_definitions.list_all()) != metric_definitions_before:
            raise StorageError("FRED/ALFRED import must not create metric definitions")
        if self._storage.metric_results.count() != metric_results_before:
            raise StorageError("FRED/ALFRED import must not create metric results")
        if self._storage.diagnostics.count() != diagnostics_before:
            raise StorageError("FRED/ALFRED import must not create diagnostics")
        return verified
