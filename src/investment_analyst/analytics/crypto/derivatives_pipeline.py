"""Append-only persistence for provider-independent derivatives metrics."""

from collections.abc import Callable, Collection
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.analytics.crypto.derivatives_engine import (
    METRIC_DEFINITIONS,
    CryptoDerivativesMetricEngine,
)
from investment_analyst.analytics.crypto.derivatives_models import (
    CryptoDerivativesMetricPersistenceSummary,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import MetricResult
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError
from investment_analyst.storage.repositories import DuckDBMetricResultRepository


def _lookup_existing_metrics(
    repository: DuckDBMetricResultRepository,
    identifiers: Collection[UUID],
) -> dict[UUID, MetricResult]:
    if not identifiers:
        return {}
    try:
        return repository.get_many(identifiers)
    except RecordNotFoundError:
        pass
    found: dict[UUID, MetricResult] = {}
    for identifier in identifiers:
        try:
            found[identifier] = repository.get(identifier)
        except RecordNotFoundError:
            continue
    return found


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

        candidate_ids = {candidate.result_id for candidate in computation.results}
        existing_results = _lookup_existing_metrics(self._storage.metric_results, candidate_ids)

        created = 0
        reused = 0
        to_save: list[MetricResult] = []
        persisted: list[MetricResult] = []

        for candidate in computation.results:
            existing = existing_results.get(candidate.result_id)
            if existing is not None:
                _verify_equivalent(existing, candidate)
                persisted.append(existing)
                reused += 1
            else:
                to_save.append(candidate)
                persisted.append(candidate)
                created += 1

        if to_save:
            receipt = self._storage.metric_results.save_many(to_save)
        else:
            receipt = BatchWriteReceipt()

        self._verify_traceability(persisted, receipt)
        return CryptoDerivativesMetricPersistenceSummary(
            results=tuple(persisted),
            results_created=created,
            results_reused=reused,
            missing_requirements=computation.missing_requirements,
            traceability_verified=True,
        )

    def _verify_traceability(
        self,
        results: list[MetricResult],
        receipt: BatchWriteReceipt,
    ) -> None:
        new_ids = set(receipt.created_ids)
        if not new_ids:
            return

        new_results = [result for result in results if result.result_id in new_ids]
        observation_ids = {
            observation_id
            for result in new_results
            for observation_id in result.input_observation_ids
        }
        observations = (
            self._storage.observations.get_many(observation_ids) if observation_ids else {}
        )
        raw_ids = {item.raw_record_id for item in observations.values()}
        raw_records = self._storage.raw_records.get_many(raw_ids) if raw_ids else {}
        for result in new_results:
            if result.available_at > result.computed_at:
                raise StorageError("Deribit metric uses inputs after computed_at")
            for observation_id in result.input_observation_ids:
                if observation_id not in observations:
                    raise StorageError("Deribit metric input traceability failed")
                observation = observations[observation_id]
                if observation.raw_record_id not in raw_records:
                    raise StorageError("Deribit metric input traceability failed")
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
