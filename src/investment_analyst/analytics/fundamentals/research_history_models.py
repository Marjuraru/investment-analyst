"""Strict contracts for historical analysis of fundamental research metrics."""

from decimal import Context, Decimal, localcontext
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.fundamentals.research_models import (
    FUNDAMENTAL_RESEARCH_METRIC_COUNT,
    AaplFundamentalResearchRequest,
    AaplFundamentalResearchResult,
    FinancialDecimal,
    get_fundamental_research_metric_definition,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
)

HISTORY_ALGORITHM_VERSION = "fundamental-research-history-v2-decimal34"
_DAYS_PER_YEAR = Decimal("365.2425")
_LEVEL_UNITS = frozenset({"USD", "shares", "USD/shares"})


class FundamentalResearchHistoryFormula(ContractModel):
    """Published formula for one historical statistic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statistic_key: NonEmptyStr
    display_name_es: NonEmptyStr
    formula: NonEmptyStr
    availability_rule: NonEmptyStr
    algorithm_version: Literal["fundamental-research-history-v2-decimal34"] = (
        HISTORY_ALGORITHM_VERSION
    )


FUNDAMENTAL_RESEARCH_HISTORY_FORMULAS = (
    FundamentalResearchHistoryFormula(
        statistic_key="arithmetic_mean",
        display_name_es="Media aritmética",
        formula="sum(values) / point_count",
        availability_rule="Se calcula con todos los puntos disponibles del horizonte.",
    ),
    FundamentalResearchHistoryFormula(
        statistic_key="compound_annual_growth_rate",
        display_name_es="CAGR",
        formula="(latest / earliest) ** (365.2425 / elapsed_days) - 1",
        availability_rule=(
            "Solo para frecuencia anual, unidades de nivel, extremos positivos y al menos "
            "dos puntos."
        ),
    ),
    FundamentalResearchHistoryFormula(
        statistic_key="horizon_change",
        display_name_es="Cambio del horizonte",
        formula="latest - earliest",
        availability_rule="Requiere al menos dos puntos disponibles.",
    ),
    FundamentalResearchHistoryFormula(
        statistic_key="horizon_change_rate",
        display_name_es="Variación del horizonte",
        formula="latest / earliest - 1",
        availability_rule="Solo para unidades de nivel y un valor inicial positivo.",
    ),
    FundamentalResearchHistoryFormula(
        statistic_key="latest_change_from_previous_available",
        display_name_es="Cambio frente al período disponible anterior",
        formula="latest - previous_available",
        availability_rule="Requiere al menos dos puntos disponibles.",
    ),
    FundamentalResearchHistoryFormula(
        statistic_key="latest_change_rate_from_previous_available",
        display_name_es="Variación frente al período disponible anterior",
        formula="latest / previous_available - 1",
        availability_rule="Solo para unidades de nivel y un valor anterior positivo.",
    ),
    FundamentalResearchHistoryFormula(
        statistic_key="range",
        display_name_es="Rango observado",
        formula="maximum - minimum",
        availability_rule="Se calcula con todos los puntos disponibles del horizonte.",
    ),
)

FUNDAMENTAL_RESEARCH_HISTORY_LIMITATIONS = (
    "Las comparaciones usan períodos disponibles; no imputan períodos ausentes.",
    "El CAGR usa días transcurridos y 365.2425 días por año; no se calcula para ratios.",
    "La media y el rango describen dispersión, pero no forman una puntuación de estabilidad.",
    "No se ajustan inflación, divisas, cambios contables, estacionalidad ni eventos corporativos.",
)


class FundamentalResearchHistoryPoint(ContractModel):
    """One exact calculated metric retained as historical evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_end: UTCDateTime
    value: FinancialDecimal
    available_at: UTCDateTime
    metric_algorithm_version: NonEmptyStr
    input_observation_ids: tuple[UUID, ...]

    @model_validator(mode="after")
    def validate_point(self) -> "FundamentalResearchHistoryPoint":
        """Require finite values and unique non-empty metric evidence."""
        if not self.value.is_finite():
            raise ValueError("fundamental history point value must be finite")
        if not self.input_observation_ids:
            raise ValueError("fundamental history points require input observations")
        if len(self.input_observation_ids) != len(set(self.input_observation_ids)):
            raise ValueError("fundamental history input observations must be unique")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON-compatible point evidence."""
        return {
            "period_end": self.period_end.isoformat(),
            "value": str(self.value),
            "available_at": self.available_at.isoformat(),
            "metric_algorithm_version": self.metric_algorithm_version,
            "input_observation_ids": [str(item) for item in self.input_observation_ids],
        }


class FundamentalResearchHistoryStatistics(ContractModel):
    """Transparent descriptive statistics for one metric series."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    point_count: int = Field(ge=1, le=100)
    first_period_end: UTCDateTime
    previous_period_end: UTCDateTime | None = None
    latest_period_end: UTCDateTime
    elapsed_days: int = Field(ge=0)
    first_value: FinancialDecimal
    latest_value: FinancialDecimal
    minimum: FinancialDecimal
    maximum: FinancialDecimal
    arithmetic_mean: FinancialDecimal
    range: FinancialDecimal
    latest_change_from_previous_available: FinancialDecimal | None = None
    latest_change_rate_from_previous_available: FinancialDecimal | None = None
    horizon_change: FinancialDecimal | None = None
    horizon_change_rate: FinancialDecimal | None = None
    compound_annual_growth_rate: FinancialDecimal | None = None
    algorithm_version: Literal["fundamental-research-history-v2-decimal34"] = (
        HISTORY_ALGORITHM_VERSION
    )

    @model_validator(mode="after")
    def validate_statistics(self) -> "FundamentalResearchHistoryStatistics":
        """Keep bounds, optional comparisons, and Decimal values consistent."""
        decimal_values = (
            self.first_value,
            self.latest_value,
            self.minimum,
            self.maximum,
            self.arithmetic_mean,
            self.range,
            self.latest_change_from_previous_available,
            self.latest_change_rate_from_previous_available,
            self.horizon_change,
            self.horizon_change_rate,
            self.compound_annual_growth_rate,
        )
        if any(value is not None and not value.is_finite() for value in decimal_values):
            raise ValueError("fundamental history statistics must be finite")
        if self.first_period_end > self.latest_period_end:
            raise ValueError("fundamental history period bounds are reversed")
        with localcontext(Context(prec=34)):
            expected_range = self.maximum - self.minimum
        if self.minimum > self.maximum or self.range != expected_range:
            raise ValueError("fundamental history range is inconsistent")
        if not self.minimum <= self.arithmetic_mean <= self.maximum:
            raise ValueError("fundamental history mean is outside its observed bounds")
        comparison_values = (
            self.latest_change_from_previous_available,
            self.latest_change_rate_from_previous_available,
            self.horizon_change,
            self.horizon_change_rate,
            self.compound_annual_growth_rate,
        )
        if self.point_count == 1:
            if self.previous_period_end is not None or any(
                value is not None for value in comparison_values
            ):
                raise ValueError("single-point history cannot define comparisons")
            if self.first_period_end != self.latest_period_end or self.elapsed_days != 0:
                raise ValueError("single-point history must have a zero-length horizon")
        else:
            if self.previous_period_end is None:
                raise ValueError("multi-point history requires a previous period")
            if self.latest_change_from_previous_available is None or self.horizon_change is None:
                raise ValueError("multi-point history requires absolute changes")
            if not self.first_period_end < self.latest_period_end or self.elapsed_days <= 0:
                raise ValueError("multi-point history requires a positive time horizon")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact Decimal strings for every historical statistic."""
        return {
            "point_count": self.point_count,
            "first_period_end": self.first_period_end.isoformat(),
            "previous_period_end": (
                self.previous_period_end.isoformat() if self.previous_period_end else None
            ),
            "latest_period_end": self.latest_period_end.isoformat(),
            "elapsed_days": self.elapsed_days,
            "first_value": str(self.first_value),
            "latest_value": str(self.latest_value),
            "minimum": str(self.minimum),
            "maximum": str(self.maximum),
            "arithmetic_mean": str(self.arithmetic_mean),
            "range": str(self.range),
            "latest_change_from_previous_available": _optional_decimal(
                self.latest_change_from_previous_available
            ),
            "latest_change_rate_from_previous_available": _optional_decimal(
                self.latest_change_rate_from_previous_available
            ),
            "horizon_change": _optional_decimal(self.horizon_change),
            "horizon_change_rate": _optional_decimal(self.horizon_change_rate),
            "compound_annual_growth_rate": _optional_decimal(self.compound_annual_growth_rate),
            "algorithm_version": self.algorithm_version,
        }


class FundamentalResearchMetricHistory(ContractModel):
    """Ordered history and statistics for one published research metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    display_name_es: NonEmptyStr
    unit: Literal["ratio", "USD", "shares", "USD/shares"]
    frequency: DataFrequency
    points: tuple[FundamentalResearchHistoryPoint, ...]
    statistics: FundamentalResearchHistoryStatistics

    @model_validator(mode="after")
    def validate_history(self) -> "FundamentalResearchMetricHistory":
        """Match the source definition, ordered points, and statistic boundaries."""
        definition = get_fundamental_research_metric_definition(self.metric_key)
        if self.display_name_es != definition.display_name_es or self.unit != definition.unit:
            raise ValueError("fundamental history does not match its metric definition")
        if self.frequency not in {DataFrequency.ANNUAL, DataFrequency.QUARTERLY}:
            raise ValueError("fundamental history frequency must be annual or quarterly")
        if not self.points:
            raise ValueError("fundamental metric history requires at least one point")
        period_ends = tuple(point.period_end for point in self.points)
        if period_ends != tuple(sorted(period_ends)) or len(period_ends) != len(set(period_ends)):
            raise ValueError("fundamental history points must be ordered and unique")
        values = tuple(point.value for point in self.points)
        statistics = self.statistics
        if statistics.point_count != len(self.points):
            raise ValueError("fundamental history point count is inconsistent")
        if (
            statistics.first_period_end != period_ends[0]
            or statistics.latest_period_end != period_ends[-1]
            or statistics.first_value != values[0]
            or statistics.latest_value != values[-1]
            or statistics.minimum != min(values)
            or statistics.maximum != max(values)
        ):
            raise ValueError("fundamental history statistic evidence is inconsistent")
        expected_previous = period_ends[-2] if len(period_ends) > 1 else None
        if statistics.previous_period_end != expected_previous:
            raise ValueError("fundamental history previous period is inconsistent")
        elapsed_days = (period_ends[-1].date() - period_ends[0].date()).days
        previous_value = values[-2] if len(values) > 1 else None
        latest_delta: Decimal | None = None
        horizon_delta: Decimal | None = None
        latest_rate: Decimal | None = None
        horizon_rate: Decimal | None = None
        cagr: Decimal | None = None
        with localcontext(Context(prec=34)):
            expected_mean = sum(values, Decimal(0)) / Decimal(len(values))
            expected_range = max(values) - min(values)
            if previous_value is not None:
                latest_delta = values[-1] - previous_value
                horizon_delta = values[-1] - values[0]
                if self.unit in _LEVEL_UNITS:
                    if previous_value > 0:
                        latest_rate = values[-1] / previous_value - 1
                    if values[0] > 0:
                        horizon_rate = values[-1] / values[0] - 1
                    if (
                        self.frequency is DataFrequency.ANNUAL
                        and values[-1] > 0
                        and values[0] > 0
                        and elapsed_days > 0
                    ):
                        cagr = (values[-1] / values[0]) ** (
                            _DAYS_PER_YEAR / Decimal(elapsed_days)
                        ) - 1
        if (
            statistics.elapsed_days != elapsed_days
            or statistics.arithmetic_mean != expected_mean
            or statistics.range != expected_range
            or statistics.latest_change_from_previous_available != latest_delta
            or statistics.latest_change_rate_from_previous_available != latest_rate
            or statistics.horizon_change != horizon_delta
            or statistics.horizon_change_rate != horizon_rate
            or statistics.compound_annual_growth_rate != cagr
        ):
            raise ValueError("fundamental history calculations are inconsistent")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact history and statistics."""
        return {
            "metric_key": self.metric_key,
            "display_name_es": self.display_name_es,
            "unit": self.unit,
            "frequency": self.frequency.value,
            "points": [point.to_json_dict() for point in self.points],
            "statistics": self.statistics.to_json_dict(),
        }


class AaplFundamentalResearchHistoryCoverage(ContractModel):
    """Bounded counts for one historical analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    series_returned: int = Field(ge=0, le=FUNDAMENTAL_RESEARCH_METRIC_COUNT)
    points_returned: int = Field(ge=0, le=FUNDAMENTAL_RESEARCH_METRIC_COUNT * 100)
    series_with_previous_comparison: int = Field(
        ge=0,
        le=FUNDAMENTAL_RESEARCH_METRIC_COUNT,
    )
    series_with_cagr: int = Field(ge=0, le=FUNDAMENTAL_RESEARCH_METRIC_COUNT)

    @model_validator(mode="after")
    def validate_counts(self) -> "AaplFundamentalResearchHistoryCoverage":
        """Keep optional-statistic counts bounded by returned series."""
        if (
            self.series_with_previous_comparison > self.series_returned
            or self.series_with_cagr > self.series_returned
        ):
            raise ValueError("historical statistic counts exceed returned series")
        return self


class AaplFundamentalResearchHistoryResult(ContractModel):
    """Versioned historical view over one exact research result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["aapl-fundamental-research-history-v2"] = (
        "aapl-fundamental-research-history-v2"
    )
    asset_id: Literal["equity:us:aapl"] = ASSET_ID
    source_id: Literal["sec-edgar:aapl:companyfacts"] = COMPANYFACTS_SOURCE_ID
    request: AaplFundamentalResearchRequest
    research: AaplFundamentalResearchResult
    formulas: tuple[FundamentalResearchHistoryFormula, ...] = FUNDAMENTAL_RESEARCH_HISTORY_FORMULAS
    series: tuple[FundamentalResearchMetricHistory, ...]
    coverage: AaplFundamentalResearchHistoryCoverage
    traceability_verified: Literal[True] = True
    limitations: tuple[NonEmptyStr, ...] = FUNDAMENTAL_RESEARCH_HISTORY_LIMITATIONS

    @model_validator(mode="after")
    def validate_result(self) -> "AaplFundamentalResearchHistoryResult":
        """Tie every historical point to the exact embedded research result."""
        if self.request != self.research.request:
            raise ValueError("historical request must match the embedded research request")
        if self.formulas != FUNDAMENTAL_RESEARCH_HISTORY_FORMULAS:
            raise ValueError("historical formulas must preserve the versioned contract")
        if self.limitations != FUNDAMENTAL_RESEARCH_HISTORY_LIMITATIONS:
            raise ValueError("historical limitations must preserve the versioned contract")
        keys = tuple(item.metric_key for item in self.series)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("fundamental history series must be ordered and unique")
        expected: dict[str, list[FundamentalResearchHistoryPoint]] = {}
        for period in self.research.periods:
            for metric in period.metrics:
                expected.setdefault(metric.metric_key, []).append(
                    FundamentalResearchHistoryPoint(
                        period_end=metric.period_end,
                        value=metric.value,
                        available_at=metric.available_at,
                        metric_algorithm_version=metric.algorithm_version,
                        input_observation_ids=tuple(item.observation_id for item in metric.inputs),
                    )
                )
        actual = {item.metric_key: list(item.points) for item in self.series}
        if actual != expected:
            raise ValueError("fundamental history does not match embedded research evidence")
        if self.coverage.series_returned != len(self.series):
            raise ValueError("historical series count does not match coverage")
        if self.coverage.points_returned != sum(len(item.points) for item in self.series):
            raise ValueError("historical point count does not match coverage")
        if self.coverage.series_with_previous_comparison != sum(
            item.statistics.latest_change_from_previous_available is not None
            for item in self.series
        ):
            raise ValueError("historical comparison count does not match coverage")
        if self.coverage.series_with_cagr != sum(
            item.statistics.compound_annual_growth_rate is not None for item in self.series
        ):
            raise ValueError("historical CAGR count does not match coverage")
        if any(
            point.available_at > self.request.known_at
            for series in self.series
            for point in series.points
        ):
            raise ValueError("fundamental history uses evidence unavailable at known_at")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact exact JSON-compatible historical contract."""
        return {
            "schema_version": self.schema_version,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "request": self.request.model_dump(mode="json"),
            "research": self.research.to_json_dict(),
            "formulas": [item.model_dump(mode="json") for item in self.formulas],
            "series": [item.to_json_dict() for item in self.series],
            "coverage": self.coverage.model_dump(mode="json"),
            "traceability_verified": self.traceability_verified,
            "limitations": list(self.limitations),
        }


def _optional_decimal(value: FinancialDecimal | None) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "AaplFundamentalResearchHistoryCoverage",
    "AaplFundamentalResearchHistoryResult",
    "FUNDAMENTAL_RESEARCH_HISTORY_FORMULAS",
    "FUNDAMENTAL_RESEARCH_HISTORY_LIMITATIONS",
    "FundamentalResearchHistoryFormula",
    "FundamentalResearchHistoryPoint",
    "FundamentalResearchHistoryStatistics",
    "FundamentalResearchMetricHistory",
    "HISTORY_ALGORITHM_VERSION",
]
