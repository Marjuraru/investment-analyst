"""Persistence pipeline for deterministic Apple SEC fundamental metrics."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid5

from investment_analyst.core.models import DataQuality, MetricResult
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
)
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricCandidate,
    SecFundamentalMetricComputation,
    SecFundamentalMetricImportSummary,
    SecFundamentalMetricRequest,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
)
from investment_analyst.providers.fundamentals.sec_query_models import SecFundamentalQuery
from investment_analyst.storage import LocalStorage

_RESULT_NAMESPACE = UUID("fe4bb5e6-0983-4ff5-a82e-c20f0789b6c4")


class SecFundamentalMetricPipelineError(RuntimeError):
    """Base error for SEC fundamental metric persistence."""


class SecFundamentalMetricIdentityConflictError(SecFundamentalMetricPipelineError):
    """Raised when an existing result conflicts with its deterministic identity."""


class SecFundamentalMetricTraceabilityError(SecFundamentalMetricPipelineError):
    """Raised when a generated or stored metric cannot be audited."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def sec_fundamental_metric_result_id(candidate: SecFundamentalMetricCandidate) -> UUID:
    """Return the stable UUID5 identity for one fundamental metric candidate."""
    document = {
        "asset_id": candidate.asset_id,
        "metric_name": candidate.metric_name,
        "frequency": candidate.frequency.value,
        "period_end": candidate.period_end.isoformat(),
        "formula": candidate.formula,
        "algorithm_version": candidate.algorithm_version,
        "input_roles": [
            {"role": item.role, "observation_id": str(item.observation_id)}
            for item in candidate.input_roles
        ],
    }
    canonical = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_RESULT_NAMESPACE, canonical)


class SecAaplFundamentalMetricPipeline:
    """Query selected SEC facts once and persist deterministic metrics idempotently."""

    def __init__(
        self,
        storage: LocalStorage,
        point_in_time_service: SecAaplFundamentalPointInTimeService,
        engine: SecFundamentalMetricEngine,
        *,
        clock=_utc_now,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._point_in_time_service = point_in_time_service
        self._engine = engine
        self._clock = clock

    def run(
        self,
        request: SecFundamentalMetricRequest,
    ) -> SecFundamentalMetricImportSummary:
        """Execute one logically prevalidated and idempotent metric persistence run."""
        self._storage.require_open()
        counts_before = self._protected_counts()
        internal_query = SecFundamentalQuery(
            asset_id=request.asset_id,
            known_at=request.known_at,
            frequency=request.frequency,
            start_period_end=None,
            end_period_end=None,
            limit=None,
        )
        source_result = self._point_in_time_service.query(internal_query)
        computed_at = _utc_datetime(self._clock(), "computed_at")
        computation = self._engine.compute(
            request,
            source_result,
            computed_at=computed_at,
        )

        proposed = tuple(
            self._to_metric_result(candidate, computed_at) for candidate in computation.candidates
        )
        self._validate_proposed_results(proposed, computation)

        existing_results = self._storage.metric_results.list(asset_id=ASSET_ID)
        existing_by_id = {result.result_id: result for result in existing_results}
        if len(existing_by_id) != len(existing_results):
            raise SecFundamentalMetricPipelineError("stored metric result IDs are not unique")

        to_create: list[MetricResult] = []
        reused: list[MetricResult] = []
        for result in proposed:
            existing = existing_by_id.get(result.result_id)
            if existing is None:
                to_create.append(result)
            else:
                _verify_identity(existing, result)
                reused.append(existing)

        for result in to_create:
            self._storage.metric_results.save(result)

        stored = [self._storage.metric_results.get(result.result_id) for result in proposed]
        self._verify_stored_results(stored, computation)
        counts_after = self._protected_counts()
        if counts_after != counts_before:
            raise SecFundamentalMetricTraceabilityError(
                "fundamental metric pipeline changed source data or diagnostics"
            )

        target_set = set(computation.target_periods)
        target_views = [
            period for period in source_result.periods if period.period_end in target_set
        ]
        return SecFundamentalMetricImportSummary(
            asset_id=ASSET_ID,
            known_at=request.known_at,
            frequency=request.frequency,
            computed_at=computed_at,
            periods_examined=len(source_result.periods),
            target_periods=len(target_views),
            complete_periods=sum(period.is_complete for period in target_views),
            incomplete_periods=sum(not period.is_complete for period in target_views),
            metrics_generated=len(proposed),
            metrics_created=len(to_create),
            metrics_reused=len(reused),
            metric_counts=computation.metric_counts,
            skipped_counts=computation.skipped_counts,
            earliest_period_end=(target_views[0].period_end if target_views else None),
            latest_period_end=(target_views[-1].period_end if target_views else None),
            raw_records_created=0,
            observations_created=0,
            diagnostics_created=0,
            traceability_verified=True,
        )

    @staticmethod
    def _to_metric_result(
        candidate: SecFundamentalMetricCandidate,
        computed_at: datetime,
    ) -> MetricResult:
        input_roles = [
            {"role": item.role, "observation_id": str(item.observation_id)}
            for item in candidate.input_roles
        ]
        parameters = {
            "source_id": COMPANYFACTS_SOURCE_ID,
            "frequency": candidate.frequency.value,
            "period_end": candidate.period_end.isoformat(),
            "comparison": candidate.comparison.value,
            "formula": candidate.formula,
            "input_roles": input_roles,
            "fiscal_year": candidate.fiscal_year,
            "fiscal_period": candidate.fiscal_period,
        }
        return MetricResult(
            result_id=sec_fundamental_metric_result_id(candidate),
            asset_id=candidate.asset_id,
            metric_key=candidate.metric_name,
            value=candidate.value,
            unit=candidate.unit,
            as_of=candidate.period_end,
            available_at=candidate.available_at,
            computed_at=computed_at,
            parameters=parameters,
            input_observation_ids=list(candidate.input_observation_ids()),
            algorithm_version=candidate.algorithm_version,
            quality=candidate.quality,
        )

    @staticmethod
    def _validate_proposed_results(
        results: tuple[MetricResult, ...],
        computation: SecFundamentalMetricComputation,
    ) -> None:
        if len(results) != len(computation.candidates):
            raise SecFundamentalMetricPipelineError("candidate conversion lost results")
        identifiers = tuple(result.result_id for result in results)
        if len(set(identifiers)) != len(identifiers):
            raise SecFundamentalMetricIdentityConflictError(
                "multiple candidates produced the same deterministic result ID"
            )
        facts = {
            fact.observation_id: fact
            for period in computation.source_result.periods
            for fact in period.facts
        }
        for result, candidate in zip(results, computation.candidates, strict=True):
            expected_id = sec_fundamental_metric_result_id(candidate)
            if result.result_id != expected_id:
                raise SecFundamentalMetricIdentityConflictError(
                    "metric result ID does not match its canonical inputs"
                )
            input_facts = []
            for identifier in result.input_observation_ids:
                fact = facts.get(identifier)
                if fact is None:
                    raise SecFundamentalMetricTraceabilityError(
                        "metric references an observation outside the point-in-time result"
                    )
                input_facts.append(fact)
            if any(fact.source_id != COMPANYFACTS_SOURCE_ID for fact in input_facts):
                raise SecFundamentalMetricTraceabilityError("metric mixes fundamental sources")
            if any(fact.available_at > computation.request.known_at for fact in input_facts):
                raise SecFundamentalMetricTraceabilityError(
                    "metric uses an observation unavailable at known_at"
                )
            if max(fact.available_at for fact in input_facts) != result.available_at:
                raise SecFundamentalMetricTraceabilityError(
                    "metric available_at does not match its latest input"
                )
            if result.as_of != candidate.period_end:
                raise SecFundamentalMetricTraceabilityError(
                    "metric as_of does not match the current reporting period"
                )
            if result.quality is not DataQuality.VALID:
                raise SecFundamentalMetricTraceabilityError("metric quality must remain VALID")

    def _verify_stored_results(
        self,
        results: list[MetricResult],
        computation: SecFundamentalMetricComputation,
    ) -> None:
        expected = {
            sec_fundamental_metric_result_id(candidate): candidate
            for candidate in computation.candidates
        }
        if len(results) != len(expected):
            raise SecFundamentalMetricTraceabilityError("stored result count is inconsistent")
        for result in results:
            candidate = expected.get(result.result_id)
            if candidate is None:
                raise SecFundamentalMetricTraceabilityError("unexpected stored result returned")
            expected_result = self._to_metric_result(candidate, result.computed_at)
            _verify_identity(result, expected_result)
            if result.computed_at.tzinfo is not UTC:
                raise SecFundamentalMetricTraceabilityError(
                    "stored computed_at must be normalized to UTC"
                )

    def _protected_counts(self) -> tuple[int, int, int]:
        connection = self._storage.store.connection
        tables = (
            "raw_record_index",
            "normalized_observations",
            "diagnostic_results",
        )
        counts: list[int] = []
        for table in tables:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
            if row is None:
                raise SecFundamentalMetricPipelineError(f"could not count protected table {table}")
            counts.append(int(row[0]))
        return counts[0], counts[1], counts[2]


def _verify_identity(existing: MetricResult, expected: MetricResult) -> None:
    fields = (
        "result_id",
        "asset_id",
        "metric_key",
        "value",
        "unit",
        "as_of",
        "available_at",
        "parameters",
        "input_observation_ids",
        "algorithm_version",
        "quality",
    )
    if any(getattr(existing, field) != getattr(expected, field) for field in fields):
        raise SecFundamentalMetricIdentityConflictError(
            f"metric result {expected.result_id} conflicts with its deterministic identity"
        )


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecFundamentalMetricPipelineError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)
