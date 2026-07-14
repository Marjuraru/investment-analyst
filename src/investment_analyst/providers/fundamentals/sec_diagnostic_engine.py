"""Deterministic engine for point-in-time Apple fundamental diagnostics."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from uuid import UUID, uuid5

from investment_analyst.core.models import (
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticComputation,
    SecFundamentalDiagnosticMetric,
    SecFundamentalDiagnosticRequest,
    SecFundamentalDiagnosticSelection,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_rules import (
    ALGORITHM_VERSION,
    BASE_WEIGHTS,
    MINIMUM_COVERAGE,
    REQUIRED_SCORE_METRICS,
    SCORE_TOLERANCE,
    centered_evidence_contribution,
    confidence_for,
    coverage_for,
    evidence_direction,
    has_minimum_coverage,
    normalized_weights,
    quality_for,
    recency_factor,
    score_metric,
    verdict_for,
)

_DIAGNOSTIC_NAMESPACE = UUID("97aee6de-3d19-5ee5-a383-1bdb8e894b77")


class SecFundamentalDiagnosticEngineError(RuntimeError):
    """Base error for Apple fundamental diagnostic computation."""


class SecFundamentalDiagnosticComputationError(SecFundamentalDiagnosticEngineError):
    """Raised when selected metrics cannot support the published diagnostic rules."""


class SecFundamentalDiagnosticTraceabilityError(SecFundamentalDiagnosticEngineError):
    """Raised when a diagnostic cannot be reconstructed from its selected metrics."""


@dataclass(frozen=True)
class _DerivedDiagnostic:
    metrics: tuple[SecFundamentalDiagnosticMetric, ...]
    scores: dict[str, Decimal]
    weights: dict[str, Decimal]
    coverage: Decimal
    recency_factor: Decimal
    confidence: Decimal
    quality: DataQuality
    verdict: DiagnosticVerdict
    final_score: Decimal
    missing_requirements: tuple[str, ...]
    sufficient: bool


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecFundamentalDiagnosticComputationError(f"{name} must include timezone information")
    return value.astimezone(UTC)


def fundamental_diagnostic_id(
    selection: SecFundamentalDiagnosticSelection,
    *,
    algorithm_version: str = ALGORITHM_VERSION,
) -> UUID:
    """Return a UUID5 identity based only on period, version, and selected metric inputs."""
    document = {
        "asset_id": selection.request.asset_id,
        "mode": DiagnosticMode.FUNDAMENTAL.value,
        "frequency": selection.request.frequency.value,
        "target_period_end": (
            selection.target_period_end.isoformat() if selection.target_period_end else None
        ),
        "algorithm_version": algorithm_version,
        "selected_metric_result_ids": [str(item.result_id) for item in selection.selected_metrics],
    }
    canonical = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_DIAGNOSTIC_NAMESPACE, canonical)


def _missing_requirements(
    selection: SecFundamentalDiagnosticSelection,
    coverage: Decimal,
) -> tuple[str, ...]:
    missing: list[str] = []
    selected_names = {item.metric_name for item in selection.selected_metrics}
    if selection.target_period_end is None:
        missing.append("eligible_reporting_period")
    for metric_name in REQUIRED_SCORE_METRICS:
        if metric_name not in selected_names:
            missing.append(metric_name)
    if coverage < MINIMUM_COVERAGE:
        missing.append("coverage_at_least_0.60")
    for metric_name in selection.missing_metric_names:
        if metric_name not in missing:
            missing.append(metric_name)
    return tuple(missing)


def _derive(
    request: SecFundamentalDiagnosticRequest,
    selection: SecFundamentalDiagnosticSelection,
) -> _DerivedDiagnostic:
    if selection.request != request:
        raise SecFundamentalDiagnosticComputationError(
            "selection does not correspond to the diagnostic request"
        )
    if not selection.traceability_verified:
        raise SecFundamentalDiagnosticTraceabilityError("selection must have verified traceability")
    metrics = selection.selected_metrics
    names = tuple(item.metric_name for item in metrics)
    if names != tuple(sorted(names)) or len(set(names)) != len(names):
        raise SecFundamentalDiagnosticTraceabilityError(
            "selected metrics must be uniquely ordered by metric name"
        )
    for metric in metrics:
        if metric.frequency is not request.frequency:
            raise SecFundamentalDiagnosticTraceabilityError(
                "selected metric frequency does not match request"
            )
        if metric.available_at > request.known_at:
            raise SecFundamentalDiagnosticTraceabilityError(
                "selected metric was unavailable at known_at"
            )
        if selection.target_period_end is None or metric.period_end != selection.target_period_end:
            raise SecFundamentalDiagnosticTraceabilityError(
                "selected metric does not belong to target period"
            )
        if metric.metric_name not in BASE_WEIGHTS:
            raise SecFundamentalDiagnosticTraceabilityError(
                "selection contains an unsupported metric"
            )

    coverage = coverage_for(names)
    sufficient = selection.target_period_end is not None and has_minimum_coverage(names)
    missing = _missing_requirements(selection, coverage)
    if not metrics:
        return _DerivedDiagnostic(
            metrics=metrics,
            scores={},
            weights={},
            coverage=coverage,
            recency_factor=Decimal("0"),
            confidence=Decimal("0"),
            quality=DataQuality.PARTIAL,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0"),
            missing_requirements=missing,
            sufficient=False,
        )

    latest_available = max(item.available_at for item in metrics)
    recency = recency_factor(request.known_at, latest_available, request.frequency)
    if not sufficient:
        return _DerivedDiagnostic(
            metrics=metrics,
            scores={},
            weights={},
            coverage=coverage,
            recency_factor=recency,
            confidence=Decimal("0"),
            quality=DataQuality.PARTIAL,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0"),
            missing_requirements=missing,
            sufficient=False,
        )

    with localcontext(Context(prec=34)):
        scores = {item.metric_name: score_metric(item.metric_name, item.value) for item in metrics}
        weights = normalized_weights(names)
        final_score = sum(
            (scores[name] * weights[name] for name in names),
            Decimal("0"),
        )
        confidence = confidence_for(coverage, recency)
    quality = quality_for(sufficient=True, coverage=coverage, recency=recency)
    return _DerivedDiagnostic(
        metrics=metrics,
        scores=scores,
        weights=weights,
        coverage=coverage,
        recency_factor=recency,
        confidence=confidence,
        quality=quality,
        verdict=verdict_for(final_score),
        final_score=final_score,
        missing_requirements=missing,
        sufficient=True,
    )


def _rule_description(metric_name: str) -> str:
    descriptions = {
        "fundamental.net_margin": "linear score from 0.00 to 0.25",
        "fundamental.liabilities_to_assets": "inverse linear score from 0.40 to 0.90",
        "fundamental.liabilities_to_equity": "inverse linear score from 0.50 to 5.00",
        "fundamental.revenue_yoy_growth": "linear score from -0.10 to 0.15",
        "fundamental.net_income_yoy_change_rate": "linear score from -0.50 to 0.50",
    }
    return descriptions[metric_name]


def _normal_summary(
    request: SecFundamentalDiagnosticRequest,
    selection: SecFundamentalDiagnosticSelection,
    derived: _DerivedDiagnostic,
) -> str:
    assert selection.target_period_end is not None
    return (
        "Apple fundamental condition for "
        f"{request.frequency.value} period {selection.target_period_end.date().isoformat()} "
        f"is {derived.verdict.value} under explicit heuristic rules. This is descriptive, "
        "not a recommendation, and confidence summarizes coverage and recency rather than "
        "probability."
    )


def _insufficient_summary(
    request: SecFundamentalDiagnosticRequest,
    selection: SecFundamentalDiagnosticSelection,
    missing: tuple[str, ...],
) -> str:
    period = (
        selection.target_period_end.date().isoformat()
        if selection.target_period_end is not None
        else "no eligible period"
    )
    return (
        f"Apple {request.frequency.value} fundamental diagnostic for {period} has insufficient "
        f"data: {', '.join(missing)}. No directional interpretation is produced, and this is "
        "not a recommendation."
    )


class SecFundamentalDiagnosticEngine:
    """Apply published heuristic rules to selected persisted fundamental metrics."""

    def compute(
        self,
        request: SecFundamentalDiagnosticRequest,
        selection: SecFundamentalDiagnosticSelection,
        *,
        computed_at: datetime,
    ) -> SecFundamentalDiagnosticComputation:
        """Build one sufficient or insufficient point-in-time diagnostic."""
        computed_at = _utc(computed_at, "computed_at")
        derived = _derive(request, selection)
        if derived.metrics:
            available_at = max(item.available_at for item in derived.metrics)
        else:
            available_at = request.known_at
        if available_at > computed_at:
            raise SecFundamentalDiagnosticComputationError(
                "computed_at must not precede diagnostic availability"
            )
        as_of = selection.target_period_end or request.known_at
        diagnostic_id = fundamental_diagnostic_id(selection)

        if not derived.sufficient:
            diagnostic = DiagnosticResult(
                diagnostic_id=diagnostic_id,
                asset_id=request.asset_id,
                mode=DiagnosticMode.FUNDAMENTAL,
                verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
                final_score=Decimal("0"),
                confidence=Decimal("0"),
                as_of=as_of,
                available_at=available_at,
                computed_at=computed_at,
                components=[],
                evidence=[],
                algorithm_version=ALGORITHM_VERSION,
                summary=_insufficient_summary(
                    request,
                    selection,
                    derived.missing_requirements,
                ),
                quality=DataQuality.PARTIAL,
            )
        else:
            components = []
            evidence = []
            for metric in derived.metrics:
                score = derived.scores[metric.metric_name]
                weight = derived.weights[metric.metric_name]
                components.append(
                    DiagnosticComponent(
                        component_key=metric.metric_name,
                        score=score,
                        weight=weight,
                        weighted_contribution=score * weight,
                        metric_result_ids=[metric.result_id],
                        explanation=(
                            f"{metric.metric_name} value {metric.value} uses "
                            f"{_rule_description(metric.metric_name)}; interpretation is "
                            "descriptive."
                        ),
                    )
                )
                contribution = centered_evidence_contribution(score)
                evidence.append(
                    DiagnosticEvidence(
                        metric_result_id=metric.result_id,
                        direction=evidence_direction(contribution),
                        contribution=contribution,
                        reason=(
                            f"{metric.metric_name} value {metric.value} was scored with "
                            f"{_rule_description(metric.metric_name)}. This evidence is "
                            "descriptive."
                        ),
                    )
                )
            diagnostic = DiagnosticResult(
                diagnostic_id=diagnostic_id,
                asset_id=request.asset_id,
                mode=DiagnosticMode.FUNDAMENTAL,
                verdict=derived.verdict,
                final_score=derived.final_score,
                confidence=derived.confidence,
                as_of=as_of,
                available_at=available_at,
                computed_at=computed_at,
                components=components,
                evidence=evidence,
                algorithm_version=ALGORITHM_VERSION,
                summary=_normal_summary(request, selection, derived),
                quality=derived.quality,
            )

        return SecFundamentalDiagnosticComputation(
            request=request,
            selection=selection,
            diagnostic=diagnostic,
            missing_requirements=derived.missing_requirements,
            coverage=derived.coverage,
            recency_factor=derived.recency_factor,
            traceability_verified=True,
        )


def verify_fundamental_diagnostic_computation(
    computation: SecFundamentalDiagnosticComputation,
) -> None:
    """Recalculate published rules once and verify a generated diagnostic before persistence."""
    derived = _derive(computation.request, computation.selection)
    diagnostic = computation.diagnostic
    if diagnostic.diagnostic_id != fundamental_diagnostic_id(computation.selection):
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic ID does not match selected metric inputs"
        )
    if diagnostic.algorithm_version != ALGORITHM_VERSION:
        raise SecFundamentalDiagnosticTraceabilityError("diagnostic algorithm version is invalid")
    if computation.coverage != derived.coverage:
        raise SecFundamentalDiagnosticTraceabilityError("diagnostic coverage is inconsistent")
    if computation.recency_factor != derived.recency_factor:
        raise SecFundamentalDiagnosticTraceabilityError("diagnostic recency factor is inconsistent")
    if computation.missing_requirements != derived.missing_requirements:
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic missing requirements are inconsistent"
        )
    if diagnostic.available_at > diagnostic.computed_at:
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic was computed before it became available"
        )
    if diagnostic.as_of != (
        computation.selection.target_period_end or computation.request.known_at
    ):
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic as_of does not match the selected period"
        )

    if not derived.sufficient:
        if diagnostic.verdict is not DiagnosticVerdict.INSUFFICIENT_DATA:
            raise SecFundamentalDiagnosticTraceabilityError(
                "insufficient inputs require INSUFFICIENT_DATA"
            )
        if diagnostic.components or diagnostic.evidence:
            raise SecFundamentalDiagnosticTraceabilityError(
                "insufficient diagnostic must not contain components or evidence"
            )
        if diagnostic.final_score != 0 or diagnostic.confidence != 0:
            raise SecFundamentalDiagnosticTraceabilityError(
                "insufficient diagnostic score and confidence must be zero"
            )
        if diagnostic.quality is not DataQuality.PARTIAL:
            raise SecFundamentalDiagnosticTraceabilityError(
                "insufficient diagnostic must use PARTIAL quality"
            )
        return

    metric_by_name = {item.metric_name: item for item in derived.metrics}
    components = {item.component_key: item for item in diagnostic.components}
    if set(components) != set(metric_by_name):
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic components do not match selected metrics"
        )
    if len(diagnostic.evidence) != len(metric_by_name):
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic evidence count does not match selected metrics"
        )
    evidence_by_id = {item.metric_result_id: item for item in diagnostic.evidence}
    if set(evidence_by_id) != {item.result_id for item in derived.metrics}:
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic evidence does not match selected metrics"
        )

    for metric_name, metric in metric_by_name.items():
        component = components[metric_name]
        expected_score = derived.scores[metric_name]
        expected_weight = derived.weights[metric_name]
        if abs(component.score - expected_score) > SCORE_TOLERANCE:
            raise SecFundamentalDiagnosticTraceabilityError(
                f"component score is invalid for {metric_name}"
            )
        if abs(component.weight - expected_weight) > SCORE_TOLERANCE:
            raise SecFundamentalDiagnosticTraceabilityError(
                f"component weight is invalid for {metric_name}"
            )
        expected_contribution = expected_score * expected_weight
        if abs(component.weighted_contribution - expected_contribution) > SCORE_TOLERANCE:
            raise SecFundamentalDiagnosticTraceabilityError(
                f"component contribution is invalid for {metric_name}"
            )
        if component.metric_result_ids != [metric.result_id]:
            raise SecFundamentalDiagnosticTraceabilityError(
                f"component input is invalid for {metric_name}"
            )
        evidence = evidence_by_id[metric.result_id]
        expected_evidence = centered_evidence_contribution(expected_score)
        if abs(evidence.contribution - expected_evidence) > SCORE_TOLERANCE:
            raise SecFundamentalDiagnosticTraceabilityError(
                f"evidence contribution is invalid for {metric_name}"
            )
        if evidence.direction is not evidence_direction(expected_evidence):
            raise SecFundamentalDiagnosticTraceabilityError(
                f"evidence direction is invalid for {metric_name}"
            )

    weight_sum = sum((item.weight for item in diagnostic.components), Decimal("0"))
    if abs(weight_sum - Decimal("1")) > SCORE_TOLERANCE:
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic component weights do not sum to one"
        )
    contribution_sum = sum(
        (item.weighted_contribution for item in diagnostic.components),
        Decimal("0"),
    )
    if abs(contribution_sum - diagnostic.final_score) > SCORE_TOLERANCE:
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic score does not match weighted contributions"
        )
    if abs(diagnostic.final_score - derived.final_score) > SCORE_TOLERANCE:
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic final score does not match published rules"
        )
    if diagnostic.verdict is not derived.verdict:
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic verdict does not match published thresholds"
        )
    if diagnostic.confidence != derived.confidence:
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic confidence does not match coverage and recency"
        )
    if diagnostic.quality is not derived.quality:
        raise SecFundamentalDiagnosticTraceabilityError(
            "diagnostic quality does not match coverage and recency"
        )
