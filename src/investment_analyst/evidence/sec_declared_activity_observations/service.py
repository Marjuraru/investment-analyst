"""Idempotent persistence service for declared-activity observations.

Composes already-persisted Form 3/4/5 and Schedule 13D/13G evidence at a fixed ``known_at``
cut, normalizes it with the pure functions in ``normalizer``, and saves each resulting
``NormalizedObservation`` through the existing append-only ``DuckDBObservationRepository``.
A late failure preserves every observation already saved; there is no global rollback.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.evidence.sec_declared_activity_observations.models import (
    DeclaredActivityObservationRunSummary,
)
from investment_analyst.evidence.sec_declared_activity_observations.normalizer import (
    DeclaredActivityNormalizationResult,
    normalize_beneficial_ownership_statement,
    normalize_ownership_statement,
)
from investment_analyst.evidence.sec_ownership.repository import OwnershipRepository
from investment_analyst.storage import RecordNotFoundError, StorageError
from investment_analyst.storage.local import LocalStorage


class DeclaredActivityObservationServiceError(RuntimeError):
    """Base error for the declared-activity observation normalization service."""


class DeclaredActivityObservationIdentityConflictError(DeclaredActivityObservationServiceError):
    """Raised when one observation identity would receive contradictory content."""


class DeclaredActivityObservationService:
    """Compose insider and beneficial-ownership evidence into append-only observations."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._clock = clock

    def normalize(
        self, *, asset_id: str, known_at: datetime
    ) -> DeclaredActivityObservationRunSummary:
        """Normalize and persist declared activity for one asset up to one PIT cut."""
        self._storage.require_open()
        if self._storage.read_only:
            raise StorageError(
                "declared activity observation normalization requires writable storage"
            )
        normalized_at = _require_utc(self._clock())

        insider_statements = OwnershipRepository(self._storage.raw_records).list(
            asset_id=asset_id, known_at=known_at
        )
        beneficial_statements = BeneficialOwnershipRepository(self._storage.raw_records).list(
            asset_id=asset_id, known_at=known_at
        )

        results: list[DeclaredActivityNormalizationResult] = [
            normalize_ownership_statement(statement, normalized_at=normalized_at)
            for statement in insider_statements
        ] + [
            normalize_beneficial_ownership_statement(statement, normalized_at=normalized_at)
            for statement in beneficial_statements
        ]

        candidates_by_id: dict[UUID, NormalizedObservation] = {}
        skip_counts: Counter[str] = Counter()
        skipped_total = 0
        for result in results:
            skipped_total += len(result.skipped)
            skip_counts.update(item.reason for item in result.skipped)
            for candidate in result.observations:
                existing = candidates_by_id.get(candidate.observation_id)
                if existing is not None and existing != candidate:
                    raise DeclaredActivityObservationIdentityConflictError(
                        f"observation {candidate.observation_id} is contradictory within this run"
                    )
                candidates_by_id[candidate.observation_id] = candidate

        created = 0
        reused = 0
        for observation_id, candidate in candidates_by_id.items():
            try:
                existing = self._storage.observations.get(observation_id)
            except RecordNotFoundError:
                self._storage.observations.save(candidate)
                created += 1
            else:
                if _semantic_identity(existing) != _semantic_identity(candidate):
                    raise DeclaredActivityObservationIdentityConflictError(
                        f"observation {observation_id} already has different declared content"
                    )
                reused += 1

        return DeclaredActivityObservationRunSummary(
            asset_id=asset_id,
            known_at=known_at,
            normalized_at=normalized_at,
            statements_examined=len(insider_statements) + len(beneficial_statements),
            values_examined=len(candidates_by_id) + skipped_total,
            observations_generated=len(candidates_by_id),
            observations_created=created,
            observations_reused=reused,
            skipped_total=skipped_total,
            skipped_by_reason=dict(skip_counts),
        )


def _semantic_identity(observation: NormalizedObservation) -> tuple[object, ...]:
    """Identity-relevant content, excluding the normalization clock.

    ``normalized_at`` legitimately differs between an original run and a later idempotent
    rerun; the append-only repository compares raw document bytes, so re-saving an
    otherwise-identical candidate with a new clock would look like a spurious conflict.
    This comparison is what actually decides reuse versus a genuine content conflict.
    """
    return (
        observation.observation_id,
        observation.raw_record_id,
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
        observation.source.record_key,
        observation.source.raw_uri,
        observation.source.checksum_sha256,
    )


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DeclaredActivityObservationServiceError(
            "clock result must include timezone information"
        )
    return value.astimezone(UTC)
