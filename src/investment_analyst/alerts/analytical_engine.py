"""Pure deterministic engine for independent market or fundamental rules."""

from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionResult,
    AnalyticalConditionState,
    AnalyticalRuleState,
    AnalyticalScreeningCondition,
    AnalyticalScreeningRequest,
    AnalyticalScreeningResult,
    analytical_screening_result_id,
)
from investment_analyst.core.models import MetricResult


class AnalyticalScreeningError(RuntimeError):
    """Base error for invalid or ambiguous analytical evidence."""


class IncompatibleAnalyticalMetricError(AnalyticalScreeningError):
    """Raised when an in-scope metric violates the condition contract."""


class AmbiguousAnalyticalMetricError(AnalyticalScreeningError):
    """Raised when one condition has more than one compatible metric revision."""


class AnalyticalScreeningEngine:
    """Evaluate one rule over one caller-selected point-in-time metric snapshot."""

    def evaluate(self, request: AnalyticalScreeningRequest) -> AnalyticalScreeningResult:
        """Return a reproducible tri-valued result without I/O or persistence."""
        by_key: dict[str, tuple[MetricResult, ...]] = {}
        for condition in request.rule.conditions:
            by_key[condition.metric_key] = tuple(
                item for item in request.metrics if item.metric_key == condition.metric_key
            )
        condition_results = tuple(
            self._evaluate_condition(condition, by_key[condition.metric_key])
            for condition in request.rule.conditions
        )
        as_of = request.metrics[0].as_of if request.metrics else None
        matched = all(item.state is AnalyticalConditionState.MET for item in condition_results)
        retained = (
            None
            if any(
                item.retention_state is AnalyticalConditionState.NOT_EVALUABLE
                for item in condition_results
            )
            else all(
                item.retention_state is AnalyticalConditionState.MET for item in condition_results
            )
        )
        activated = matched and request.rule.state in {
            AnalyticalRuleState.SILENT,
            AnalyticalRuleState.ACTIVE,
        }
        explanation = self._explanation(condition_results, matched=matched)
        return AnalyticalScreeningResult(
            result_id=analytical_screening_result_id(
                request.rule,
                asset_id=request.asset_id,
                asset_class=request.asset_class,
                source_id=request.source_id,
                known_at=request.known_at,
                as_of=as_of,
                conditions=condition_results,
            ),
            rule=request.rule,
            asset_id=request.asset_id,
            asset_class=request.asset_class,
            source_id=request.source_id,
            known_at=request.known_at,
            as_of=as_of,
            computed_at=request.computed_at,
            conditions=condition_results,
            matched=matched,
            retained=retained,
            activated=activated,
            explanation_es=explanation,
        )

    def _evaluate_condition(
        self,
        condition: AnalyticalScreeningCondition,
        metrics: tuple[MetricResult, ...],
    ) -> AnalyticalConditionResult:
        candidates: list[MetricResult] = []
        for metric in metrics:
            if metric.algorithm_version != condition.algorithm_version:
                raise IncompatibleAnalyticalMetricError(
                    f"{condition.condition_id}: metric algorithm version is incompatible"
                )
            if metric.unit != condition.unit:
                raise IncompatibleAnalyticalMetricError(
                    f"{condition.condition_id}: metric unit is incompatible"
                )
            if not all(
                metric.parameters.get(name) == expected
                for name, expected in condition.parameter_filters.items()
            ):
                continue
            if metric.quality not in condition.accepted_qualities:
                continue
            candidates.append(metric)
        if not candidates:
            return AnalyticalConditionResult(
                condition_id=condition.condition_id,
                state=AnalyticalConditionState.NOT_EVALUABLE,
                metric_key=condition.metric_key,
                operator=condition.operator,
                threshold=condition.threshold,
                retention_threshold=condition.retention_threshold,
                unit=condition.unit,
                retention_state=AnalyticalConditionState.NOT_EVALUABLE,
                explanation_es=(
                    f"{condition.label_es}: no existe evidencia compatible en el período evaluado."
                ),
            )
        if len(candidates) > 1:
            raise AmbiguousAnalyticalMetricError(
                f"{condition.condition_id}: multiple compatible metric revisions exist"
            )
        metric = candidates[0]
        state = (
            AnalyticalConditionState.MET
            if condition.operator.compare(metric.value, condition.threshold)
            else AnalyticalConditionState.NOT_MET
        )
        retention_state = (
            AnalyticalConditionState.MET
            if condition.operator.compare(metric.value, condition.retention_threshold)
            else AnalyticalConditionState.NOT_MET
        )
        relationship = "cumple" if state is AnalyticalConditionState.MET else "no cumple"
        return AnalyticalConditionResult(
            condition_id=condition.condition_id,
            state=state,
            metric_key=condition.metric_key,
            operator=condition.operator,
            threshold=condition.threshold,
            retention_threshold=condition.retention_threshold,
            unit=condition.unit,
            retention_state=retention_state,
            observed_value=metric.value,
            metric_result_id=metric.result_id,
            as_of=metric.as_of,
            available_at=metric.available_at,
            quality=metric.quality,
            explanation_es=f"{condition.label_es}: {relationship} la condición configurada.",
        )

    @staticmethod
    def _explanation(
        conditions: tuple[AnalyticalConditionResult, ...],
        *,
        matched: bool,
    ) -> str:
        not_evaluable = sum(
            item.state is AnalyticalConditionState.NOT_EVALUABLE for item in conditions
        )
        if not_evaluable:
            return (
                f"Evaluación incompleta: {not_evaluable} condición(es) no tienen evidencia "
                "compatible en el corte."
            )
        if matched:
            return (
                "La evidencia cumple todas las condiciones configuradas para revisión; "
                "no constituye una recomendación."
            )
        return (
            "La evidencia no cumple todas las condiciones configuradas; no se activa un candidato."
        )


__all__ = [
    "AmbiguousAnalyticalMetricError",
    "AnalyticalScreeningEngine",
    "AnalyticalScreeningError",
    "IncompatibleAnalyticalMetricError",
]
