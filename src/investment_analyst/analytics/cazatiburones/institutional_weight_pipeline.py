"""Append-only persistence pipeline for declared 13F position-value weights."""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from investment_analyst.analytics.cazatiburones.institutional_close_totals import (
    effective_close_total,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_candidates import (
    candidates_by_period,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_engine import resolve
from investment_analyst.analytics.cazatiburones.institutional_weight_definitions import (
    ALGORITHM_VERSION,
    INSTITUTIONAL_WEIGHT_DEFINITIONS,
    WEIGHT_FIELDS,
)
from investment_analyst.analytics.cazatiburones.institutional_weight_engine import calculate
from investment_analyst.analytics.cazatiburones.institutional_weight_identity import (
    expected_weight_result_id,
)
from investment_analyst.analytics.cazatiburones.institutional_weight_models import (
    InstitutionalWeightRunSummary,
    InstitutionalWeightSkip,
)
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_observations.definitions import SOURCE_ID
from investment_analyst.evidence.sec_institutional_semantics.models import (
    SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
    SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    semantics_from_raw_record,
)
from investment_analyst.storage import RecordNotFoundError, StorageError


class InstitutionalWeightPipeline:
    """Persist one MetricResult per visible as-filed position and weight field."""

    def __init__(self, storage, *, clock=lambda: datetime.now(UTC)) -> None:
        self._storage, self._clock = storage, clock

    def compute(
        self, *, asset_id: str, manager_cik: str, known_at: datetime
    ) -> InstitutionalWeightRunSummary:
        if self._storage.read_only:
            raise StorageError("institutional weight computation requires writable storage")
        manager = normalize_cik(manager_cik)
        computed_at = self._clock().astimezone(UTC)
        for definition in INSTITUTIONAL_WEIGHT_DEFINITIONS:
            self._storage.metric_definitions.upsert(definition)
        artifacts = tuple(
            semantics_from_raw_record(record)
            for record in self._storage.raw_records.list(
                source_id=SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
                schema_version=SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
                available_to=known_at,
            )
        )
        artifact_by_id = {item.artifact_id: item for item in artifacts}
        observations = tuple(
            self._storage.observations.list(
                asset_id=asset_id, source_id=SOURCE_ID, available_to=known_at
            )
        )
        positions = _positions_by_artifact(observations)
        skipped: list[InstitutionalWeightSkip] = []
        candidates = []
        for report_period, close_candidates in sorted(
            candidates_by_period(artifacts, manager_cik=manager).items(),
            key=lambda item: (item[0] is None, item[0]),
        ):
            result = resolve(
                manager_cik=manager,
                report_period=report_period,
                known_at=known_at,
                candidates=close_candidates,
            )
            if report_period is None or result.status not in {"original_complete", "amended"}:
                skipped.extend(
                    InstitutionalWeightSkip(metric_key=key, reason="unresolved_close")
                    for key, _ in WEIGHT_FIELDS
                )
                continue
            effective = artifact_by_id.get(result.effective_artifact_id)
            if effective is None:
                skipped.extend(
                    InstitutionalWeightSkip(metric_key=key, reason="missing_total")
                    for key, _ in WEIGHT_FIELDS
                )
                continue
            total, total_quality = effective_close_total(effective)
            position_groups = positions.get(effective.artifact_id, {})
            if not position_groups:
                skipped.extend(
                    InstitutionalWeightSkip(metric_key=key, reason="missing_position")
                    for key, _ in WEIGHT_FIELDS
                )
                continue
            for row_id, values in position_groups.items():
                lineage = _lineage(values, row_id)
                engine = calculate(
                    asset_id=asset_id,
                    manager_cik=manager,
                    report_period=report_period.isoformat(),
                    known_at=known_at,
                    artifact_id=str(effective.artifact_id),
                    accession=effective.accession,
                    status=result.status,
                    total=total,
                    total_quality=total_quality,
                    observations=values,
                    lineage=lineage,
                )
                candidates.extend(engine.candidates)
                skipped.extend(engine.skipped)
        created = reused = 0
        for candidate in candidates:
            observation = _observation_by_id(observations, candidate.input_observation_id)
            result = MetricResult(
                result_id=expected_weight_result_id(
                    asset_id=candidate.asset_id,
                    metric_key=candidate.metric_key,
                    known_at=candidate.known_at,
                    parameters=candidate.parameters,
                    input_observation_id=candidate.input_observation_id,
                ),
                asset_id=candidate.asset_id,
                metric_key=candidate.metric_key,
                value=candidate.value,
                unit="ratio",
                as_of=observation.period_end,
                available_at=candidate.available_at,
                computed_at=computed_at,
                parameters=candidate.parameters,
                input_observation_ids=[candidate.input_observation_id],
                algorithm_version=ALGORITHM_VERSION,
                quality=candidate.quality,
            )
            try:
                existing = self._storage.metric_results.get(result.result_id)
            except RecordNotFoundError:
                self._storage.metric_results.save(result)
                created += 1
            else:
                if existing.model_dump(exclude={"computed_at"}) != result.model_dump(
                    exclude={"computed_at"}
                ):
                    raise StorageError("institutional weight identity conflicts")
                reused += 1
        reasons = Counter(item.reason for item in skipped)
        return InstitutionalWeightRunSummary(
            asset_id=asset_id,
            manager_cik=manager,
            known_at=known_at,
            computed_at=computed_at,
            values_examined=len(candidates) + len(skipped),
            metrics_generated=len(candidates),
            metrics_created=created,
            metrics_reused=reused,
            skipped_total=len(skipped),
            skipped_by_reason=dict(reasons),
        )


def _positions_by_artifact(
    observations: tuple[NormalizedObservation, ...],
) -> dict[UUID, dict[str, tuple[NormalizedObservation, ...]]]:
    grouped: dict[UUID, dict[str, list[NormalizedObservation]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for observation in observations:
        record_key = json.loads(observation.source.record_key)
        artifact_id = UUID(cast(str, record_key["artifact_id"]))
        row_id = cast(str, record_key["row_id"])
        grouped[artifact_id][row_id].append(observation)
    return {
        artifact_id: {row_id: tuple(values) for row_id, values in rows.items()}
        for artifact_id, rows in grouped.items()
    }


def _lineage(observations: tuple[NormalizedObservation, ...], row_id: str) -> dict[str, str | None]:
    key = json.loads(observations[0].source.record_key)
    return {
        "row_id": row_id,
        "cusip": cast(str, key["cusip"]),
        "title_of_class": cast(str, key["title_of_class"]),
        "put_call": cast(str | None, key["put_call"]),
        "cover_revision_id": cast(str, key["cover_revision_id"]),
        "cover_content_sha256": cast(str, key["cover_content_sha256"]),
        "information_table_revision_id": cast(str, key["information_table_revision_id"]),
        "information_table_content_sha256": cast(str, key["information_table_content_sha256"]),
        "monetary_policy_version": cast(str, key["monetary_policy_version"]),
        "filing_accepted_at": cast(str, key["filing_accepted_at"]),
    }


def _observation_by_id(
    observations: tuple[NormalizedObservation, ...], observation_id: UUID
) -> NormalizedObservation:
    for observation in observations:
        if observation.observation_id == observation_id:
            return observation
    raise StorageError("institutional weight input observation is missing")
