"""Selection of persisted valuation results without writes, providers, or clocks."""

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Context, Decimal, localcontext
from typing import Protocol

from investment_analyst.analytics.valuation.history_models import (
    CorporateValuationHistory,
    CorporateValuationHistoryCoverage,
    CorporateValuationHistoryPoint,
    CorporateValuationHistoryRequest,
    CorporateValuationHistorySeries,
    CorporateValuationHistoryStatistics,
)
from investment_analyst.core.models import DataQuality, MetricResult

_DECIMAL34 = Context(prec=34)


class CorporateValuationHistoryError(RuntimeError):
    """Raised for malformed or ambiguous persisted valuation evidence."""


class _ResultRepository(Protocol):
    def list(self, *, asset_id: str | None = None) -> list[MetricResult]: ...


class _Storage(Protocol):
    metric_results: _ResultRepository

    def require_open(self) -> None: ...


class CorporateValuationHistoryService:
    """Read materialized latest-annual valuation results at an explicit cut."""

    def __init__(self, storage: _Storage) -> None:
        storage.require_open()
        self._storage = storage

    def query(self, request: CorporateValuationHistoryRequest) -> CorporateValuationHistory:
        candidates = tuple(
            result
            for result in self._storage.metric_results.list(asset_id=request.asset_id)
            if self._is_candidate(result, request)
        )
        selected, superseded = self._select(candidates)
        grouped: dict[tuple[str, str, str, str], list[CorporateValuationHistoryPoint]] = (
            defaultdict(list)
        )
        for result in selected[: request.limit]:
            point = self._point(result)
            grouped[
                (
                    point.metric_key,
                    point.algorithm_version,
                    point.unit,
                    point.security_basis_version,
                )
            ].append(point)
        series = tuple(
            CorporateValuationHistorySeries(
                metric_key=key[0],
                algorithm_version=key[1],
                unit=key[2],
                basis="latest_annual",
                security_basis_version=key[3],
                points=tuple(
                    sorted(points, key=lambda point: (point.valuation_date, str(point.result_id)))
                ),
                statistics=self._statistics(points),
            )
            for key, points in sorted(grouped.items())
        )
        returned = sum(len(item.points) for item in series)
        return CorporateValuationHistory(
            request=request,
            series=series,
            coverage=CorporateValuationHistoryCoverage(
                candidate_results=len(candidates),
                superseded_revisions=superseded,
                returned_points=returned,
                returned_series=len(series),
                truncated=len(selected) > request.limit,
            ),
        )

    @staticmethod
    def _is_candidate(result: MetricResult, request: CorporateValuationHistoryRequest) -> bool:
        parameters = result.parameters
        if (
            result.quality is not DataQuality.VALID
            or parameters.get("category") != "valuation"
            or parameters.get("basis") != request.basis
        ):
            return False
        try:
            valuation_date = _calendar_parameter(parameters["valuation_date"], "valuation_date")
            known_at = _timestamp_parameter(parameters["known_at"], "known_at")
            _timestamp_parameter(parameters["annual_period_end"], "annual_period_end")
            security_basis_version = parameters["security_basis_version"]
        except (KeyError, TypeError, ValueError) as error:
            raise CorporateValuationHistoryError(
                "valuation history parameters are malformed"
            ) from error
        if not isinstance(security_basis_version, str) or not security_basis_version:
            raise CorporateValuationHistoryError("valuation security basis version is malformed")
        if not result.value.is_finite():
            raise CorporateValuationHistoryError("valuation history value must be finite")
        return (
            request.start_date <= valuation_date <= request.end_date
            and known_at <= request.known_at
            and result.available_at <= request.known_at
        )

    @staticmethod
    def _select(results: tuple[MetricResult, ...]) -> tuple[tuple[MetricResult, ...], int]:
        revisions: dict[tuple[str, str, str, str, str], list[MetricResult]] = defaultdict(list)
        for result in results:
            parameters = result.parameters
            revisions[
                (
                    result.metric_key,
                    result.algorithm_version,
                    result.unit,
                    str(parameters["security_basis_version"]),
                    str(parameters["valuation_date"]),
                )
            ].append(result)
        selected: list[MetricResult] = []
        superseded = 0
        for alternatives in revisions.values():
            latest_known = max(
                _timestamp_parameter(item.parameters["known_at"], "known_at")
                for item in alternatives
            )
            winners = [
                item
                for item in alternatives
                if _timestamp_parameter(item.parameters["known_at"], "known_at") == latest_known
            ]
            semantic = {
                (item.value, item.available_at, tuple(item.input_observation_ids))
                for item in winners
            }
            if len(semantic) != 1:
                raise CorporateValuationHistoryError("valuation history revision is ambiguous")
            selected.append(winners[0])
            superseded += len(alternatives) - 1
        return tuple(
            sorted(selected, key=lambda item: (item.metric_key, item.as_of, str(item.result_id)))
        ), superseded

    @staticmethod
    def _point(result: MetricResult) -> CorporateValuationHistoryPoint:
        parameters = result.parameters
        return CorporateValuationHistoryPoint(
            metric_key=result.metric_key,
            algorithm_version=result.algorithm_version,
            unit=result.unit,
            basis="latest_annual",
            security_basis_version=str(parameters["security_basis_version"]),
            valuation_date=_calendar_parameter(parameters["valuation_date"], "valuation_date"),
            price_as_of=result.as_of,
            annual_period_end=_timestamp_parameter(
                parameters["annual_period_end"], "annual_period_end"
            ),
            source_known_at=_timestamp_parameter(parameters["known_at"], "known_at"),
            available_at=result.available_at,
            result_id=result.result_id,
            value=result.value,
            input_observation_ids=tuple(result.input_observation_ids),
        )

    @staticmethod
    def _statistics(
        points: list[CorporateValuationHistoryPoint],
    ) -> CorporateValuationHistoryStatistics:
        values = tuple(point.value for point in points)
        with localcontext(_DECIMAL34):
            first, last = values[0], values[-1]
            return CorporateValuationHistoryStatistics(
                count=len(values),
                first_value=first,
                last_value=last,
                minimum=min(values),
                maximum=max(values),
                arithmetic_mean=sum(values) / Decimal(len(values)),
                value_range=max(values) - min(values),
                previous_change=last - values[-2] if len(values) > 1 else None,
                horizon_change=last - first if len(values) > 1 else None,
            )


def _calendar_parameter(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO calendar date")
    return datetime.strptime(value, "%Y-%m-%d").date()


def _timestamp_parameter(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timestamp")
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return timestamp.astimezone(UTC)
