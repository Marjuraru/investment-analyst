"""Point-in-time selection of persisted market-statistics snapshots."""

import json
from datetime import UTC, datetime

from investment_analyst.analytics.market.diagnostic_models import (
    MarketDiagnosticRequest,
    MarketMetricSnapshot,
)
from investment_analyst.core.models import MetricResult
from investment_analyst.storage import LocalStorage

_RETURN_KEY = "market.history.simple_return_1d"
_SMA_KEY = "market.history.sma"
_VOLATILITY_KEY = "market.history.rolling_daily_volatility"
_RELATIVE_VOLUME_KEY = "market.history.relative_volume"
_RETURN_ALGORITHM = "market-simple-return-1d-v1-decimal34"
_SMA_ALGORITHM = "market-sma-v1-decimal34"
_VOLATILITY_ALGORITHM = "market-rolling-daily-volatility-v1-decimal34"
_RELATIVE_VOLUME_ALGORITHM = "market-relative-volume-v1-decimal34"

_RETURN_SLOT = "simple_return_1d"
_SHORT_SMA_SLOT = "sma_short"
_LONG_SMA_SLOT = "sma_long"
_VOLATILITY_SLOT = "rolling_daily_volatility"
_RELATIVE_VOLUME_SLOT = "relative_volume"
_REQUIRED_SLOTS = (
    _RETURN_SLOT,
    _SHORT_SMA_SLOT,
    _LONG_SMA_SLOT,
    _VOLATILITY_SLOT,
    _RELATIVE_VOLUME_SLOT,
)


class MarketDiagnosticSelectionError(RuntimeError):
    """Base error for persisted metric selection."""


class AmbiguousMetricRevisionError(MarketDiagnosticSelectionError):
    """Raised when revisions cannot be selected without an arbitrary UUID tie-break."""


class InvalidMetricContextError(MarketDiagnosticSelectionError):
    """Raised when a required persisted metric has malformed point-in-time context."""


def _known_at_parameter(result: MetricResult) -> datetime:
    value = result.parameters.get("known_at")
    if not isinstance(value, str):
        raise InvalidMetricContextError(
            f"metric result {result.result_id} has an invalid known_at parameter"
        )
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise InvalidMetricContextError(
            f"metric result {result.result_id} has an invalid known_at parameter"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidMetricContextError(
            f"metric result {result.result_id} has a naive known_at parameter"
        )
    return parsed.astimezone(UTC)


def _window_parameter(result: MetricResult) -> int:
    value = result.parameters.get("window")
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidMetricContextError(
            f"metric result {result.result_id} has an invalid window parameter"
        )
    return value


def _slot(result: MetricResult, request: MarketDiagnosticRequest) -> str | None:
    if result.metric_key == _RETURN_KEY:
        return _RETURN_SLOT
    if result.metric_key == _SMA_KEY:
        window = _window_parameter(result)
        if window == request.short_sma_window:
            return _SHORT_SMA_SLOT
        if window == request.long_sma_window:
            return _LONG_SMA_SLOT
        return None
    if result.metric_key == _VOLATILITY_KEY:
        return _VOLATILITY_SLOT if _window_parameter(result) == request.volatility_window else None
    if result.metric_key == _RELATIVE_VOLUME_KEY:
        return (
            _RELATIVE_VOLUME_SLOT
            if _window_parameter(result) == request.relative_volume_window
            else None
        )
    return None


def _algorithm_matches(result: MetricResult) -> bool:
    expected = {
        _RETURN_KEY: _RETURN_ALGORITHM,
        _SMA_KEY: _SMA_ALGORITHM,
        _VOLATILITY_KEY: _VOLATILITY_ALGORITHM,
        _RELATIVE_VOLUME_KEY: _RELATIVE_VOLUME_ALGORITHM,
    }
    return result.algorithm_version == expected.get(result.metric_key)


def _parameter_sort_key(result: MetricResult) -> str:
    return json.dumps(result.parameters, allow_nan=False, separators=(",", ":"), sort_keys=True)


def describe_missing_requirements(
    request: MarketDiagnosticRequest,
    candidates: tuple[MetricResult, ...],
) -> tuple[str, ...]:
    """Describe missing configurations at the latest candidate as-of timestamp."""
    if not candidates:
        return _REQUIRED_SLOTS
    latest_as_of = max(result.as_of for result in candidates)
    present = {
        slot
        for result in candidates
        if result.as_of == latest_as_of
        if (slot := _slot(result, request)) is not None
    }
    missing = tuple(slot for slot in _REQUIRED_SLOTS if slot not in present)
    return missing or ("complete_common_as_of_snapshot",)


class MarketDiagnosticMetricSelector:
    """Select the latest complete and unambiguous persisted metric snapshot."""

    def __init__(self, storage: LocalStorage) -> None:
        storage.require_open()
        self._storage = storage

    def candidates(self, request: MarketDiagnosticRequest) -> tuple[MetricResult, ...]:
        """Return deterministically ordered metrics compatible with the request context."""
        self._storage.require_open()
        output: list[MetricResult] = []
        required_keys = {
            _RETURN_KEY,
            _SMA_KEY,
            _VOLATILITY_KEY,
            _RELATIVE_VOLUME_KEY,
        }
        for result in self._storage.metric_results.list(asset_id=request.query.asset_id):
            if result.asset_id != request.query.asset_id:
                continue
            if result.metric_key not in required_keys or not _algorithm_matches(result):
                continue
            if not request.query.start <= result.as_of < request.query.end:
                continue
            if result.available_at > request.query.known_at:
                continue
            source_id = result.parameters.get("source_id")
            if not isinstance(source_id, str):
                raise InvalidMetricContextError(
                    f"metric result {result.result_id} has an invalid source_id parameter"
                )
            if source_id != request.query.source_id:
                continue
            if _known_at_parameter(result) != request.query.known_at:
                continue
            if _slot(result, request) is None:
                continue
            output.append(result)
        output.sort(
            key=lambda item: (
                item.as_of,
                item.metric_key,
                _parameter_sort_key(item),
                item.available_at,
                str(item.result_id),
            )
        )
        return tuple(output)

    def select(self, request: MarketDiagnosticRequest) -> MarketMetricSnapshot | None:
        """Select the latest complete snapshot for one request."""
        return self.select_from_results(request, self.candidates(request))

    def select_from_results(
        self,
        request: MarketDiagnosticRequest,
        candidates: tuple[MetricResult, ...],
    ) -> MarketMetricSnapshot | None:
        """Select from prefiltered results without issuing another repository query."""
        grouped: dict[datetime, dict[str, list[MetricResult]]] = {}
        for result in candidates:
            slot = _slot(result, request)
            if slot is None:
                continue
            grouped.setdefault(result.as_of, {}).setdefault(slot, []).append(result)

        for as_of in sorted(grouped, reverse=True):
            slots = grouped[as_of]
            if any(slot not in slots for slot in _REQUIRED_SLOTS):
                continue
            selected = {
                slot: self._select_latest(slots[slot], as_of=as_of, slot=slot)
                for slot in _REQUIRED_SLOTS
            }
            snapshot = MarketMetricSnapshot(
                asset_id=request.query.asset_id,
                source_id=request.query.source_id,
                known_at=request.query.known_at,
                as_of=as_of,
                simple_return=selected[_RETURN_SLOT],
                short_sma=selected[_SHORT_SMA_SLOT],
                long_sma=selected[_LONG_SMA_SLOT],
                rolling_volatility=selected[_VOLATILITY_SLOT],
                relative_volume=selected[_RELATIVE_VOLUME_SLOT],
            )
            self._verify_request_windows(snapshot, request)
            return snapshot
        return None

    @staticmethod
    def _select_latest(
        revisions: list[MetricResult],
        *,
        as_of: datetime,
        slot: str,
    ) -> MetricResult:
        latest_available_at = max(result.available_at for result in revisions)
        latest = [result for result in revisions if result.available_at == latest_available_at]
        if len(latest) != 1:
            raise AmbiguousMetricRevisionError(
                f"ambiguous revisions for {slot} at {as_of.isoformat()} and "
                f"available_at {latest_available_at.isoformat()}"
            )
        return latest[0]

    @staticmethod
    def _verify_request_windows(
        snapshot: MarketMetricSnapshot,
        request: MarketDiagnosticRequest,
    ) -> None:
        checks = (
            (snapshot.short_sma, request.short_sma_window),
            (snapshot.long_sma, request.long_sma_window),
            (snapshot.rolling_volatility, request.volatility_window),
            (snapshot.relative_volume, request.relative_volume_window),
        )
        if any(_window_parameter(result) != expected for result, expected in checks):
            raise InvalidMetricContextError("selected metric windows do not match the request")
