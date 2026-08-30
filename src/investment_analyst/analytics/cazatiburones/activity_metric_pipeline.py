# ruff: noqa: E501
"""Idempotent persistence pipeline for cazatiburones activity metrics (layer 3).

Reads already-persisted Form 3/4/5 and Schedule 13D/13G statements and the layer-2
``NormalizedObservation`` they were normalized into, computes candidates with the pure
engine, assigns deterministic identity, and saves each result through the existing
append-only ``DuckDBMetricResultRepository``. A late failure preserves every metric already
persisted; there is no global rollback of the run.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.analytics.cazatiburones.activity_metric_definitions import (
    ACTIVITY_METRIC_DEFINITIONS,
    BENEFICIAL_LAYER2_FIELDS,
    INSIDER_LAYER2_FIELDS,
)
from investment_analyst.analytics.cazatiburones.activity_metric_engine import (
    calculate_beneficial_activity_metrics,
    calculate_insider_activity_metrics,
)
from investment_analyst.analytics.cazatiburones.activity_metric_identity import (
    expected_activity_metric_result_id,
)
from investment_analyst.analytics.cazatiburones.activity_metric_models import (
    ActivityMetricCandidate,
    ActivityMetricRunSummary,
)
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.evidence.sec_declared_activity_observations.normalizer import (
    expected_observation_id,
)
from investment_analyst.evidence.sec_ownership.models import OWNERSHIP_SOURCE_ID, OwnershipStatement
from investment_analyst.evidence.sec_ownership.repository import OwnershipRepository
from investment_analyst.storage import RecordNotFoundError, StorageError
from investment_analyst.storage.local import LocalStorage


class ActivityMetricPipelineError(RuntimeError):
    """Base error for the cazatiburones activity metric pipeline."""


class ActivityMetricIdentityConflictError(ActivityMetricPipelineError):
    """Raised when a persisted metric identity already carries different semantic content."""


class ActivityMetricPipeline:
    """Compose declared-activity evidence, its layer-2 observations, and layer-3 metrics."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._clock = clock

    def compute(self, *, asset_id: str, known_at: datetime) -> ActivityMetricRunSummary:
        """Compute and persist activity metrics for one asset up to one PIT cut."""
        self._storage.require_open()
        if self._storage.read_only:
            raise StorageError(
                "cazatiburones activity metric computation requires writable storage"
            )
        computed_at = _require_utc(self._clock())

        for definition in ACTIVITY_METRIC_DEFINITIONS:
            self._storage.metric_definitions.upsert(definition)

        insider_statements = OwnershipRepository(self._storage.raw_records).list(
            asset_id=asset_id, known_at=known_at
        )
        beneficial_statements = BeneficialOwnershipRepository(self._storage.raw_records).list(
            asset_id=asset_id, known_at=known_at
        )
        observation_index = self._build_observation_index(
            insider_statements=insider_statements, beneficial_statements=beneficial_statements
        )

        insider_result = calculate_insider_activity_metrics(
            insider_statements, observations=observation_index, known_at=known_at
        )
        beneficial_result = calculate_beneficial_activity_metrics(
            beneficial_statements, observations=observation_index, known_at=known_at
        )
        candidates = insider_result.candidates + beneficial_result.candidates
        skip_counts: Counter[str] = Counter()
        for skip in insider_result.skipped + beneficial_result.skipped:
            skip_counts[skip.reason] += 1
        skipped_total = sum(skip_counts.values())

        created = 0
        reused = 0
        for candidate in candidates:
            created_here = self._persist(candidate, computed_at=computed_at)
            if created_here:
                created += 1
            else:
                reused += 1

        return ActivityMetricRunSummary(
            asset_id=asset_id,
            known_at=known_at,
            computed_at=computed_at,
            values_examined=len(candidates) + skipped_total,
            metrics_generated=len(candidates),
            metrics_created=created,
            metrics_reused=reused,
            skipped_total=skipped_total,
            skipped_by_reason=dict(skip_counts),
        )

    def _persist(self, candidate: ActivityMetricCandidate, *, computed_at: datetime) -> bool:
        """Save one candidate; return True when a new row was created, False when reused."""
        result_id = expected_activity_metric_result_id(candidate)
        result = MetricResult(
            result_id=result_id,
            asset_id=candidate.asset_id,
            metric_key=candidate.metric_key,
            value=candidate.value,
            unit=candidate.unit,
            as_of=candidate.as_of,
            available_at=candidate.available_at,
            computed_at=computed_at,
            parameters=candidate.parameters,
            input_observation_ids=list(candidate.input_observation_ids),
            algorithm_version=candidate.algorithm_version,
            quality=candidate.quality,
        )
        try:
            existing = self._storage.metric_results.get(result_id)
        except RecordNotFoundError:
            self._storage.metric_results.save(result)
            return True
        if _semantic_identity(existing) != _semantic_identity(result):
            raise ActivityMetricIdentityConflictError(
                f"activity metric result {result_id} already has different content"
            )
        return False

    def _build_observation_index(
        self,
        *,
        insider_statements: list[OwnershipStatement],
        beneficial_statements: list[BeneficialOwnershipStatement],
    ) -> dict[UUID, NormalizedObservation]:
        """Read, once, every layer-2 observation this run's candidates could reference."""
        index: dict[UUID, NormalizedObservation] = {}
        for statement in insider_statements:
            for entry in statement.entries:
                if entry.kind != "transaction":
                    continue
                for field_name in INSIDER_LAYER2_FIELDS:
                    self._add_if_present(
                        index,
                        expected_observation_id(
                            source_id=OWNERSHIP_SOURCE_ID,
                            statement_id=statement.statement_id,
                            entry_id=entry.entry_id,
                            field_name=field_name,
                        ),
                    )
        for statement in beneficial_statements:
            for field_name in BENEFICIAL_LAYER2_FIELDS:
                self._add_if_present(
                    index,
                    expected_observation_id(
                        source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
                        statement_id=statement.statement_id,
                        entry_id=None,
                        field_name=field_name,
                    ),
                )
        return index

    def _add_if_present(
        self, index: dict[UUID, NormalizedObservation], observation_id: UUID
    ) -> None:
        try:
            index[observation_id] = self._storage.observations.get(observation_id)
        except RecordNotFoundError:
            return


def _semantic_identity(result: MetricResult) -> dict[str, object]:
    """Identity-relevant content, excluding the computation clock.

    ``computed_at`` legitimately differs between an original run and a later idempotent
    rerun; comparing full documents byte-for-byte would treat that as a spurious conflict.
    """
    return result.model_dump(exclude={"computed_at"})


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ActivityMetricPipelineError("clock result must include timezone information")
    return value.astimezone(UTC)
