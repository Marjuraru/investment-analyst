"""Read-only point-in-time reconstruction from stored FRED/ALFRED vintages."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.macro.fred_alfred import validate_fred_series_id
from investment_analyst.providers.macro.fred_raw_records import (
    StoredFredVintage,
    create_fred_source,
    fred_source_id,
    stored_fred_vintage_from_raw_record,
)
from investment_analyst.storage import LocalStorage, StorageError


class FredPointInTimeError(RuntimeError):
    """Base error for local FRED/ALFRED point-in-time reconstruction."""


class AmbiguousFredRevisionError(FredPointInTimeError):
    """Raised when equally dated vintage snapshots disagree semantically."""


class FredPointInTimeQuery(ContractModel):
    """One inclusive observation-range query at an explicit information cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    series_id: NonEmptyStr
    known_at: UTCDateTime
    observation_start: date | None = None
    observation_end: date | None = None

    @model_validator(mode="after")
    def validate_query(self) -> "FredPointInTimeQuery":
        """Validate the canonical series and inclusive optional date range."""
        validate_fred_series_id(self.series_id)
        if (
            self.observation_start is not None
            and self.observation_end is not None
            and self.observation_start > self.observation_end
        ):
            raise ValueError("observation_start must not be later than observation_end")
        return self


class FredSelectedObservation(ContractModel):
    """Latest point-in-time-safe value for one observation date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    series_id: NonEmptyStr
    source_id: NonEmptyStr
    observation_date: date
    value: Decimal | None
    raw_value: NonEmptyStr
    vintage_date: date
    realtime_start: date
    realtime_end: date
    available_at: UTCDateTime
    raw_record_ids: tuple[UUID, ...]
    superseded_count: int = Field(ge=0)

    @field_validator("value", mode="before")
    @classmethod
    def reject_binary_floating_point(cls, value: object) -> object:
        """Preserve exact decimal values from the provider's string representation."""
        if isinstance(value, (bool, float)):
            raise ValueError("value must be Decimal-compatible without float or bool")
        return value

    @field_validator("superseded_count", mode="before")
    @classmethod
    def reject_boolean_count(cls, value: object) -> object:
        """Reject booleans accepted as integers by Python."""
        if isinstance(value, bool):
            raise ValueError("superseded_count must be an integer")
        return value

    @model_validator(mode="after")
    def validate_selected(self) -> "FredSelectedObservation":
        """Validate value, real-time membership, and deterministic evidence ordering."""
        validate_fred_series_id(self.series_id)
        if self.value is not None and not self.value.is_finite():
            raise ValueError("value must be finite")
        if (self.value is None) != (self.raw_value == "."):
            raise ValueError("missing value and raw FRED marker are inconsistent")
        if not self.realtime_start <= self.vintage_date <= self.realtime_end:
            raise ValueError("vintage_date must be inside the observation real-time range")
        if not self.raw_record_ids:
            raise ValueError("selected observations require raw record evidence")
        if self.raw_record_ids != tuple(sorted(set(self.raw_record_ids), key=str)):
            raise ValueError("raw_record_ids must be unique and deterministically ordered")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "series_id": self.series_id,
            "source_id": self.source_id,
            "observation_date": self.observation_date.isoformat(),
            "value": str(self.value) if self.value is not None else None,
            "raw_value": self.raw_value,
            "vintage_date": self.vintage_date.isoformat(),
            "realtime_start": self.realtime_start.isoformat(),
            "realtime_end": self.realtime_end.isoformat(),
            "available_at": self.available_at.isoformat(),
            "raw_record_ids": [str(record_id) for record_id in self.raw_record_ids],
            "superseded_count": self.superseded_count,
        }


class FredPointInTimeResult(ContractModel):
    """Auditable macro series reconstructed only from vintages known at the cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: FredPointInTimeQuery
    source_id: NonEmptyStr
    observations: tuple[FredSelectedObservation, ...]
    raw_records_examined: int = Field(ge=0)
    raw_records_eligible: int = Field(ge=0)
    observations_selected: int = Field(ge=0)
    revisions_superseded: int = Field(ge=0)
    earliest_observation_date: date | None = None
    latest_observation_date: date | None = None
    latest_vintage_date: date | None = None
    availability_precision: Literal["day-conservative"]
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_result(self) -> "FredPointInTimeResult":
        """Validate order, counts, query scope, and the information cut."""
        dates = tuple(item.observation_date for item in self.observations)
        if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
            raise ValueError("observations must be unique and ordered chronologically")
        if self.observations_selected != len(self.observations):
            raise ValueError("observations_selected must match returned observations")
        if self.raw_records_eligible > self.raw_records_examined:
            raise ValueError("eligible raw record count exceeds examined records")
        if self.revisions_superseded != sum(item.superseded_count for item in self.observations):
            raise ValueError("revisions_superseded must match selected observations")
        if self.earliest_observation_date != (dates[0] if dates else None):
            raise ValueError("earliest_observation_date is inconsistent")
        if self.latest_observation_date != (dates[-1] if dates else None):
            raise ValueError("latest_observation_date is inconsistent")
        latest_vintage = (
            max(item.vintage_date for item in self.observations) if self.observations else None
        )
        if self.latest_vintage_date != latest_vintage:
            raise ValueError("latest_vintage_date is inconsistent")
        if any(item.available_at > self.query.known_at for item in self.observations):
            raise ValueError("a selected macro value was unavailable at known_at")
        if any(item.series_id != self.query.series_id for item in self.observations):
            raise ValueError("a selected macro value belongs to another series")
        if any(item.source_id != self.source_id for item in self.observations):
            raise ValueError("a selected macro value belongs to another source")
        if self.source_id != fred_source_id(self.query.series_id):
            raise ValueError("result source_id is inconsistent with the query")
        if any(
            self.query.observation_start is not None
            and item.observation_date < self.query.observation_start
            for item in self.observations
        ) or any(
            self.query.observation_end is not None
            and item.observation_date > self.query.observation_end
            for item in self.observations
        ):
            raise ValueError("a selected macro value is outside the query range")
        if not self.traceability_verified:
            raise ValueError("point-in-time result must have verified traceability")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible representation."""
        return {
            "query": self.query.model_dump(mode="json"),
            "source_id": self.source_id,
            "observations": [item.to_json_dict() for item in self.observations],
            "raw_records_examined": self.raw_records_examined,
            "raw_records_eligible": self.raw_records_eligible,
            "observations_selected": self.observations_selected,
            "revisions_superseded": self.revisions_superseded,
            "earliest_observation_date": (
                self.earliest_observation_date.isoformat()
                if self.earliest_observation_date is not None
                else None
            ),
            "latest_observation_date": (
                self.latest_observation_date.isoformat()
                if self.latest_observation_date is not None
                else None
            ),
            "latest_vintage_date": (
                self.latest_vintage_date.isoformat()
                if self.latest_vintage_date is not None
                else None
            ),
            "availability_precision": self.availability_precision,
            "traceability_verified": self.traceability_verified,
        }


class FredPointInTimeService:
    """Reconstruct one macro series from immutable, eligible vintage snapshots."""

    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(self, request: FredPointInTimeQuery) -> FredPointInTimeResult:
        """Select the latest non-ambiguous vintage for every observation date."""
        self._storage.require_open()
        source_id = fred_source_id(request.series_id)
        source = self._storage.sources.get(source_id)
        if source != create_fred_source(request.series_id):
            raise FredPointInTimeError("stored FRED/ALFRED source identity is inconsistent")

        records = self._storage.raw_records.list(source_id=source_id)
        snapshots: list[StoredFredVintage] = []
        for record in records:
            try:
                snapshot = stored_fred_vintage_from_raw_record(record)
            except (StorageError, ValueError) as error:
                raise FredPointInTimeError(
                    f"stored FRED/ALFRED record {record.record_id} is invalid"
                ) from error
            if snapshot.metadata.series_id != request.series_id:
                raise FredPointInTimeError("stored FRED/ALFRED series identity is inconsistent")
            if record.available_at <= request.known_at:
                snapshots.append(snapshot)

        candidates = self._observation_candidates(snapshots, request)
        selected, superseded = self._select_revisions(candidates, request)
        result = FredPointInTimeResult(
            query=request,
            source_id=source_id,
            observations=tuple(selected),
            raw_records_examined=len(records),
            raw_records_eligible=len(snapshots),
            observations_selected=len(selected),
            revisions_superseded=superseded,
            earliest_observation_date=selected[0].observation_date if selected else None,
            latest_observation_date=selected[-1].observation_date if selected else None,
            latest_vintage_date=(max(item.vintage_date for item in selected) if selected else None),
            availability_precision="day-conservative",
            traceability_verified=True,
        )
        self._verify_traceability(result)
        return result

    def _observation_candidates(
        self,
        snapshots: list[StoredFredVintage],
        request: FredPointInTimeQuery,
    ) -> dict[date, list[tuple[StoredFredVintage, int]]]:
        candidates: dict[date, list[tuple[StoredFredVintage, int]]] = defaultdict(list)
        for snapshot in snapshots:
            for index, observation in enumerate(snapshot.response.observations):
                if (
                    request.observation_start is not None
                    and observation.observation_date < request.observation_start
                ):
                    continue
                if (
                    request.observation_end is not None
                    and observation.observation_date > request.observation_end
                ):
                    continue
                candidates[observation.observation_date].append((snapshot, index))
        return candidates

    def _select_revisions(
        self,
        candidates: dict[date, list[tuple[StoredFredVintage, int]]],
        request: FredPointInTimeQuery,
    ) -> tuple[list[FredSelectedObservation], int]:
        selected: list[FredSelectedObservation] = []
        superseded_total = 0
        for observation_date in sorted(candidates):
            revisions = candidates[observation_date]
            latest_vintage = max(item[0].metadata.vintage_date for item in revisions)
            latest = [item for item in revisions if item[0].metadata.vintage_date == latest_vintage]
            semantics = {_semantic_key(snapshot, index) for snapshot, index in latest}
            if len(semantics) != 1:
                raise AmbiguousFredRevisionError(
                    f"conflicting {request.series_id} values share vintage "
                    f"{latest_vintage.isoformat()} for {observation_date.isoformat()}"
                )
            evidence = tuple(sorted({snapshot.record.record_id for snapshot, _ in latest}, key=str))
            representative, index = min(latest, key=lambda item: str(item[0].record.record_id))
            observation = representative.response.observations[index]
            superseded_count = len(revisions) - len(latest)
            superseded_total += superseded_count
            selected.append(
                FredSelectedObservation(
                    series_id=request.series_id,
                    source_id=representative.record.source.source_id,
                    observation_date=observation.observation_date,
                    value=observation.value,
                    raw_value=observation.raw_value,
                    vintage_date=latest_vintage,
                    realtime_start=observation.realtime_start,
                    realtime_end=observation.realtime_end,
                    available_at=min(snapshot.record.available_at for snapshot, _ in latest),
                    raw_record_ids=evidence,
                    superseded_count=superseded_count,
                )
            )
        return selected, superseded_total

    def _verify_traceability(self, result: FredPointInTimeResult) -> None:
        for observation in result.observations:
            records = self._storage.raw_records.get_many(observation.raw_record_ids)
            if set(records) != set(observation.raw_record_ids):
                raise FredPointInTimeError("selected macro evidence is incomplete")
            for record in records.values():
                snapshot = stored_fred_vintage_from_raw_record(record)
                if snapshot.metadata.series_id != result.query.series_id:
                    raise FredPointInTimeError("selected macro evidence has the wrong series")
                if record.available_at > result.query.known_at:
                    raise FredPointInTimeError(
                        "selected macro evidence was unavailable at known_at"
                    )


def _semantic_key(snapshot: StoredFredVintage, index: int) -> tuple[date, date, str]:
    observation = snapshot.response.observations[index]
    return observation.realtime_start, observation.realtime_end, observation.raw_value
