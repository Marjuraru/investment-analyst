"""Decimal34 metrics over revision-selected crypto-derivatives observations."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from uuid import UUID

from pydantic import JsonValue

from investment_analyst.analytics.crypto.derivatives_identity import metric_result_id
from investment_analyst.analytics.crypto.derivatives_models import (
    CryptoDerivativesMetricComputation,
    observation_time,
)
from investment_analyst.core.models import (
    DataQuality,
    MetricCategory,
    MetricDefinition,
    MetricResult,
    NormalizedObservation,
)

ALGORITHM_VERSION = "crypto-derivatives-metrics-v1-decimal34"
FUNDING_SUM_KEY = "crypto.derivatives.funding.sum_1h"
FUNDING_MEAN_KEY = "crypto.derivatives.funding.mean_1h"
DVOL_CHANGE_KEY = "crypto.derivatives.dvol.change_points"
SPREAD_BPS_KEY = "crypto.derivatives.perpetual.bid_ask_spread_bps"
FUNDING_WINDOWS = (24, 168, 720)
DVOL_WINDOWS = (1, 7, 30)
DECIMAL34 = Context(prec=34)


class CryptoDerivativesMetricError(ValueError):
    """Invalid or ambiguous derivatives analytical input."""


class AmbiguousCryptoDerivativesRevisionError(CryptoDerivativesMetricError):
    """Two semantically different revisions share one latest availability."""


METRIC_DEFINITIONS = (
    MetricDefinition(
        metric_key=FUNDING_SUM_KEY,
        display_name="Hourly funding interest sum",
        category=MetricCategory.CRYPTO_DERIVATIVES,
        description="Exact sum of consecutive Deribit historical interest_1h observations.",
        formula="sum(funding_interest_1h[t-window+1:t])",
        unit="ratio",
        default_parameters={"windows": list(FUNDING_WINDOWS)},
        limitations=[
            "Historical Deribit backfill is available only from first local retrieval.",
            "The metric is descriptive and is not annualized or a trading signal.",
        ],
        references=["Deribit public/get_funding_rate_history"],
        definition_version=ALGORITHM_VERSION,
    ),
    MetricDefinition(
        metric_key=FUNDING_MEAN_KEY,
        display_name="Hourly funding interest mean",
        category=MetricCategory.CRYPTO_DERIVATIVES,
        description="Exact mean of consecutive Deribit historical interest_1h observations.",
        formula="sum_1h / window",
        unit="ratio_per_hour",
        default_parameters={"windows": list(FUNDING_WINDOWS)},
        limitations=["No annualization or interpolation is performed."],
        references=["Deribit public/get_funding_rate_history"],
        definition_version=ALGORITHM_VERSION,
    ),
    MetricDefinition(
        metric_key=DVOL_CHANGE_KEY,
        display_name="DVOL point change",
        category=MetricCategory.CRYPTO_DERIVATIVES,
        description="Daily DVOL close difference across a complete exact-day window.",
        formula="dvol_close(as_of) - dvol_close(as_of-window_days)",
        unit="dvol_index_points",
        default_parameters={"windows": list(DVOL_WINDOWS)},
        limitations=["DVOL gaps are not interpolated."],
        references=["Deribit public/get_volatility_index_data"],
        definition_version=ALGORITHM_VERSION,
    ),
    MetricDefinition(
        metric_key=SPREAD_BPS_KEY,
        display_name="Perpetual bid-ask spread",
        category=MetricCategory.CRYPTO_DERIVATIVES,
        description="Quoted spread in basis points from one prospective summary snapshot.",
        formula="(ask_price - bid_price) / mid_price * 10000",
        unit="basis_points",
        limitations=["Snapshots are locally captured and have no fixed provider cadence."],
        references=["Deribit public/get_book_summary_by_instrument"],
        definition_version=ALGORITHM_VERSION,
    ),
)


class CryptoDerivativesMetricEngine:
    """Compute independent metrics without importing provider implementations."""

    def compute(
        self,
        observations: Iterable[NormalizedObservation],
        *,
        asset_id: str,
        funding_source_id: str,
        dvol_source_id: str,
        summary_source_id: str,
        known_at: datetime,
        computed_at: datetime,
        as_of_from: datetime | None = None,
        as_of_before: datetime | None = None,
    ) -> CryptoDerivativesMetricComputation:
        known = _utc(known_at)
        computed = _utc(computed_at)
        lower = _utc(as_of_from) if as_of_from is not None else None
        upper = _utc(as_of_before) if as_of_before is not None else None
        expected_sources = {
            "funding_interest_1h": funding_source_id,
            "dvol_close": dvol_source_id,
            "bid_price": summary_source_id,
            "ask_price": summary_source_id,
            "mid_price": summary_source_id,
        }
        eligible = tuple(
            item
            for item in observations
            if item.asset_id == asset_id
            and item.field_name in expected_sources
            and item.source.source_id == expected_sources[item.field_name]
            and item.available_at <= known
            and item.observed_at is not None
            and item.observed_at <= known
            and (item.period_end is None or item.period_end <= known)
        )
        selected = select_latest_revisions(eligible, known_at=known)
        by_field: dict[str, list[NormalizedObservation]] = defaultdict(list)
        for item in selected:
            by_field[item.field_name].append(item)
        for values in by_field.values():
            values.sort(key=lambda item: (observation_time(item), str(item.observation_id)))

        results: list[MetricResult] = []
        missing: set[str] = set()
        funding = by_field["funding_interest_1h"]
        for window in FUNDING_WINDOWS:
            window_results = self._funding_metrics(
                funding,
                asset_id=asset_id,
                source_id=funding_source_id,
                known_at=known,
                computed_at=computed,
                window=window,
                as_of_from=lower,
                as_of_before=upper,
            )
            if not window_results:
                missing.add(f"funding_interest_1h:consecutive_window={window}")
            results.extend(window_results)

        dvol = by_field["dvol_close"]
        for window in DVOL_WINDOWS:
            window_results = self._dvol_metrics(
                dvol,
                asset_id=asset_id,
                source_id=dvol_source_id,
                known_at=known,
                computed_at=computed,
                window=window,
                as_of_from=lower,
                as_of_before=upper,
            )
            if not window_results:
                missing.add(f"dvol_close:consecutive_window_days={window}")
            results.extend(window_results)

        spread_results = self._spread_metrics(
            selected,
            asset_id=asset_id,
            source_id=summary_source_id,
            known_at=known,
            computed_at=computed,
            as_of_from=lower,
            as_of_before=upper,
        )
        if not spread_results:
            missing.add("perpetual_summary:bid_ask_mid_same_raw")
        results.extend(spread_results)
        return CryptoDerivativesMetricComputation(
            results=tuple(
                sorted(
                    results,
                    key=lambda item: (item.as_of, item.metric_key, str(item.result_id)),
                )
            ),
            missing_requirements=tuple(sorted(missing)),
        )

    def _funding_metrics(
        self,
        observations: list[NormalizedObservation],
        *,
        asset_id: str,
        source_id: str,
        known_at: datetime,
        computed_at: datetime,
        window: int,
        as_of_from: datetime | None,
        as_of_before: datetime | None,
    ) -> tuple[MetricResult, ...]:
        results: list[MetricResult] = []
        for index in range(window - 1, len(observations)):
            inputs = tuple(observations[index - window + 1 : index + 1])
            times = tuple(observation_time(item) for item in inputs)
            if any(
                current - previous != timedelta(hours=1)
                for previous, current in zip(times, times[1:], strict=False)
            ):
                continue
            as_of = times[-1]
            if not _within(as_of, as_of_from, as_of_before):
                continue
            with localcontext(DECIMAL34):
                sum_value = sum((item.value for item in inputs), start=Decimal(0))
                mean_value = sum_value / Decimal(window)
            common = {
                "asset_id": asset_id,
                "inputs": inputs,
                "source_ids": (source_id,),
                "known_at": known_at,
                "computed_at": computed_at,
                "window": window,
                "as_of": as_of,
            }
            results.append(
                _metric_result(
                    **common,
                    metric_key=FUNDING_SUM_KEY,
                    value=sum_value,
                    unit="ratio",
                    formula="sum(funding_interest_1h)",
                )
            )
            results.append(
                _metric_result(
                    **common,
                    metric_key=FUNDING_MEAN_KEY,
                    value=mean_value,
                    unit="ratio_per_hour",
                    formula="sum_1h / window",
                )
            )
        return tuple(results)

    def _dvol_metrics(
        self,
        observations: list[NormalizedObservation],
        *,
        asset_id: str,
        source_id: str,
        known_at: datetime,
        computed_at: datetime,
        window: int,
        as_of_from: datetime | None,
        as_of_before: datetime | None,
    ) -> tuple[MetricResult, ...]:
        by_time = {observation_time(item): item for item in observations}
        results: list[MetricResult] = []
        for current in observations:
            as_of = observation_time(current)
            if not _within(as_of, as_of_from, as_of_before):
                continue
            times = tuple(as_of - timedelta(days=offset) for offset in range(window, -1, -1))
            if any(timestamp not in by_time for timestamp in times):
                continue
            inputs = tuple(by_time[timestamp] for timestamp in times)
            with localcontext(DECIMAL34):
                change = inputs[-1].value - inputs[0].value
            results.append(
                _metric_result(
                    asset_id=asset_id,
                    metric_key=DVOL_CHANGE_KEY,
                    value=change,
                    unit="dvol_index_points",
                    inputs=inputs,
                    source_ids=(source_id,),
                    known_at=known_at,
                    computed_at=computed_at,
                    window=window,
                    as_of=as_of,
                    formula="dvol_close(as_of) - dvol_close(as_of-window_days)",
                )
            )
        return tuple(results)

    def _spread_metrics(
        self,
        observations: tuple[NormalizedObservation, ...],
        *,
        asset_id: str,
        source_id: str,
        known_at: datetime,
        computed_at: datetime,
        as_of_from: datetime | None,
        as_of_before: datetime | None,
    ) -> tuple[MetricResult, ...]:
        by_raw: dict[UUID, dict[str, NormalizedObservation]] = defaultdict(dict)
        for item in observations:
            if item.field_name in {"bid_price", "ask_price", "mid_price"}:
                by_raw[item.raw_record_id][item.field_name] = item
        results: list[MetricResult] = []
        for fields in by_raw.values():
            if set(fields) != {"bid_price", "ask_price", "mid_price"}:
                continue
            inputs = tuple(fields[name] for name in ("bid_price", "ask_price", "mid_price"))
            times = {observation_time(item) for item in inputs}
            if len(times) != 1:
                continue
            as_of = next(iter(times))
            if not _within(as_of, as_of_from, as_of_before):
                continue
            bid, ask, mid = (item.value for item in inputs)
            if mid <= 0 or ask < bid:
                continue
            with localcontext(DECIMAL34):
                spread = (ask - bid) / mid * Decimal(10_000)
            results.append(
                _metric_result(
                    asset_id=asset_id,
                    metric_key=SPREAD_BPS_KEY,
                    value=spread,
                    unit="basis_points",
                    inputs=inputs,
                    source_ids=(source_id,),
                    known_at=known_at,
                    computed_at=computed_at,
                    window=1,
                    as_of=as_of,
                    formula="(ask_price - bid_price) / mid_price * 10000",
                )
            )
        return tuple(results)


def select_latest_revisions(
    observations: Iterable[NormalizedObservation],
    *,
    known_at: datetime,
) -> tuple[NormalizedObservation, ...]:
    """Select the latest eligible semantic revision per field/event or fail closed."""
    known = _utc(known_at)
    grouped: dict[tuple[str, datetime], list[NormalizedObservation]] = defaultdict(list)
    for item in observations:
        if item.available_at <= known:
            grouped[(item.field_name, observation_time(item))].append(item)
    selected: list[NormalizedObservation] = []
    for candidates in grouped.values():
        latest_available = max(item.available_at for item in candidates)
        latest = tuple(item for item in candidates if item.available_at == latest_available)
        semantic = {_observation_semantic(item) for item in latest}
        if len(semantic) > 1:
            raise AmbiguousCryptoDerivativesRevisionError(
                "semantically different derivatives revisions share one availability"
            )
        selected.append(min(latest, key=lambda item: str(item.observation_id)))
    return tuple(
        sorted(
            selected,
            key=lambda item: (observation_time(item), item.field_name, str(item.observation_id)),
        )
    )


def _metric_result(
    *,
    asset_id: str,
    metric_key: str,
    value: Decimal,
    unit: str,
    inputs: tuple[NormalizedObservation, ...],
    source_ids: tuple[str, ...],
    known_at: datetime,
    computed_at: datetime,
    window: int,
    as_of: datetime,
    formula: str,
) -> MetricResult:
    input_ids = tuple(item.observation_id for item in inputs)
    available_at = max(item.available_at for item in inputs)
    parameters: dict[str, JsonValue] = {
        "formula": formula,
        "known_at": known_at.isoformat(),
        "source_ids": list(source_ids),
        "window": window,
    }
    quality = DataQuality.VALID
    identifier = metric_result_id(
        asset_id=asset_id,
        metric_key=metric_key,
        input_observation_ids=input_ids,
        parameters=parameters,
        algorithm_version=ALGORITHM_VERSION,
        as_of=as_of,
        available_at=available_at,
        value=value,
        unit=unit,
        quality=quality,
    )
    return MetricResult(
        result_id=identifier,
        asset_id=asset_id,
        metric_key=metric_key,
        value=value,
        unit=unit,
        as_of=as_of,
        available_at=available_at,
        computed_at=max(computed_at, available_at),
        parameters=parameters,
        input_observation_ids=list(input_ids),
        algorithm_version=ALGORITHM_VERSION,
        quality=quality,
    )


def _observation_semantic(item: NormalizedObservation) -> tuple[object, ...]:
    return (
        item.asset_id,
        item.field_name,
        item.value,
        item.unit,
        item.frequency,
        item.observed_at,
        item.period_start,
        item.period_end,
        item.available_at,
        item.source.source_id,
        item.quality,
        item.transformation_version,
    )


def _within(
    as_of: datetime,
    lower: datetime | None,
    upper: datetime | None,
) -> bool:
    return (lower is None or as_of >= lower) and (upper is None or as_of < upper)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoDerivativesMetricError("analytics datetimes must include timezone information")
    return value.astimezone(UTC)


__all__ = [
    "ALGORITHM_VERSION",
    "AmbiguousCryptoDerivativesRevisionError",
    "CryptoDerivativesMetricEngine",
    "CryptoDerivativesMetricError",
    "DVOL_CHANGE_KEY",
    "DVOL_WINDOWS",
    "FUNDING_MEAN_KEY",
    "FUNDING_SUM_KEY",
    "FUNDING_WINDOWS",
    "METRIC_DEFINITIONS",
    "SPREAD_BPS_KEY",
    "select_latest_revisions",
]
