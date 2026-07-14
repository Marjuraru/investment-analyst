"""Persistence pipeline for deterministic point-in-time market diagnostics."""

from datetime import UTC, datetime
from decimal import Decimal

from investment_analyst.analytics.market.diagnostic_models import (
    MarketDiagnosticComputation,
    MarketDiagnosticRequest,
    MarketDiagnosticRunSummary,
    MarketMetricSnapshot,
)
from investment_analyst.analytics.market.diagnostic_rules import (
    ALGORITHM_VERSION,
    MarketDiagnosticEngine,
    diagnostic_result_id,
)
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
    describe_missing_requirements,
)
from investment_analyst.core.models import (
    DataQuality,
    DiagnosticResult,
    DiagnosticVerdict,
    MetricResult,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError


class MarketDiagnosticPipelineError(RuntimeError):
    """Base error for diagnostic persistence and verification."""


class DiagnosticIdentityConflictError(MarketDiagnosticPipelineError):
    """Raised when a deterministic diagnostic ID maps to different analytical content."""


class MarketDiagnosticTraceabilityError(MarketDiagnosticPipelineError):
    """Raised when a diagnostic cannot be traced to persisted source records."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalized_clock(clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketDiagnosticPipelineError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _quality(results: tuple[MetricResult, ...]) -> DataQuality:
    for candidate in (
        DataQuality.SUSPECT,
        DataQuality.PARTIAL,
        DataQuality.DELAYED,
        DataQuality.VALID,
    ):
        if any(result.quality is candidate for result in results):
            return candidate
    return DataQuality.PARTIAL


def _without_computed_at(result: DiagnosticResult) -> dict[str, object]:
    return result.model_dump(mode="python", exclude={"computed_at"})


def _metric_known_at(result: MetricResult) -> datetime:
    value = result.parameters.get("known_at")
    if not isinstance(value, str):
        raise MarketDiagnosticTraceabilityError("metric known_at parameter is invalid")
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise MarketDiagnosticTraceabilityError("metric known_at parameter is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketDiagnosticTraceabilityError("metric known_at parameter is naive")
    return parsed.astimezone(UTC)


class MarketDiagnosticPipeline:
    """Select persisted metrics, apply rules, persist, and verify one diagnostic."""

    def __init__(
        self,
        storage: LocalStorage,
        selector: MarketDiagnosticMetricSelector,
        engine: MarketDiagnosticEngine,
        *,
        clock=_utc_now,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._selector = selector
        self._engine = engine
        self._clock = clock

    def run(self, request: MarketDiagnosticRequest) -> MarketDiagnosticRunSummary:
        """Execute one idempotent market-diagnostic persistence run."""
        self._storage.require_open()
        before = self._counts()
        candidates = self._selector.candidates(request)
        snapshot = self._selector.select_from_results(request, candidates)
        missing = () if snapshot else describe_missing_requirements(request, candidates)
        computed_at = _normalized_clock(self._clock)
        generated = self._engine.compute(
            request,
            snapshot,
            computed_at=computed_at,
            fallback_metric_results=candidates,
        )
        expected_id = diagnostic_result_id(
            request,
            selected_metric_result_ids=snapshot.metric_result_ids() if snapshot else (),
            missing_requirements=missing,
            final_score=generated.final_score,
            confidence=generated.confidence,
            verdict=generated.verdict,
            quality=generated.quality,
        )
        if generated.diagnostic_id != expected_id:
            raise DiagnosticIdentityConflictError(
                "diagnostic engine returned an unexpected deterministic identity"
            )

        pre_persistence_computation = MarketDiagnosticComputation(
            request=request,
            snapshot=snapshot,
            diagnostic=generated,
            missing_requirements=missing,
            traceability_verified=True,
        )
        self._verify_generated(
            request,
            pre_persistence_computation,
            candidates,
        )

        created = 0
        reused = 0
        try:
            stored = self._storage.diagnostics.get(expected_id)
        except RecordNotFoundError:
            self._storage.diagnostics.save(generated)
            stored = self._storage.diagnostics.get(expected_id)
            created = 1
        else:
            if _without_computed_at(stored) != _without_computed_at(generated):
                raise DiagnosticIdentityConflictError(
                    f"diagnostic {expected_id} conflicts with its deterministic identity"
                )
            reused = 1

        computation = MarketDiagnosticComputation(
            request=request,
            snapshot=snapshot,
            diagnostic=stored,
            missing_requirements=missing,
            traceability_verified=True,
        )
        self._verify_run(request, computation, candidates, before, created)
        selected_ids = snapshot.metric_result_ids() if snapshot else ()
        return MarketDiagnosticRunSummary(
            asset_id=request.query.asset_id,
            source_id=request.query.source_id,
            requested_start=request.query.start,
            requested_end=request.query.end,
            known_at=request.query.known_at,
            as_of=stored.as_of,
            computed_at=stored.computed_at,
            verdict=stored.verdict,
            final_score=stored.final_score,
            confidence=stored.confidence,
            quality=stored.quality,
            selected_metric_result_ids=selected_ids,
            missing_requirements=missing,
            diagnostics_generated=1,
            diagnostics_created=created,
            diagnostics_reused=reused,
            traceability_verified=True,
        )

    def _counts(self) -> tuple[int, int, int, int]:
        try:
            return (
                len(self._storage.raw_records.list()),
                len(self._storage.observations.list()),
                len(self._storage.metric_results.list()),
                len(self._storage.diagnostics.list()),
            )
        except StorageError as error:
            raise MarketDiagnosticPipelineError("storage counts could not be read") from error

    def _verify_generated(
        self,
        request: MarketDiagnosticRequest,
        computation: MarketDiagnosticComputation,
        candidates: tuple[MetricResult, ...],
    ) -> None:
        diagnostic = computation.diagnostic
        if diagnostic.algorithm_version != ALGORITHM_VERSION:
            raise MarketDiagnosticTraceabilityError("unexpected diagnostic algorithm version")
        if diagnostic.asset_id != request.query.asset_id:
            raise MarketDiagnosticTraceabilityError("diagnostic asset does not match request")

        expected = self._engine.compute(
            request,
            computation.snapshot,
            computed_at=diagnostic.computed_at,
            fallback_metric_results=candidates,
        )
        if expected != diagnostic:
            raise MarketDiagnosticTraceabilityError("diagnostic does not match the published rules")

        for timestamp in (
            diagnostic.as_of,
            diagnostic.available_at,
            diagnostic.computed_at,
        ):
            if timestamp.tzinfo is not UTC:
                raise MarketDiagnosticTraceabilityError(
                    "diagnostic timestamps must be normalized to UTC"
                )

        if computation.snapshot is None:
            self._verify_insufficient(computation)
        else:
            self._verify_normal(
                request,
                computation.snapshot,
                diagnostic,
            )

    def _verify_run(
        self,
        request: MarketDiagnosticRequest,
        computation: MarketDiagnosticComputation,
        candidates: tuple[MetricResult, ...],
        before: tuple[int, int, int, int],
        created: int,
    ) -> None:
        diagnostic = computation.diagnostic
        self._verify_generated(request, computation, candidates)
        if self._storage.diagnostics.get(diagnostic.diagnostic_id) != diagnostic:
            raise MarketDiagnosticTraceabilityError("diagnostic storage round-trip failed")

        after = self._counts()
        if after[0] != before[0]:
            raise MarketDiagnosticTraceabilityError("diagnostic pipeline created raw records")
        if after[1] != before[1]:
            raise MarketDiagnosticTraceabilityError("diagnostic pipeline created observations")
        if after[2] != before[2]:
            raise MarketDiagnosticTraceabilityError("diagnostic pipeline created metric results")
        if after[3] != before[3] + created:
            raise MarketDiagnosticTraceabilityError("diagnostic count changed unexpectedly")
        if tuple(self._selector.candidates(request)) != candidates:
            raise MarketDiagnosticTraceabilityError("candidate metrics changed during the run")

    @staticmethod
    def _verify_insufficient(computation: MarketDiagnosticComputation) -> None:
        diagnostic = computation.diagnostic
        if diagnostic.verdict is not DiagnosticVerdict.INSUFFICIENT_DATA:
            raise MarketDiagnosticTraceabilityError("missing snapshot must be insufficient data")
        if diagnostic.components or diagnostic.evidence:
            raise MarketDiagnosticTraceabilityError(
                "insufficient diagnostic must not contain components or evidence"
            )
        if diagnostic.final_score != 0 or diagnostic.confidence != 0:
            raise MarketDiagnosticTraceabilityError(
                "insufficient diagnostic score and confidence must be zero"
            )
        if not all(item in diagnostic.summary for item in computation.missing_requirements):
            raise MarketDiagnosticTraceabilityError(
                "insufficient diagnostic summary does not explain missing requirements"
            )

    def _verify_normal(
        self,
        request: MarketDiagnosticRequest,
        snapshot: MarketMetricSnapshot,
        diagnostic: DiagnosticResult,
    ) -> None:
        selected = snapshot.metric_results()
        selected_ids = set(snapshot.metric_result_ids())
        if diagnostic.verdict is DiagnosticVerdict.INSUFFICIENT_DATA:
            raise MarketDiagnosticTraceabilityError("complete snapshot produced insufficient data")
        component_ids = {
            identifier
            for component in diagnostic.components
            for identifier in component.metric_result_ids
        }
        evidence_ids = {item.metric_result_id for item in diagnostic.evidence}
        if not component_ids <= selected_ids:
            raise MarketDiagnosticTraceabilityError("component contains an external metric")
        if evidence_ids != selected_ids or len(diagnostic.evidence) != 5:
            raise MarketDiagnosticTraceabilityError(
                "evidence must reference exactly the five selected metrics"
            )
        components = {item.component_key: item for item in diagnostic.components}
        if set(components) != {"trend_alignment", "recent_momentum"}:
            raise MarketDiagnosticTraceabilityError("diagnostic components are incomplete")
        if components["trend_alignment"].metric_result_ids != [
            snapshot.short_sma.result_id,
            snapshot.long_sma.result_id,
        ]:
            raise MarketDiagnosticTraceabilityError("trend component metric order is invalid")
        if components["recent_momentum"].metric_result_ids != [snapshot.simple_return.result_id]:
            raise MarketDiagnosticTraceabilityError("momentum component metric is invalid")

        for result in selected:
            persisted = self._storage.metric_results.get(result.result_id)
            if persisted != result:
                raise MarketDiagnosticTraceabilityError("selected metric round-trip failed")
            if result.asset_id != request.query.asset_id:
                raise MarketDiagnosticTraceabilityError("selected metric mixes assets")
            if result.parameters.get("source_id") != request.query.source_id:
                raise MarketDiagnosticTraceabilityError("selected metric mixes sources")
            if _metric_known_at(result) != request.query.known_at:
                raise MarketDiagnosticTraceabilityError("selected metric mixes known_at contexts")
            if result.available_at > request.query.known_at:
                raise MarketDiagnosticTraceabilityError(
                    "selected metric was unavailable at known_at"
                )
            observations = tuple(
                self._storage.observations.get(identifier)
                for identifier in result.input_observation_ids
            )
            if any(item.asset_id != request.query.asset_id for item in observations):
                raise MarketDiagnosticTraceabilityError("metric observations mix assets")
            if any(item.source.source_id != request.query.source_id for item in observations):
                raise MarketDiagnosticTraceabilityError("metric observations mix sources")
            if any(item.available_at > request.query.known_at for item in observations):
                raise MarketDiagnosticTraceabilityError(
                    "metric observation was unavailable at known_at"
                )
            if max(item.available_at for item in observations) != result.available_at:
                raise MarketDiagnosticTraceabilityError(
                    "metric available_at does not match observations"
                )
            observed_times = [item.observed_at for item in observations if item.observed_at]
            if not observed_times or max(observed_times) != result.as_of:
                raise MarketDiagnosticTraceabilityError("metric as_of does not match observations")
            for observation in observations:
                raw = self._storage.raw_records.get(observation.raw_record_id)
                if raw.asset_id != request.query.asset_id:
                    raise MarketDiagnosticTraceabilityError("raw record asset does not match")
                if raw.source.source_id != request.query.source_id:
                    raise MarketDiagnosticTraceabilityError("raw record source does not match")

        if diagnostic.available_at != max(item.available_at for item in selected):
            raise MarketDiagnosticTraceabilityError(
                "diagnostic available_at does not match selected metrics"
            )
        if diagnostic.as_of != snapshot.as_of:
            raise MarketDiagnosticTraceabilityError("diagnostic as_of does not match snapshot")
        if diagnostic.quality is not _quality(selected):
            raise MarketDiagnosticTraceabilityError("diagnostic quality does not match metrics")
        if sum((item.weight for item in diagnostic.components), Decimal("0")) != Decimal("1"):
            raise MarketDiagnosticTraceabilityError("diagnostic weights do not sum to one")
        contribution_sum = sum(
            (item.weighted_contribution for item in diagnostic.components),
            Decimal("0"),
        )
        if abs(diagnostic.final_score - contribution_sum) > Decimal("0.0001"):
            raise MarketDiagnosticTraceabilityError(
                "diagnostic score does not match weighted contributions"
            )
