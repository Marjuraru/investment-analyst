"""Selection of persisted valuation results without writes, providers, or clocks."""

from collections import defaultdict
from collections.abc import Collection
from datetime import UTC, date, datetime
from decimal import Context, Decimal, localcontext
from typing import Protocol
from uuid import UUID

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
_RESULT_LOOKUP_BATCH_SIZE = 256
_VALUATION_METRIC_KEYS = (
    "valuation.corporate.earnings_yield_latest_annual",
    "valuation.corporate.enterprise_value",
    "valuation.corporate.enterprise_value_to_ebit_latest_annual",
    "valuation.corporate.enterprise_value_to_ebitda_latest_annual",
    "valuation.corporate.enterprise_value_to_sales_latest_annual",
    "valuation.corporate.financial_debt",
    "valuation.corporate.free_cash_flow_yield_latest_annual",
    "valuation.corporate.market_cap",
    "valuation.corporate.price_to_book",
    "valuation.corporate.price_to_earnings_latest_annual",
    "valuation.corporate.price_to_sales_latest_annual",
)


class CorporateValuationHistoryError(RuntimeError):
    """Raised for malformed or ambiguous persisted valuation evidence."""


class _ResultRepository(Protocol):
    def list_ids(
        self,
        *,
        asset_id: str | None = None,
        metric_keys: Collection[str] | None = None,
        available_to: datetime | None = None,
        parameter_equals: dict[str, str] | None = None,
        parameter_date_range: tuple[str, str] | None = None,
        legacy_known_at_to: datetime | None = None,
    ) -> list[UUID]: ...

    def get_many(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]: ...


class _Storage(Protocol):
    metric_results: _ResultRepository

    def require_open(self) -> None: ...


class CorporateValuationHistoryService:
    """Read materialized latest-annual valuation results at an explicit cut."""

    def __init__(self, storage: _Storage) -> None:
        storage.require_open()
        self._storage = storage

    def query(self, request: CorporateValuationHistoryRequest) -> CorporateValuationHistory:
        candidate_ids = self._storage.metric_results.list_ids(
            asset_id=request.asset_id,
            metric_keys=_VALUATION_METRIC_KEYS,
            available_to=request.known_at,
            parameter_equals={"category": "valuation", "basis": request.basis},
            parameter_date_range=(request.start_date.isoformat(), request.end_date.isoformat()),
            legacy_known_at_to=request.known_at,
        )
        indexed: dict[UUID, MetricResult] = {}
        for offset in range(0, len(candidate_ids), _RESULT_LOOKUP_BATCH_SIZE):
            batch_ids = candidate_ids[offset : offset + _RESULT_LOOKUP_BATCH_SIZE]
            indexed.update(self._storage.metric_results.get_many(batch_ids))
        missing = [item for item in candidate_ids if item not in indexed]
        if missing:
            raise CorporateValuationHistoryError("selected valuation result is absent")
        candidates = tuple(
            indexed[result_id]
            for result_id in candidate_ids
            if self._is_candidate(indexed[result_id], request)
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
            known_at = _source_cut(result)
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
            latest_known = max(_source_cut(item) for item in alternatives)
            winners = [item for item in alternatives if _source_cut(item) == latest_known]
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
            source_known_at=_source_cut(result),
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


def _source_cut(result: MetricResult) -> datetime:
    """Resolve legacy cuts from parameters and v2 visibility from available_at."""
    if result.result_id.version == 8:
        if "known_at" in result.parameters:
            raise ValueError("v2 valuation parameters must not contain known_at")
        return result.available_at
    return _timestamp_parameter(result.parameters["known_at"], "known_at")
