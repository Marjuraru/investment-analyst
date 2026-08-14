"""Append-only persistence for provider-independent derivatives metrics."""

from collections.abc import Callable
from datetime import UTC, datetime

from investment_analyst.analytics.crypto.derivatives_engine import (
    METRIC_DEFINITIONS,
    CryptoDerivativesMetricEngine,
)
from investment_analyst.analytics.crypto.derivatives_models import (
    CryptoDerivativesMetricPersistenceSummary,
)
from investment_analyst.core.models import MetricResult
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError


class CryptoDerivativesMetricPipeline:
    """Compute and persist exact-cut metrics while reusing equivalent identities."""

    def __init__(
        self,
        storage: LocalStorage,
        engine: CryptoDerivativesMetricEngine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._engine = engine
        self._clock = clock

    def run(
        self,
        *,
        asset_id: str,
        funding_source_id: str,
        dvol_source_id: str,
        summary_source_id: str,
        known_at: datetime,
        as_of_from: datetime,
        as_of_before: datetime,
    ) -> CryptoDerivativesMetricPersistenceSummary:
        self._storage.require_open()
        observations = tuple(
            self._storage.observations.list(
                asset_id=asset_id,
                available_to=known_at,
            )
        )
        computation = self._engine.compute(
            observations,
            asset_id=asset_id,
            funding_source_id=funding_source_id,
            dvol_source_id=dvol_source_id,
            summary_source_id=summary_source_id,
            known_at=known_at,
            computed_at=self._clock(),
            as_of_from=as_of_from,
            as_of_before=as_of_before,
        )
        for definition in METRIC_DEFINITIONS:
            self._storage.metric_definitions.upsert(definition)
        created = 0
        reused = 0
        persisted: list[MetricResult] = []
        for candidate in computation.results:
            try:
                existing = self._storage.metric_results.get(candidate.result_id)
                _verify_equivalent(existing, candidate)
                stored = existing
                reused += 1
            except RecordNotFoundError:
                self._storage.metric_results.save(candidate)
                stored = self._storage.metric_results.get(candidate.result_id)
                created += 1
            persisted.append(stored)
        self._verify_traceability(persisted)
        return CryptoDerivativesMetricPersistenceSummary(
            results=tuple(persisted),
            results_created=created,
            results_reused=reused,
            missing_requirements=computation.missing_requirements,
            traceability_verified=True,
        )

    def _verify_traceability(self, results: list[MetricResult]) -> None:
        observation_ids = {
            observation_id for result in results for observation_id in result.input_observation_ids
        }
        observations = {
            observation_id: self._storage.observations.get(observation_id)
            for observation_id in observation_ids
        }
        raw_ids = {item.raw_record_id for item in observations.values()}
        raw_records = self._storage.raw_records.get_many(raw_ids)
        for result in results:
            if self._storage.metric_results.get(result.result_id) != result:
                raise StorageError("Deribit metric round-trip verification failed")
            if result.available_at > result.computed_at:
                raise StorageError("Deribit metric uses inputs after computed_at")
            for observation_id in result.input_observation_ids:
                observation = observations[observation_id]
                raw = raw_records[observation.raw_record_id]
                if (
                    observation.asset_id != result.asset_id
                    or observation.source != raw.source
                    or observation.available_at > result.available_at
                ):
                    raise StorageError("Deribit metric input traceability failed")


def _verify_equivalent(existing: MetricResult, candidate: MetricResult) -> None:
    existing_semantic = existing.model_dump(mode="python", exclude={"computed_at"})
    candidate_semantic = candidate.model_dump(mode="python", exclude={"computed_at"})
    if existing_semantic != candidate_semantic:
        raise StorageError("Deribit metric identity collides with different semantic content")


__all__ = ["CryptoDerivativesMetricPipeline"]
