"""Deterministic fixed annual horizons over point-in-time research metrics."""

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Context, localcontext
from typing import Protocol

from investment_analyst.analytics.fundamentals.annual_horizon_models import (
    ANNUAL_HORIZON_LEVEL_UNITS,
    ANNUAL_HORIZON_MAX_GAP_DAYS,
    ANNUAL_HORIZON_MAX_POINTS,
    ANNUAL_HORIZON_MIN_GAP_DAYS,
    FUNDAMENTAL_ANNUAL_HORIZONS_RESULT_SCHEMA_VERSION,
    FundamentalAnnualHorizonPoint,
    FundamentalAnnualHorizonRow,
    FundamentalAnnualHorizonsCoverage,
    FundamentalAnnualHorizonSeries,
    FundamentalAnnualHorizonsRequest,
    FundamentalAnnualHorizonsResult,
)
from investment_analyst.analytics.fundamentals.research_history_models import (
    FundamentalResearchHistoryPoint,
)
from investment_analyst.analytics.fundamentals.research_history_service import (
    fundamental_research_history_statistics,
)
from investment_analyst.analytics.fundamentals.research_models import (
    FundamentalResearchMetricDefinition,
    FundamentalResearchMetricValue,
    fundamental_research_metric_definitions,
)
from investment_analyst.analytics.fundamentals.research_service import (
    SecIssuerFundamentalResearchService,
    calculate_fundamental_research_metric,
)
from investment_analyst.core.models import DataFrequency, NormalizedObservation
from investment_analyst.providers.asset_config import SecAssetConfiguration

_ANNUAL_HORIZON_WINDOW_DAYS = 4000


class _AnnualHorizonStorage(Protocol):
    def require_open(self) -> None:
        """Reject operations on a closed storage facade."""
        ...


class FundamentalAnnualHorizonError(RuntimeError):
    """Raised when explicit annual horizons cannot be produced deterministically."""


class SecIssuerFundamentalAnnualHorizonService:
    """Compute fixed annual horizons from exact selected SEC evidence without writes."""

    def __init__(
        self,
        storage: _AnnualHorizonStorage,
        configuration: SecAssetConfiguration | None = None,
    ) -> None:
        self._research = SecIssuerFundamentalResearchService(storage, configuration)
        self._definitions = {
            definition.metric_key: definition
            for definition in fundamental_research_metric_definitions(
                self._research.configuration.asset_id
            )
        }

    @property
    def configuration(self) -> SecAssetConfiguration:
        """Return the immutable SEC issuer configuration used by this service."""
        return self._research.configuration

    def query(
        self,
        request: FundamentalAnnualHorizonsRequest,
    ) -> FundamentalAnnualHorizonsResult:
        """Return explicit 3/5/10 annual horizons ending exactly at the requested anchor."""
        definitions = tuple(self._definition(metric_key) for metric_key in request.metric_keys)
        field_names = frozenset(
            field.field_name for definition in definitions for field in definition.input_fields
        )
        window_start = request.end_period_end - timedelta(days=_ANNUAL_HORIZON_WINDOW_DAYS)
        evidence = self._research.select_annual_evidence(
            known_at=request.known_at,
            start_period_end=window_start,
            end_period_end=request.end_period_end,
            field_names=field_names,
        )

        grouped: dict[date, dict[str, NormalizedObservation]] = defaultdict(dict)
        for (field_name, period_end), observation in evidence.selected.items():
            grouped[period_end][field_name] = observation

        skipped: Counter[str] = Counter()
        series: list[FundamentalAnnualHorizonSeries] = []
        points_returned = 0
        complete_horizons = 0
        incomplete_horizons = 0
        with localcontext(Context(prec=34)):
            for definition in definitions:
                values = []
                for period_end in sorted(grouped):
                    metric, reason = calculate_fundamental_research_metric(
                        definition,
                        grouped[period_end],
                        DataFrequency.ANNUAL,
                    )
                    if metric is None:
                        skipped[f"{definition.metric_key}:{reason}"] += 1
                        continue
                    values.append(metric)
                points = tuple(_point(metric) for metric in values)[-ANNUAL_HORIZON_MAX_POINTS:]
                rows = tuple(
                    _annual_horizon_row(
                        horizon,
                        points,
                        request.end_period_end,
                        definition.unit,
                    )
                    for horizon in request.horizons
                )
                complete_horizons += sum(row.status == "complete" for row in rows)
                incomplete_horizons += sum(row.status != "complete" for row in rows)
                points_returned += len(points)
                series.append(
                    FundamentalAnnualHorizonSeries(
                        definition=definition,
                        points=points,
                        horizons=rows,
                    )
                )

        return FundamentalAnnualHorizonsResult(
            schema_version=FUNDAMENTAL_ANNUAL_HORIZONS_RESULT_SCHEMA_VERSION,
            asset_id=self._research.configuration.asset_id,
            source_id=self._research.configuration.companyfacts_source_id,
            request=request,
            series=tuple(series),
            coverage=FundamentalAnnualHorizonsCoverage(
                observations_examined=evidence.observations_examined,
                observations_eligible=evidence.observations_eligible,
                observations_selected=evidence.selected_count,
                observations_superseded=evidence.superseded_count,
                source_periods=len(grouped),
                series_returned=len(series),
                points_returned=points_returned,
                horizon_rows_returned=complete_horizons + incomplete_horizons,
                complete_horizons=complete_horizons,
                incomplete_horizons=incomplete_horizons,
                skipped_counts=dict(sorted(skipped.items())),
            ),
        )

    def _definition(self, metric_key: str) -> FundamentalResearchMetricDefinition:
        try:
            return self._definitions[metric_key]
        except KeyError as error:
            raise FundamentalAnnualHorizonError(
                f"annual horizon metric is not published for this issuer: {metric_key}"
            ) from error


def annual_horizon_window_start(anchor: date) -> date:
    """Return the inclusive earliest close admissible for the maximum horizon."""
    return anchor - timedelta(days=_ANNUAL_HORIZON_WINDOW_DAYS)


def _point(metric: FundamentalResearchMetricValue) -> FundamentalAnnualHorizonPoint:
    return FundamentalAnnualHorizonPoint(
        period_end=metric.period_end,
        value=metric.value,
        unit=metric.unit,
        available_at=metric.available_at,
        metric_algorithm_version=metric.algorithm_version,
        input_observation_ids=tuple(item.observation_id for item in metric.inputs),
    )


def _annual_horizon_row(
    horizon_years: int,
    points: tuple[FundamentalAnnualHorizonPoint, ...],
    anchor: date,
    unit: str,
) -> FundamentalAnnualHorizonRow:
    anchor_at = datetime.combine(anchor, datetime.min.time(), tzinfo=UTC)
    pool = tuple(point for point in points if point.period_end <= anchor_at)
    anchor_present = any(point.period_end == anchor_at for point in pool)
    if not anchor_present:
        status = "anchor_unavailable"
        segment = pool
    elif len(pool) < horizon_years + 1:
        status = "insufficient_history"
        segment = pool
    else:
        segment = pool[-(horizon_years + 1) :]
        gaps = tuple(
            (later.period_end.date() - earlier.period_end.date()).days
            for earlier, later in zip(segment, segment[1:], strict=False)
        )
        if any(
            gap < ANNUAL_HORIZON_MIN_GAP_DAYS or gap > ANNUAL_HORIZON_MAX_GAP_DAYS for gap in gaps
        ):
            status = "nonconsecutive_periods"
        else:
            status = "complete"

    statistics = None
    cagr_reason = None
    if status == "complete":
        history_points = tuple(
            FundamentalResearchHistoryPoint(
                period_end=point.period_end,
                value=point.value,
                available_at=point.available_at,
                metric_algorithm_version=point.metric_algorithm_version,
                input_observation_ids=point.input_observation_ids,
            )
            for point in segment
        )
        statistics = fundamental_research_history_statistics(
            history_points,
            unit=unit,
            frequency=DataFrequency.ANNUAL,
        )
        if unit not in ANNUAL_HORIZON_LEVEL_UNITS:
            cagr_reason = "unit_not_eligible"
        elif segment[0].value <= 0 or segment[-1].value <= 0:
            cagr_reason = "nonpositive_endpoint"
        else:
            cagr_reason = "calculated"
        if (statistics.compound_annual_growth_rate is not None) != (cagr_reason == "calculated"):
            raise FundamentalAnnualHorizonError(
                "annual horizon CAGR could not be derived deterministically"
            )

    elapsed_days = (
        (segment[-1].period_end.date() - segment[0].period_end.date()).days
        if len(segment) >= 2
        else (0 if segment else None)
    )
    return FundamentalAnnualHorizonRow(
        horizon_years=horizon_years,
        status=status,
        required_points=horizon_years + 1,
        observed_points=len(segment),
        period_ends=tuple(point.period_end for point in segment),
        elapsed_days=elapsed_days,
        statistics=statistics,
        cagr_reason=cagr_reason,
    )


__all__ = [
    "FundamentalAnnualHorizonError",
    "SecIssuerFundamentalAnnualHorizonService",
    "annual_horizon_window_start",
]
