"""Append-only persistence pipeline for institutional descriptive metrics."""

from collections import Counter
from datetime import UTC, datetime

from investment_analyst.analytics.cazatiburones.institutional_composition_candidates import (
    candidates_by_period,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_engine import resolve
from investment_analyst.analytics.cazatiburones.institutional_metric_definitions import (
    ALGORITHM_VERSION,
    INSTITUTIONAL_METRIC_DEFINITIONS,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_engine import calculate
from investment_analyst.analytics.cazatiburones.institutional_metric_identity import (
    expected_institutional_metric_result_id,
    semantic_institutional_metric_result_id,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_models import (
    InstitutionalMetricClose,
    InstitutionalMetricRunSummary,
)
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
)
from investment_analyst.evidence.sec_institutional_semantics.artifact_reader import (
    InstitutionalSemanticsArtifactReader,
)
from investment_analyst.storage import StorageError

_OBSERVATION_REFERENCE_BATCH_SIZE = 512


class InstitutionalMetricPipeline:
    def __init__(self, storage, *, clock=lambda: datetime.now(UTC)) -> None:
        self._storage, self._clock = storage, clock

    def compute(
        self, *, asset_id: str, manager_cik: str, known_at: datetime
    ) -> InstitutionalMetricRunSummary:
        if self._storage.read_only:
            raise StorageError("institutional metric computation requires writable storage")
        manager = normalize_cik(manager_cik)
        computed_at = self._clock().astimezone(UTC)
        for definition in INSTITUTIONAL_METRIC_DEFINITIONS:
            self._storage.metric_definitions.upsert(definition)
        artifacts = InstitutionalSemanticsArtifactReader(
            self._storage.raw_records
        ).list_for_manager(manager_cik=manager, known_at=known_at)
        periods = candidates_by_period(artifacts, manager_cik=manager)
        observations_service = InstitutionalObservationService(self._storage)
        references = observations_service.list_for_manager(
            asset_id=asset_id, manager_cik=manager, known_at=known_at
        )
        observations = tuple(
            observations_service.observation_ids_for_references(
                tuple(item.observation_id for item in references)
            ).values()
        )
        by_artifact = {}
        for observation in observations:
            import json

            by_artifact.setdefault(
                json.loads(observation.source.record_key)["artifact_id"], []
            ).append(observation)
        closes = tuple(
            InstitutionalMetricClose(
                report_period=period,
                artifact_id=result.effective_artifact_id,
                status=result.status,
                observations=tuple(by_artifact.get(str(result.effective_artifact_id), ())),
            )
            for period, candidates in sorted(
                periods.items(), key=lambda item: (item[0] is None, item[0])
            )
            if period is not None
            for result in (
                resolve(
                    manager_cik=manager,
                    report_period=period,
                    known_at=known_at,
                    candidates=candidates,
                ),
            )
        )
        engine = calculate(asset_id=asset_id, manager_cik=manager, known_at=known_at, closes=closes)
        semantic_v2 = self._storage.paths.format_version == 2
        identifiers = {
            (
                semantic_institutional_metric_result_id(candidate)
                if semantic_v2
                else expected_institutional_metric_result_id(candidate)
            )
            for candidate in engine.candidates
        }
        existing_by_id = self._storage.metric_results.get_existing(identifiers)
        created = reused = 0
        for candidate in engine.candidates:
            result_id = (
                semantic_institutional_metric_result_id(candidate)
                if semantic_v2
                else expected_institutional_metric_result_id(candidate)
            )
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
                algorithm_version=ALGORITHM_VERSION,
                quality=candidate.quality,
            )
            existing = existing_by_id.get(result.result_id)
            if existing is None:
                self._storage.metric_results.save_many([result])
                created += 1
            else:
                if existing.model_dump(exclude={"computed_at"}) != result.model_dump(
                    exclude={"computed_at"}
                ):
                    raise StorageError("institutional metric identity conflicts")
                reused += 1
        reasons = Counter(item.reason for item in engine.skipped)
        return InstitutionalMetricRunSummary(
            asset_id=asset_id,
            manager_cik=manager,
            known_at=known_at,
            computed_at=computed_at,
            values_examined=len(engine.candidates) + len(engine.skipped),
            metrics_generated=len(engine.candidates),
            metrics_created=created,
            metrics_reused=reused,
            skipped_total=len(engine.skipped),
            skipped_by_reason=dict(reasons),
        )
