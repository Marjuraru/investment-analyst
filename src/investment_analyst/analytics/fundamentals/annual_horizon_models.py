"""Strict contracts for explicit fixed annual horizons over research metrics."""

from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.fundamentals.research_history_models import (
    FundamentalResearchHistoryStatistics,
)
from investment_analyst.analytics.fundamentals.research_models import (
    FinancialDecimal,
    FundamentalResearchMetricDefinition,
    fundamental_research_metric_definitions,
    get_fundamental_research_metric_definition,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
)

ANNUAL_HORIZON_ALGORITHM_VERSION = "fundamental-fixed-annual-horizons-v1"
FUNDAMENTAL_ANNUAL_HORIZONS_REQUEST_SCHEMA_VERSION = "fundamental-annual-horizons-request-v1"
FUNDAMENTAL_ANNUAL_HORIZONS_RESULT_SCHEMA_VERSION = "fundamental-annual-horizons-result-v1"
ANNUAL_HORIZON_CHOICES = (3, 5, 10)
ANNUAL_HORIZON_MAX_POINTS = 11
ANNUAL_HORIZON_MIN_GAP_DAYS = 330
ANNUAL_HORIZON_MAX_GAP_DAYS = 400
ANNUAL_HORIZON_LEVEL_UNITS = frozenset({"USD", "shares", "USD/shares"})
ANNUAL_HORIZON_MAX_METRICS = 6

FUNDAMENTAL_ANNUAL_HORIZONS_LIMITATIONS = (
    "Los horizontes usan cierres fiscales anuales reales de 52/53 semanas; no equivalen a "
    "años calendario exactos.",
    "Un horizonte incompleto publica ausencia explícita y nunca estadísticas del tramo más corto.",
    "El CAGR usa días transcurridos y 365.2425 días por año; solo aplica a niveles USD, "
    "USD por acción o acciones con extremos positivos.",
    "Los ratios no publican CAGR ni variación relativa; los valores intermedios negativos no "
    "prohíben el CAGR si los extremos son positivos.",
    "La vista es descriptiva: no es estabilidad, rentabilidad de inversión, puntuación, "
    "recomendación ni señal.",
    "No se adquieren proveedores, no se escriben datos financieros y no se rellenan períodos "
    "ausentes.",
)

HorizonStatus = Literal[
    "complete",
    "anchor_unavailable",
    "insufficient_history",
    "nonconsecutive_periods",
]
CagrReason = Literal["calculated", "unit_not_eligible", "nonpositive_endpoint"]


class FundamentalAnnualHorizonsRequest(ContractModel):
    """Bounded annual-only request anchored on one explicit fiscal close."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["fundamental-annual-horizons-request-v1"] = (
        FUNDAMENTAL_ANNUAL_HORIZONS_REQUEST_SCHEMA_VERSION
    )
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    end_period_end: date
    metric_keys: tuple[NonEmptyStr, ...] = Field(
        min_length=1,
        max_length=ANNUAL_HORIZON_MAX_METRICS,
    )
    horizons: tuple[int, ...] = Field(
        default=ANNUAL_HORIZON_CHOICES,
        min_length=1,
        max_length=len(ANNUAL_HORIZON_CHOICES),
    )

    @field_validator("metric_keys")
    @classmethod
    def validate_metric_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require one to six known, unique, canonically ordered metric keys."""
        if not 1 <= len(value) <= ANNUAL_HORIZON_MAX_METRICS:
            raise ValueError("annual horizons require between 1 and 6 metric keys")
        if len(set(value)) != len(value):
            raise ValueError("annual horizon metric keys must be unique")
        for metric_key in value:
            get_fundamental_research_metric_definition(metric_key)
        return tuple(sorted(value))

    @field_validator("horizons")
    @classmethod
    def validate_horizons(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """Require a non-empty subset of the published horizon choices."""
        if isinstance(value, bool):
            raise ValueError("annual horizons must be integers")
        if len(set(value)) != len(value):
            raise ValueError("annual horizon choices must be unique")
        if any(isinstance(item, bool) or item not in ANNUAL_HORIZON_CHOICES for item in value):
            raise ValueError("annual horizons must be a non-empty subset of 3, 5 and 10")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_anchor(self) -> "FundamentalAnnualHorizonsRequest":
        """Reject an anchor later than the requested point-in-time cut."""
        anchor = datetime.combine(self.end_period_end, datetime.min.time(), tzinfo=UTC)
        if anchor > self.known_at:
            raise ValueError("annual horizon anchor must not be later than known_at")
        return self


class FundamentalAnnualHorizonPoint(ContractModel):
    """One exact calculated metric value retained as candidate evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_end: UTCDateTime
    value: FinancialDecimal
    unit: Literal["ratio", "USD", "shares", "USD/shares"]
    available_at: UTCDateTime
    metric_algorithm_version: NonEmptyStr
    input_observation_ids: tuple[UUID, ...]

    @model_validator(mode="after")
    def validate_point(self) -> "FundamentalAnnualHorizonPoint":
        """Require finite values and unique non-empty metric evidence."""
        if not self.value.is_finite():
            raise ValueError("annual horizon point value must be finite")
        if not self.input_observation_ids:
            raise ValueError("annual horizon points require input observations")
        if len(self.input_observation_ids) != len(set(self.input_observation_ids)):
            raise ValueError("annual horizon input observations must be unique")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON-compatible point evidence."""
        return {
            "period_end": self.period_end.isoformat(),
            "value": str(self.value),
            "unit": self.unit,
            "available_at": self.available_at.isoformat(),
            "metric_algorithm_version": self.metric_algorithm_version,
            "input_observation_ids": [str(item) for item in self.input_observation_ids],
        }


class FundamentalAnnualHorizonRow(ContractModel):
    """One explicit horizon over a contiguous tail of the candidate series."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    horizon_years: Literal[3, 5, 10]
    status: HorizonStatus
    required_points: int = Field(ge=4, le=11)
    observed_points: int = Field(ge=0, le=ANNUAL_HORIZON_MAX_POINTS)
    period_ends: tuple[UTCDateTime, ...] = Field(
        default=(),
        max_length=ANNUAL_HORIZON_MAX_POINTS,
    )
    elapsed_days: int | None = Field(default=None, ge=0)
    statistics: FundamentalResearchHistoryStatistics | None = None
    cagr_reason: CagrReason | None = None

    @model_validator(mode="after")
    def validate_row(self) -> "FundamentalAnnualHorizonRow":
        """Keep status, segment bounds, statistics and CAGR reasons consistent."""
        if self.required_points != self.horizon_years + 1:
            raise ValueError("annual horizon required points must equal horizon plus one")
        if self.observed_points != len(self.period_ends):
            raise ValueError("annual horizon observed count differs from its segment")
        period_ends = self.period_ends
        if period_ends != tuple(sorted(period_ends)) or len(period_ends) != len(set(period_ends)):
            raise ValueError("annual horizon segment must be ordered and unique")
        expected_elapsed: int | None
        if len(period_ends) >= 2:
            expected_elapsed = (period_ends[-1].date() - period_ends[0].date()).days
        else:
            expected_elapsed = 0 if period_ends else None
        if self.elapsed_days != expected_elapsed:
            raise ValueError("annual horizon elapsed days differ from its segment")

        if self.status == "complete":
            if self.observed_points != self.required_points:
                raise ValueError("complete annual horizon requires its exact point count")
            if self.statistics is None:
                raise ValueError("complete annual horizon requires statistics")
            statistics = self.statistics
            if statistics.point_count != self.required_points:
                raise ValueError("annual horizon statistics point count differs")
            if (
                statistics.first_period_end != period_ends[0]
                or statistics.latest_period_end != period_ends[-1]
                or statistics.elapsed_days != self.elapsed_days
            ):
                raise ValueError("annual horizon statistics bounds differ from its segment")
            if self.cagr_reason is None:
                raise ValueError("complete annual horizon requires a CAGR reason")
            calculated = statistics.compound_annual_growth_rate is not None
            if calculated != (self.cagr_reason == "calculated"):
                raise ValueError("annual horizon CAGR reason differs from statistics")
            return self

        if self.statistics is not None or self.cagr_reason is not None:
            raise ValueError("incomplete annual horizon cannot publish statistics or CAGR")
        if self.status == "insufficient_history":
            if self.observed_points >= self.required_points:
                raise ValueError("insufficient history requires fewer than required points")
        elif (
            self.status == "nonconsecutive_periods" and self.observed_points != self.required_points
        ):
            raise ValueError("nonconsecutive horizon requires its exact point count")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return one exact horizon row."""
        return {
            "horizon_years": self.horizon_years,
            "status": self.status,
            "required_points": self.required_points,
            "observed_points": self.observed_points,
            "period_ends": [item.isoformat() for item in self.period_ends],
            "elapsed_days": self.elapsed_days,
            "statistics": (self.statistics.to_json_dict() if self.statistics is not None else None),
            "cagr_reason": self.cagr_reason,
        }


class FundamentalAnnualHorizonSeries(ContractModel):
    """One requested metric with a single candidate series and its horizon rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: FundamentalResearchMetricDefinition
    points: tuple[FundamentalAnnualHorizonPoint, ...] = Field(
        default=(),
        max_length=ANNUAL_HORIZON_MAX_POINTS,
    )
    horizons: tuple[FundamentalAnnualHorizonRow, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_series(self) -> "FundamentalAnnualHorizonSeries":
        """Match the published definition, ordered points and referenced segments."""
        definition = self.definition
        if self.points:
            period_ends = tuple(point.period_end for point in self.points)
            if period_ends != tuple(sorted(period_ends)) or len(period_ends) != len(
                set(period_ends)
            ):
                raise ValueError("annual horizon points must be ordered and unique")
            if any(point.unit != definition.unit for point in self.points):
                raise ValueError("annual horizon point unit differs from its definition")
            if any(
                point.metric_algorithm_version != definition.algorithm_version
                for point in self.points
            ):
                raise ValueError("annual horizon point version differs from its definition")
        horizon_years = tuple(row.horizon_years for row in self.horizons)
        if horizon_years != tuple(sorted(horizon_years)) or len(horizon_years) != len(
            set(horizon_years)
        ):
            raise ValueError("annual horizon rows must be ordered and unique")
        if any(item not in ANNUAL_HORIZON_CHOICES for item in horizon_years):
            raise ValueError("annual horizon rows must use 3, 5 or 10")
        point_ends = tuple(point.period_end for point in self.points)
        for row in self.horizons:
            expected = point_ends[-row.observed_points :] if row.observed_points else ()
            if row.period_ends != expected:
                raise ValueError("annual horizon row does not reference the series tail")
            if row.status == "complete" and row.statistics is not None:
                segment = self.points[-row.observed_points :]
                statistics = row.statistics
                if (
                    statistics.first_value != segment[0].value
                    or statistics.latest_value != segment[-1].value
                ):
                    raise ValueError("annual horizon statistics values differ from points")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return one exact metric series."""
        return {
            "definition": self.definition.model_dump(mode="json"),
            "points": [point.to_json_dict() for point in self.points],
            "horizons": [row.to_json_dict() for row in self.horizons],
        }


class FundamentalAnnualHorizonsCoverage(ContractModel):
    """Bounded selection and output counts for one annual horizons query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observations_examined: int = Field(ge=0)
    observations_eligible: int = Field(ge=0)
    observations_selected: int = Field(ge=0)
    observations_superseded: int = Field(ge=0)
    source_periods: int = Field(ge=0)
    series_returned: int = Field(ge=1, le=ANNUAL_HORIZON_MAX_METRICS)
    points_returned: int = Field(ge=0, le=ANNUAL_HORIZON_MAX_METRICS * ANNUAL_HORIZON_MAX_POINTS)
    horizon_rows_returned: int = Field(ge=1, le=ANNUAL_HORIZON_MAX_METRICS * 3)
    complete_horizons: int = Field(ge=0)
    incomplete_horizons: int = Field(ge=0)
    skipped_counts: dict[NonEmptyStr, int]

    @model_validator(mode="after")
    def validate_counts(self) -> "FundamentalAnnualHorizonsCoverage":
        """Keep output counts and explicit absence counts internally consistent."""
        if any(value < 0 for value in self.skipped_counts.values()):
            raise ValueError("annual horizon skipped counts must be non-negative")
        if self.complete_horizons + self.incomplete_horizons != self.horizon_rows_returned:
            raise ValueError("annual horizon status counts differ from returned rows")
        if self.points_returned > self.series_returned * ANNUAL_HORIZON_MAX_POINTS:
            raise ValueError("annual horizon point count exceeds its series capacity")
        return self


class FundamentalAnnualHorizonsResult(ContractModel):
    """Versioned fixed-horizon view over explicit point-in-time annual closes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["fundamental-annual-horizons-result-v1"] = (
        FUNDAMENTAL_ANNUAL_HORIZONS_RESULT_SCHEMA_VERSION
    )
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    request: FundamentalAnnualHorizonsRequest
    algorithm_version: Literal["fundamental-fixed-annual-horizons-v1"] = (
        ANNUAL_HORIZON_ALGORITHM_VERSION
    )
    series: tuple[FundamentalAnnualHorizonSeries, ...] = Field(
        min_length=1,
        max_length=ANNUAL_HORIZON_MAX_METRICS,
    )
    coverage: FundamentalAnnualHorizonsCoverage
    traceability_verified: Literal[True] = True
    limitations: tuple[NonEmptyStr, ...] = FUNDAMENTAL_ANNUAL_HORIZONS_LIMITATIONS

    @model_validator(mode="after")
    def validate_result(self) -> "FundamentalAnnualHorizonsResult":
        """Bind every series and point to the exact request and published catalog."""
        if self.asset_id != self.request.asset_id:
            raise ValueError("annual horizon identity must match its request")
        if self.asset_id == ASSET_ID:
            if self.source_id != COMPANYFACTS_SOURCE_ID:
                raise ValueError("Apple annual horizons must preserve its historical source")
        elif self.source_id == COMPANYFACTS_SOURCE_ID:
            raise ValueError("generic annual horizons must not reuse Apple's source")
        if self.limitations != FUNDAMENTAL_ANNUAL_HORIZONS_LIMITATIONS:
            raise ValueError("annual horizon limitations must preserve the versioned contract")
        definitions = {
            item.metric_key: item for item in fundamental_research_metric_definitions(self.asset_id)
        }
        keys = tuple(series.definition.metric_key for series in self.series)
        if keys != self.request.metric_keys:
            raise ValueError("annual horizon series must match the requested metrics")
        if len(keys) != len(set(keys)):
            raise ValueError("annual horizon series must be unique")
        anchor = datetime.combine(
            self.request.end_period_end,
            datetime.min.time(),
            tzinfo=UTC,
        )
        for series in self.series:
            definition = definitions.get(series.definition.metric_key)
            if definition is None or definition != series.definition:
                raise ValueError("annual horizon definition does not match its asset catalog")
            horizon_years = tuple(row.horizon_years for row in series.horizons)
            if horizon_years != self.request.horizons:
                raise ValueError("annual horizon rows must match the requested horizons")
            for point in series.points:
                if point.available_at > self.request.known_at:
                    raise ValueError("annual horizons use evidence unavailable at known_at")
                if point.period_end > anchor:
                    raise ValueError("annual horizon points cannot exceed the requested anchor")
        if self.coverage.series_returned != len(self.series):
            raise ValueError("annual horizon series count differs from coverage")
        if self.coverage.points_returned != sum(len(item.points) for item in self.series):
            raise ValueError("annual horizon point count differs from coverage")
        row_count = sum(len(item.horizons) for item in self.series)
        if self.coverage.horizon_rows_returned != row_count:
            raise ValueError("annual horizon row count differs from coverage")
        complete = sum(row.status == "complete" for item in self.series for row in item.horizons)
        if self.coverage.complete_horizons != complete:
            raise ValueError("annual horizon complete count differs from coverage")
        if self.coverage.incomplete_horizons != row_count - complete:
            raise ValueError("annual horizon incomplete count differs from coverage")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return one exact JSON-compatible annual horizons contract."""
        return {
            "schema_version": self.schema_version,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "request": self.request.model_dump(mode="json"),
            "algorithm_version": self.algorithm_version,
            "series": [item.to_json_dict() for item in self.series],
            "coverage": self.coverage.model_dump(mode="json"),
            "traceability_verified": self.traceability_verified,
            "limitations": list(self.limitations),
        }


__all__ = [
    "ANNUAL_HORIZON_ALGORITHM_VERSION",
    "ANNUAL_HORIZON_CHOICES",
    "ANNUAL_HORIZON_LEVEL_UNITS",
    "ANNUAL_HORIZON_MAX_GAP_DAYS",
    "ANNUAL_HORIZON_MAX_METRICS",
    "ANNUAL_HORIZON_MAX_POINTS",
    "ANNUAL_HORIZON_MIN_GAP_DAYS",
    "CagrReason",
    "FUNDAMENTAL_ANNUAL_HORIZONS_LIMITATIONS",
    "FUNDAMENTAL_ANNUAL_HORIZONS_REQUEST_SCHEMA_VERSION",
    "FUNDAMENTAL_ANNUAL_HORIZONS_RESULT_SCHEMA_VERSION",
    "FundamentalAnnualHorizonPoint",
    "FundamentalAnnualHorizonRow",
    "FundamentalAnnualHorizonSeries",
    "FundamentalAnnualHorizonsCoverage",
    "FundamentalAnnualHorizonsRequest",
    "FundamentalAnnualHorizonsResult",
    "HorizonStatus",
]
