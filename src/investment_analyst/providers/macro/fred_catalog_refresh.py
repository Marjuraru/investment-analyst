"""Bounded resumable refresh of one versioned FRED catalog series."""

from datetime import date, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.macro.fred_alfred import FredAlfredClient
from investment_analyst.providers.macro.fred_catalog import (
    FredSeriesCatalogEntry,
    fred_catalog_entry,
)
from investment_analyst.providers.macro.fred_pipeline import FredVintagePipeline
from investment_analyst.providers.macro.fred_raw_records import (
    fred_source_id,
    stored_fred_vintage_from_raw_record,
)
from investment_analyst.storage import LocalStorage


class FredCatalogRefreshRequest(ContractModel):
    """Select one configured series and a completed local provider date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    series_id: NonEmptyStr
    run_date: date

    @model_validator(mode="after")
    def validate_catalog_scope(self) -> "FredCatalogRefreshRequest":
        """Require an automation-approved catalog entry."""
        entry = fred_catalog_entry(self.series_id)
        if not entry.automation_enabled:
            raise ValueError(
                f"automatic FRED refresh is disabled for high-volume series {self.series_id}"
            )
        if self.run_date < entry.observation_start:
            raise ValueError("run_date predates the configured observation history")
        return self


class FredCatalogRefreshSummary(ContractModel):
    """Compact discovery, resume, volume, and persistence evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["fred-catalog-refresh-summary-v1"] = "fred-catalog-refresh-summary-v1"
    catalog_version: Literal[1] = 1
    series_id: NonEmptyStr
    source_id: NonEmptyStr
    run_date: date
    discovery_start: date
    discovery_end: date
    bootstrap_latest_only: bool
    latest_stored_vintage_before: date | None = None
    selected_vintage_dates: tuple[date, ...]
    latest_stored_vintage_after: date | None = None
    provider_vintages_in_range: int = Field(ge=0)
    remaining_vintages_in_range: int = Field(ge=0)
    observations_received: int = Field(ge=0)
    raw_records_created: int = Field(ge=0)
    raw_records_reused: int = Field(ge=0)
    checked_at: UTCDateTime
    update_coverage_complete: bool
    historical_backfill_pending: bool
    traceability_verified: Literal[True] = True

    @model_validator(mode="after")
    def validate_summary(self) -> "FredCatalogRefreshSummary":
        """Keep range, ordering, progress, and source identity coherent."""
        if self.source_id != fred_source_id(self.series_id):
            raise ValueError("FRED catalog refresh source identity is inconsistent")
        if self.discovery_start > self.discovery_end:
            raise ValueError("FRED vintage discovery range is invalid")
        if self.selected_vintage_dates != tuple(sorted(set(self.selected_vintage_dates))):
            raise ValueError("selected FRED vintages must be unique and ordered")
        if any(
            item < self.discovery_start or item > self.discovery_end
            for item in self.selected_vintage_dates
        ):
            raise ValueError("selected FRED vintage is outside the discovery range")
        if (
            self.provider_vintages_in_range
            != len(self.selected_vintage_dates) + self.remaining_vintages_in_range
        ):
            raise ValueError("FRED vintage progress counts are inconsistent")
        if self.latest_stored_vintage_after is not None:
            candidates = (
                *(
                    (self.latest_stored_vintage_before,)
                    if self.latest_stored_vintage_before is not None
                    else ()
                ),
                *self.selected_vintage_dates,
            )
            if not candidates or self.latest_stored_vintage_after != max(candidates):
                raise ValueError("latest stored FRED vintage is inconsistent")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic scheduler- and CLI-safe primitives."""
        return self.model_dump(mode="json")


class FredCatalogRefreshService:
    """Discover only missing vintage edges and persist a bounded sequential batch."""

    def __init__(self, storage: LocalStorage, client: FredAlfredClient) -> None:
        self._storage = storage
        self._client = client

    def run(self, request: FredCatalogRefreshRequest) -> FredCatalogRefreshSummary:
        """Refresh one series while retaining every earlier successful snapshot."""
        self._storage.require_open()
        entry = fred_catalog_entry(request.series_id)
        stored_dates = self._stored_vintage_dates(entry)
        latest_before = stored_dates[-1] if stored_dates else None
        bootstrap = latest_before is None
        if latest_before is not None and latest_before > request.run_date:
            raise ValueError("stored FRED vintage is later than the requested run_date")
        if bootstrap:
            discovery_start = entry.observation_start
            discovery = self._client.fetch_vintage_dates(
                entry.series_id,
                realtime_start=discovery_start,
                realtime_end=request.run_date,
                max_dates=1,
                sort_order="desc",
            )
            checked_at = discovery.retrieved_at
        else:
            missing_start = latest_before + timedelta(days=1)
            discovery_start = min(missing_start, request.run_date)
            latest_probe = self._client.fetch_vintage_dates(
                entry.series_id,
                realtime_start=entry.observation_start,
                realtime_end=request.run_date,
                max_dates=1,
                sort_order="desc",
            )
            if not latest_probe.vintage_dates:
                raise ValueError("configured FRED series returned no official vintage dates")
            provider_latest = latest_probe.vintage_dates[0]
            if provider_latest <= latest_before:
                return FredCatalogRefreshSummary(
                    series_id=entry.series_id,
                    source_id=fred_source_id(entry.series_id),
                    run_date=request.run_date,
                    discovery_start=discovery_start,
                    discovery_end=request.run_date,
                    bootstrap_latest_only=False,
                    latest_stored_vintage_before=latest_before,
                    selected_vintage_dates=(),
                    latest_stored_vintage_after=latest_before,
                    provider_vintages_in_range=0,
                    remaining_vintages_in_range=0,
                    observations_received=0,
                    raw_records_created=0,
                    raw_records_reused=0,
                    checked_at=latest_probe.retrieved_at,
                    update_coverage_complete=True,
                    historical_backfill_pending=False,
                )
            discovery = self._client.fetch_vintage_dates(
                entry.series_id,
                realtime_start=missing_start,
                realtime_end=request.run_date,
                max_dates=entry.max_vintages_per_run,
                sort_order="asc",
            )
            checked_at = max(latest_probe.retrieved_at, discovery.retrieved_at)
        selected = tuple(sorted(discovery.vintage_dates))
        observations = 0
        created = 0
        reused = 0
        pipeline = FredVintagePipeline(self._storage, self._client)
        for vintage_date in selected:
            imported = pipeline.run(
                entry.series_id,
                vintage_date=vintage_date,
                observation_start=entry.observation_start,
                observation_end=vintage_date,
            )
            observations += imported.observations_received
            created += imported.raw_records_created
            reused += imported.raw_records_reused
            checked_at = max(checked_at, imported.retrieved_at)
        remaining = discovery.total_count - len(selected)
        latest_after_candidates = (
            *((latest_before,) if latest_before is not None else ()),
            *selected,
        )
        return FredCatalogRefreshSummary(
            series_id=entry.series_id,
            source_id=fred_source_id(entry.series_id),
            run_date=request.run_date,
            discovery_start=discovery_start,
            discovery_end=request.run_date,
            bootstrap_latest_only=bootstrap,
            latest_stored_vintage_before=latest_before,
            selected_vintage_dates=selected,
            latest_stored_vintage_after=(
                max(latest_after_candidates) if latest_after_candidates else None
            ),
            provider_vintages_in_range=discovery.total_count,
            remaining_vintages_in_range=remaining,
            observations_received=observations,
            raw_records_created=created,
            raw_records_reused=reused,
            checked_at=checked_at,
            update_coverage_complete=bootstrap or discovery.complete,
            historical_backfill_pending=bootstrap and remaining > 0,
        )

    def _stored_vintage_dates(
        self,
        entry: FredSeriesCatalogEntry,
    ) -> tuple[date, ...]:
        records = self._storage.raw_records.list(source_id=fred_source_id(entry.series_id))
        dates: set[date] = set()
        for record in records:
            stored = stored_fred_vintage_from_raw_record(record)
            if stored.metadata.series_id != entry.series_id:
                raise ValueError("stored FRED catalog series identity is inconsistent")
            dates.add(stored.metadata.vintage_date)
        return tuple(sorted(dates))


__all__ = [
    "FredCatalogRefreshRequest",
    "FredCatalogRefreshService",
    "FredCatalogRefreshSummary",
]
