"""Provider-free, Decimal-only evaluation over selected valuation history."""

import json
from decimal import Context, Decimal, localcontext
from uuid import UUID, uuid5

from investment_analyst.analytics.valuation.history_models import (
    CorporateValuationHistoryPoint,
    CorporateValuationHistoryRequest,
)
from investment_analyst.analytics.valuation.history_service import CorporateValuationHistoryService
from investment_analyst.analytics.valuation.rule_models import (
    CorporateValuationHistoryRuleCoverage,
    CorporateValuationHistoryRuleEvaluation,
    CorporateValuationHistoryRuleRequest,
)

_DECIMAL34 = Context(prec=34)
_NAMESPACE = UUID("4f47c096-72df-4ec7-96c7-94ec6b0d16fd")


class CorporateValuationHistoryRuleError(RuntimeError):
    """Raised when eligible materialized series are semantically ambiguous."""


class CorporateValuationHistoryRuleService:
    """Evaluate one explicit rule without acquiring a provider or writer."""

    def __init__(self, history_service: CorporateValuationHistoryService) -> None:
        self._history_service = history_service

    def query(
        self, request: CorporateValuationHistoryRuleRequest
    ) -> CorporateValuationHistoryRuleEvaluation:
        history = self._history_service.query(
            CorporateValuationHistoryRequest(
                asset_id=request.asset_id,
                known_at=request.known_at,
                start_date=request.start_date,
                end_date=request.end_date,
                basis=request.basis,
                limit=500,
            )
        )
        compatible = tuple(
            item for item in history.series if item.metric_key == request.rule.metric_key
        )
        if len(compatible) > 1:
            raise CorporateValuationHistoryRuleError("valuation rule series is ambiguous")
        points = compatible[0].points if compatible else ()
        current = points[-1] if points else None
        reference = tuple(
            point
            for point in points
            if current is not None and point.valuation_date < current.valuation_date
        )
        coverage = CorporateValuationHistoryRuleCoverage(
            candidate_results=history.coverage.candidate_results,
            superseded_revisions=history.coverage.superseded_revisions,
            prior_points=len(reference),
            required_prior_points=request.rule.minimum_prior_points,
            reference_start_date=reference[0].valuation_date if reference else None,
            reference_end_date=reference[-1].valuation_date if reference else None,
        )
        lower = equal = greater = 0
        percentile: Decimal | None = None
        status = "not_evaluable"
        if current is not None and len(reference) >= request.rule.minimum_prior_points:
            lower = sum(point.value < current.value for point in reference)
            equal = sum(point.value == current.value for point in reference)
            greater = len(reference) - lower - equal
            with localcontext(_DECIMAL34):
                percentile = (Decimal(lower) + Decimal("0.5") * Decimal(equal)) / Decimal(
                    len(reference)
                )
            status = (
                "met"
                if (
                    percentile <= request.rule.threshold
                    if request.rule.operator == "at_or_below_empirical_percentile"
                    else percentile >= request.rule.threshold
                )
                else "not_met"
            )
        result_id = _evaluation_id(request, current, reference)
        return CorporateValuationHistoryRuleEvaluation(
            request=request,
            status=status,
            result_id=result_id,
            current_point=current,
            reference_points=reference,
            empirical_percentile=percentile,
            lower_count=lower,
            equal_count=equal,
            greater_count=greater,
            coverage=coverage,
            limitations=request.rule.limitations
            + (
                "Describe sólo resultados materializados anteriores; no es una señal, "
                "recomendación ni backtest.",
            ),
        )


def _evaluation_id(
    request: CorporateValuationHistoryRuleRequest,
    current: CorporateValuationHistoryPoint | None,
    reference: tuple[CorporateValuationHistoryPoint, ...],
) -> UUID:
    document = request.model_dump(mode="json") | {
        "current_result_id": str(current.result_id) if current is not None else None,
        "reference_result_ids": [str(point.result_id) for point in reference],
    }
    return uuid5(
        _NAMESPACE, json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
