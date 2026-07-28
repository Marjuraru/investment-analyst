"""Versioned, evidence-first company classification inspired by Peter Lynch."""

from decimal import Decimal

from investment_analyst.analytics.fundamentals.analysis_models import (
    COMPANY_CLASSIFICATION_ALGORITHM_VERSION,
    CompanyCategoryAssessment,
    CompanyClassificationEvidence,
    CompanyClassificationView,
)
from investment_analyst.analytics.fundamentals.research_history_models import (
    HISTORY_ALGORITHM_VERSION,
    AaplFundamentalResearchHistoryResult,
    FundamentalResearchMetricHistory,
)
from investment_analyst.core.models import DataFrequency

_EPS_KEY = "fundamental.research.diluted_eps"
_REVENUE_PER_SHARE_KEY = "fundamental.research.revenue_per_diluted_share"
_GROWTH_KEYS = (_EPS_KEY, _REVENUE_PER_SHARE_KEY)
_MINIMUM_POINTS = 4
_SLOW_MAX = Decimal("0.10")
_STALWART_MAX = Decimal("0.20")
_STALWART_REVENUE_MIN = Decimal("0.05")
_FAST_REVENUE_MIN = Decimal("0.10")
_GROWTH_CRITERIA = {
    "slow_grower": (
        "CAGR anual de EPS diluido y de ingresos por acción entre 0 % y menos de 10 %."
    ),
    "stalwart": (
        "CAGR anual de EPS diluido entre 10 % y menos de 20 %, con ingresos por acción "
        "entre 5 % y menos de 20 %."
    ),
    "fast_grower": (
        "CAGR anual de EPS diluido de al menos 20 % e ingresos por acción de al menos 10 %."
    ),
}
_EXTERNAL_REQUIREMENTS = {
    "cyclical": ("Serie macroeconómica o sectorial versionada para medir sensibilidad al ciclo.",),
    "turnaround": (
        "Evidencia versionada de reestructuración, continuidad y obligaciones relevantes.",
    ),
    "asset_play": (
        "Tasación identificable y fechada de activos no reflejados por los resultados.",
    ),
}


def classify_company(
    history: AaplFundamentalResearchHistoryResult,
) -> CompanyClassificationView:
    """Classify a quantitative growth profile or disclose exactly what is missing."""
    if history.request.frequency is not DataFrequency.ANNUAL:
        raise ValueError("company classification requires annual fundamental history")
    series = {item.metric_key: item for item in history.series}
    usable = {
        key: item
        for key in _GROWTH_KEYS
        if (item := series.get(key)) is not None and _usable_growth_series(item)
    }
    evidence = tuple(_evidence(usable[key]) for key in sorted(usable))
    if len(usable) != len(_GROWTH_KEYS):
        missing = tuple(
            _missing_growth_requirement(key, series.get(key))
            for key in _GROWTH_KEYS
            if key not in usable
        )
        return CompanyClassificationView(
            evaluated_known_at=history.request.known_at,
            evidence=evidence,
            assessments=(
                *(
                    CompanyCategoryAssessment(
                        category_key=category,
                        status="insufficient_evidence",
                        criterion_es=_GROWTH_CRITERIA[category],
                        explanation_es="No hay un horizonte anual comparable suficiente.",
                        evidence_metric_keys=tuple(sorted(usable)),
                        missing_requirements=missing,
                    )
                    for category in ("slow_grower", "stalwart", "fast_grower")
                ),
                *_external_assessments(),
            ),
            missing_requirements=missing
            + tuple(
                requirement
                for requirements in _EXTERNAL_REQUIREMENTS.values()
                for requirement in requirements
            ),
            explanation_es=(
                "La regla versionada no asigna una categoría porque faltan series anuales "
                "comparables de EPS diluido e ingresos por acción."
            ),
        )

    eps_cagr = usable[_EPS_KEY].statistics.compound_annual_growth_rate
    revenue_cagr = usable[_REVENUE_PER_SHARE_KEY].statistics.compound_annual_growth_rate
    if eps_cagr is None or revenue_cagr is None:
        raise ValueError("usable classification series must expose exact annual CAGR")
    matches = {
        "slow_grower": (
            Decimal("0") <= eps_cagr < _SLOW_MAX and Decimal("0") <= revenue_cagr < _SLOW_MAX
        ),
        "stalwart": (
            _SLOW_MAX <= eps_cagr < _STALWART_MAX
            and _STALWART_REVENUE_MIN <= revenue_cagr < _STALWART_MAX
        ),
        "fast_grower": (eps_cagr >= _STALWART_MAX and revenue_cagr >= _FAST_REVENUE_MIN),
    }
    matched = tuple(category for category, condition in matches.items() if condition)
    assessments = (
        tuple(
            CompanyCategoryAssessment(
                category_key=category,
                status="matched" if matches[category] else "not_matched",
                criterion_es=_GROWTH_CRITERIA[category],
                explanation_es=(
                    "Los dos CAGR anuales cumplen el rango publicado."
                    if matches[category]
                    else "Los CAGR anuales no cumplen conjuntamente este rango."
                ),
                evidence_metric_keys=tuple(sorted(_GROWTH_KEYS)),
            )
            for category in ("slow_grower", "stalwart", "fast_grower")
        )
        + _external_assessments()
    )
    if len(matched) == 1:
        selected = matched[0]
        return CompanyClassificationView(
            status="classified",
            selected_category=selected,
            evaluated_known_at=history.request.known_at,
            evidence=evidence,
            assessments=assessments,
            missing_requirements=tuple(
                requirement
                for requirements in _EXTERNAL_REQUIREMENTS.values()
                for requirement in requirements
            ),
            explanation_es=(
                "Clasificación cuantitativa basada en CAGR anual de EPS diluido e ingresos "
                "por acción. Las categorías cíclica, recuperación y activo oculto requieren "
                "evidencia adicional y no se infieren por aproximación."
            ),
        )
    return CompanyClassificationView(
        evaluated_known_at=history.request.known_at,
        evidence=evidence,
        assessments=assessments,
        missing_requirements=(
            "Los CAGR de EPS diluido e ingresos por acción no coinciden en un rango "
            "cuantitativo de la regla v1.",
            *(
                requirement
                for requirements in _EXTERNAL_REQUIREMENTS.values()
                for requirement in requirements
            ),
        ),
        explanation_es=(
            "La evidencia anual es trazable, pero los dos indicadores no coinciden en una "
            "categoría cuantitativa única de la regla versionada."
        ),
    )


def _usable_growth_series(series: FundamentalResearchMetricHistory) -> bool:
    statistics = series.statistics
    return (
        series.frequency is DataFrequency.ANNUAL
        and len(series.points) >= _MINIMUM_POINTS
        and len(series.points) <= 5
        and statistics.elapsed_days >= 365 * 3
        and statistics.first_value > 0
        and statistics.latest_value > 0
        and statistics.compound_annual_growth_rate is not None
    )


def _evidence(series: FundamentalResearchMetricHistory) -> CompanyClassificationEvidence:
    observation_ids = tuple(
        sorted(
            {
                observation_id
                for point in series.points
                for observation_id in point.input_observation_ids
            },
            key=str,
        )
    )
    return CompanyClassificationEvidence(
        metric_key=series.metric_key,
        point_count=len(series.points),
        first_period_end=series.points[0].period_end,
        latest_period_end=series.points[-1].period_end,
        first_value=series.points[0].value,
        latest_value=series.points[-1].value,
        compound_annual_growth_rate=series.statistics.compound_annual_growth_rate,
        input_observation_ids=observation_ids,
        metric_algorithm_versions=tuple(
            sorted({point.metric_algorithm_version for point in series.points})
        ),
        history_algorithm_version=HISTORY_ALGORITHM_VERSION,
    )


def _missing_growth_requirement(
    metric_key: str,
    series: FundamentalResearchMetricHistory | None,
) -> str:
    label = "EPS diluido" if metric_key == _EPS_KEY else "ingresos por acción"
    if series is None:
        return f"Serie anual de {label} disponible al corte."
    return (
        f"Al menos {_MINIMUM_POINTS} puntos anuales positivos y tres años de horizonte "
        f"comparable para {label}."
    )


def _external_assessments() -> tuple[CompanyCategoryAssessment, ...]:
    criteria = {
        "cyclical": "Sensibilidad material y repetida de resultados al ciclo económico.",
        "turnaround": "Deterioro previo seguido de recuperación con continuidad verificable.",
        "asset_play": "Valor identificable de activos no reflejado por los resultados corrientes.",
    }
    return tuple(
        CompanyCategoryAssessment(
            category_key=category,
            status="insufficient_evidence",
            criterion_es=criteria[category],
            explanation_es="La fuente SEC cuantitativa actual no prueba por sí sola este criterio.",
            missing_requirements=_EXTERNAL_REQUIREMENTS[category],
        )
        for category in ("cyclical", "turnaround", "asset_play")
    )


__all__ = [
    "COMPANY_CLASSIFICATION_ALGORITHM_VERSION",
    "classify_company",
]
