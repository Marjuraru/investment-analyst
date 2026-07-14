"""Persist selected Apple SEC fundamental observations without analytics."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from uuid import UUID

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
)
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
    SecFactExtractionResult,
    sec_fact_to_observation,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SCHEMA_VERSION,
    COMPANYFACTS_SOURCE_ID,
    SUBMISSIONS_SCHEMA_VERSION,
    SUBMISSIONS_SOURCE_ID,
    TRANSFORMATION_VERSION,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError


class SecObservationPipelineError(RuntimeError):
    """Base error for the local SEC observation pipeline."""


class SecSnapshotSelectionError(SecObservationPipelineError):
    """Raised when required snapshots are absent or selection is ambiguous."""


class SecObservationIdentityConflictError(SecObservationPipelineError):
    """Raised when a stable observation ID resolves to different fact content."""


class SecObservationTraceabilityError(SecObservationPipelineError):
    """Raised when a normalized SEC observation cannot be reconstructed."""


@dataclass(frozen=True, slots=True)
class SecObservationImportSummary:
    """Compact result of one local Apple SEC fact normalization run."""

    asset_id: str
    submissions_record_id: UUID
    companyfacts_record_id: UUID
    normalized_at: datetime
    filings_indexed: int
    facts_examined: int
    facts_selected: int
    observations_generated: int
    observations_created: int
    observations_reused: int
    field_counts: Mapping[str, int]
    annual_count: int
    quarterly_count: int
    skipped_counts: Mapping[str, int]
    earliest_period_end: date | None
    latest_period_end: date | None
    raw_records_created: int
    metric_results_created: int
    diagnostics_created: int
    traceability_verified: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "field_counts",
            MappingProxyType(dict(sorted(self.field_counts.items()))),
        )
        object.__setattr__(
            self,
            "skipped_counts",
            MappingProxyType(dict(sorted(self.skipped_counts.items()))),
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "asset_id": self.asset_id,
            "submissions_record_id": str(self.submissions_record_id),
            "companyfacts_record_id": str(self.companyfacts_record_id),
            "normalized_at": self.normalized_at.isoformat(),
            "filings_indexed": self.filings_indexed,
            "facts_examined": self.facts_examined,
            "facts_selected": self.facts_selected,
            "observations_generated": self.observations_generated,
            "observations_created": self.observations_created,
            "observations_reused": self.observations_reused,
            "field_counts": dict(self.field_counts),
            "annual_count": self.annual_count,
            "quarterly_count": self.quarterly_count,
            "skipped_counts": dict(self.skipped_counts),
            "earliest_period_end": (
                self.earliest_period_end.isoformat() if self.earliest_period_end else None
            ),
            "latest_period_end": (
                self.latest_period_end.isoformat() if self.latest_period_end else None
            ),
            "raw_records_created": self.raw_records_created,
            "metric_results_created": self.metric_results_created,
            "diagnostics_created": self.diagnostics_created,
            "traceability_verified": self.traceability_verified,
        }


class SecAaplObservationPipeline:
    """Normalize the latest local Apple SEC snapshots into append-only observations."""

    def __init__(
        self,
        storage: LocalStorage,
        normalizer: SecCompanyFactsNormalizer,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._normalizer = normalizer
        self._clock = clock

    def run(self) -> SecObservationImportSummary:
        """Select snapshots, validate all candidates, then persist idempotently."""
        self._storage.require_open()
        normalized_at = _utc_datetime(self._clock(), "clock result")
        raw_ids_before = {record.record_id for record in self._storage.raw_records.list()}
        observations_before = {
            item.observation_id: item for item in self._storage.observations.list()
        }
        metric_ids_before = {item.result_id for item in self._storage.metric_results.list()}
        diagnostic_ids_before = {item.diagnostic_id for item in self._storage.diagnostics.list()}

        submissions_record = self._select_snapshot(
            source_id=SUBMISSIONS_SOURCE_ID,
            schema_version=SUBMISSIONS_SCHEMA_VERSION,
        )
        companyfacts_record = self._select_snapshot(
            source_id=COMPANYFACTS_SOURCE_ID,
            schema_version=COMPANYFACTS_SCHEMA_VERSION,
        )
        extraction = self._normalizer.extract(
            companyfacts_record,
            submissions_record,
            normalized_at=normalized_at,
        )
        candidates = self._build_candidates(
            extraction,
            companyfacts_record,
            submissions_record,
            normalized_at=normalized_at,
        )

        raw_record_cache: dict[UUID, RawRecord] = {
            submissions_record.record_id: submissions_record,
            companyfacts_record.record_id: companyfacts_record,
        }
        reconstruction_cache: dict[
            tuple[UUID, UUID, datetime],
            dict[UUID, NormalizedObservation],
        ] = {}

        existing_by_id: dict[UUID, NormalizedObservation] = {}
        missing: list[NormalizedObservation] = []
        for candidate in candidates:
            self._verify_observation(
                candidate,
                raw_record_cache=raw_record_cache,
                reconstruction_cache=reconstruction_cache,
            )
            try:
                existing = self._storage.observations.get(candidate.observation_id)
            except RecordNotFoundError:
                missing.append(candidate)
            else:
                if _semantic_identity(existing) != _semantic_identity(candidate):
                    raise SecObservationIdentityConflictError(
                        f"observation {candidate.observation_id} has different fact content"
                    )
                self._verify_observation(
                    existing,
                    raw_record_cache=raw_record_cache,
                    reconstruction_cache=reconstruction_cache,
                )
                existing_by_id[candidate.observation_id] = existing

        for candidate in missing:
            self._storage.observations.save(candidate)

        persisted = []
        for candidate in candidates:
            stored = self._storage.observations.get(candidate.observation_id)
            expected = existing_by_id.get(candidate.observation_id, candidate)
            if stored != expected:
                raise SecObservationTraceabilityError(
                    f"stored observation {candidate.observation_id} differs from validated content"
                )
            self._verify_observation(
                stored,
                raw_record_cache=raw_record_cache,
                reconstruction_cache=reconstruction_cache,
            )
            persisted.append(stored)

        self._verify_unchanged_existing_observations(observations_before)
        raw_ids_after = {record.record_id for record in self._storage.raw_records.list()}
        metric_ids_after = {item.result_id for item in self._storage.metric_results.list()}
        diagnostic_ids_after = {item.diagnostic_id for item in self._storage.diagnostics.list()}
        if raw_ids_after != raw_ids_before:
            raise SecObservationTraceabilityError("normalization created or removed RawRecords")
        if metric_ids_after != metric_ids_before:
            raise SecObservationTraceabilityError("normalization changed MetricResults")
        if diagnostic_ids_after != diagnostic_ids_before:
            raise SecObservationTraceabilityError("normalization changed DiagnosticResults")

        return SecObservationImportSummary(
            asset_id=ASSET_ID,
            submissions_record_id=submissions_record.record_id,
            companyfacts_record_id=companyfacts_record.record_id,
            normalized_at=normalized_at,
            filings_indexed=extraction.filings_indexed,
            facts_examined=extraction.facts_examined,
            facts_selected=extraction.facts_selected,
            observations_generated=len(candidates),
            observations_created=len(missing),
            observations_reused=len(candidates) - len(missing),
            field_counts=dict(extraction.field_counts),
            annual_count=extraction.annual_count,
            quarterly_count=extraction.quarterly_count,
            skipped_counts=dict(extraction.skipped_counts),
            earliest_period_end=extraction.earliest_period_end,
            latest_period_end=extraction.latest_period_end,
            raw_records_created=len(raw_ids_after - raw_ids_before),
            metric_results_created=len(metric_ids_after - metric_ids_before),
            diagnostics_created=len(diagnostic_ids_after - diagnostic_ids_before),
            traceability_verified=True,
        )

    def _select_snapshot(self, *, source_id: str, schema_version: str) -> RawRecord:
        candidates = [
            record
            for record in self._storage.raw_records.list(source_id=source_id)
            if record.asset_id == ASSET_ID and record.schema_version == schema_version
        ]
        if not candidates:
            raise SecSnapshotSelectionError(f"no compatible local snapshot for {source_id}")
        latest_received_at = max(record.received_at for record in candidates)
        latest = [record for record in candidates if record.received_at == latest_received_at]
        if len({record.record_id for record in latest}) != 1:
            raise SecSnapshotSelectionError(
                f"multiple different {source_id} snapshots share the latest received_at"
            )
        return latest[0]

    def _build_candidates(
        self,
        extraction: SecFactExtractionResult,
        companyfacts_record: RawRecord,
        submissions_record: RawRecord,
        *,
        normalized_at: datetime,
    ) -> tuple[NormalizedObservation, ...]:
        by_id: dict[UUID, NormalizedObservation] = {}
        for fact in extraction.facts:
            candidate = sec_fact_to_observation(
                fact,
                companyfacts_record,
                submissions_record,
                normalized_at=normalized_at,
            )
            _validate_candidate(candidate, normalized_at)
            existing = by_id.get(candidate.observation_id)
            if existing is None:
                by_id[candidate.observation_id] = candidate
            elif _semantic_identity(existing) != _semantic_identity(candidate):
                raise SecObservationIdentityConflictError(
                    f"candidate observation {candidate.observation_id} is contradictory"
                )
        return tuple(
            sorted(
                by_id.values(),
                key=lambda item: (
                    item.period_end or item.observed_at,
                    item.frequency.value,
                    item.field_name,
                    item.available_at,
                    str(item.observation_id),
                ),
            )
        )

    def _verify_observation(
        self,
        observation: NormalizedObservation,
        *,
        raw_record_cache: dict[UUID, RawRecord],
        reconstruction_cache: dict[
            tuple[UUID, UUID, datetime],
            dict[UUID, NormalizedObservation],
        ],
    ) -> None:
        _validate_candidate(observation, observation.normalized_at)
        key = _record_key(observation)

        try:
            companyfacts_id = UUID(_required_key(key, "companyfacts_record_id"))
            submissions_id = UUID(_required_key(key, "submissions_record_id"))
        except ValueError as error:
            raise SecObservationTraceabilityError(
                "SEC observation record_key contains an invalid RawRecord ID"
            ) from error

        if observation.raw_record_id != companyfacts_id:
            raise SecObservationTraceabilityError(
                "SEC observation raw_record_id does not match its record_key"
            )

        try:
            companyfacts_record = raw_record_cache.get(companyfacts_id)
            if companyfacts_record is None:
                companyfacts_record = self._storage.raw_records.get(companyfacts_id)
                raw_record_cache[companyfacts_id] = companyfacts_record

            submissions_record = raw_record_cache.get(submissions_id)
            if submissions_record is None:
                submissions_record = self._storage.raw_records.get(submissions_id)
                raw_record_cache[submissions_id] = submissions_record
        except (RecordNotFoundError, StorageError) as error:
            raise SecObservationTraceabilityError(
                "SEC observation references an unavailable raw snapshot"
            ) from error

        cache_key = (
            companyfacts_id,
            submissions_id,
            observation.normalized_at,
        )
        reconstructed = reconstruction_cache.get(cache_key)

        if reconstructed is None:
            extraction = self._normalizer.extract(
                companyfacts_record,
                submissions_record,
                normalized_at=observation.normalized_at,
            )
            reconstructed = {
                item.observation_id: item
                for item in (
                    sec_fact_to_observation(
                        fact,
                        companyfacts_record,
                        submissions_record,
                        normalized_at=observation.normalized_at,
                    )
                    for fact in extraction.facts
                )
            }
            reconstruction_cache[cache_key] = reconstructed

        expected = reconstructed.get(observation.observation_id)
        if expected is None or expected != observation:
            raise SecObservationTraceabilityError(
                f"observation {observation.observation_id} cannot be reconstructed"
            )

        accession = _required_key(key, "accession_number")
        if accession not in observation.source.record_key:
            raise SecObservationTraceabilityError("SEC observation accession trace is missing")

    def _verify_unchanged_existing_observations(
        self,
        before: dict[UUID, NormalizedObservation],
    ) -> None:
        after = {item.observation_id: item for item in self._storage.observations.list()}
        for identifier, original in before.items():
            if after.get(identifier) != original:
                raise SecObservationTraceabilityError(
                    f"pre-existing observation {identifier} was modified"
                )


def _validate_candidate(observation: NormalizedObservation, normalized_at: datetime) -> None:
    if observation.asset_id != ASSET_ID:
        raise SecObservationTraceabilityError("SEC observation must belong to Apple")
    if observation.source.source_id != COMPANYFACTS_SOURCE_ID:
        raise SecObservationTraceabilityError("SEC observation source is incorrect")
    if observation.unit != "USD":
        raise SecObservationTraceabilityError("SEC observation unit must be USD")
    if observation.frequency not in {DataFrequency.ANNUAL, DataFrequency.QUARTERLY}:
        raise SecObservationTraceabilityError("SEC observation frequency is invalid")
    if observation.quality is not DataQuality.VALID:
        raise SecObservationTraceabilityError("SEC observation quality must be VALID")
    if observation.transformation_version != TRANSFORMATION_VERSION:
        raise SecObservationTraceabilityError("SEC observation transformation version is invalid")
    if observation.available_at > normalized_at:
        raise SecObservationTraceabilityError("SEC observation uses a future filing")
    for timestamp in (
        observation.observed_at,
        observation.period_start,
        observation.period_end,
        observation.available_at,
        observation.normalized_at,
        observation.source.retrieved_at,
    ):
        if timestamp is not None and timestamp.utcoffset() != UTC.utcoffset(timestamp):
            raise SecObservationTraceabilityError("SEC observation timestamps must be UTC")


def _semantic_identity(observation: NormalizedObservation) -> tuple[object, ...]:
    key = _record_key(observation)
    contextual_key = tuple(
        (name, key.get(name)) for name in ("accession_number", "taxonomy", "tag", "unit", "period")
    )
    return (
        observation.observation_id,
        observation.asset_id,
        observation.field_name,
        observation.value,
        observation.unit,
        observation.frequency,
        observation.observed_at,
        observation.period_start,
        observation.period_end,
        observation.available_at,
        observation.quality,
        observation.transformation_version,
        observation.source.source_id,
        contextual_key,
    )


def _record_key(observation: NormalizedObservation) -> dict[str, object]:
    value = observation.source.record_key
    if value is None:
        raise SecObservationTraceabilityError("SEC observation record_key is missing")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise SecObservationTraceabilityError(
            "SEC observation record_key must be canonical JSON"
        ) from error
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise SecObservationTraceabilityError("SEC observation record_key must be an object")
    return decoded


def _required_key(mapping: dict[str, object], name: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, str) or not value:
        raise SecObservationTraceabilityError(f"SEC observation record_key lacks {name}")
    return value


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecObservationPipelineError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)
