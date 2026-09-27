"""Shared daily market cut policy for catalog-backed Alpaca and Coinbase flows."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from investment_analyst.application.aapl_bootstrap_models import AaplRefreshMode
from investment_analyst.application.btc_refresh_models import BtcRefreshMode
from investment_analyst.core.models import DataFrequency
from investment_analyst.storage import LocalStorage

MARKET_DAILY_NO_NEW_INPUT_CUT_POLICY = "market-daily-no-new-input-cut-v1"
_OPERATIONAL_ANALYTICS_DAYS = 90


class MarketDailyCutError(RuntimeError):
    """Raised when a stable daily cut cannot be projected or verified."""


def resolve_market_daily_cut(
    storage: LocalStorage,
    *,
    asset_id: str,
    source_id: str,
    market_start: date,
    market_end: date,
    requested_end: date,
    refresh_mode: object,
    fetch_created_inputs: bool,
    effective_known_at: datetime,
) -> tuple[datetime, datetime, datetime]:
    """Project a stable analytics cut when a fetch created no new bar inputs.

    After a completed AUTO fetch without an explicit cut, when no bar/candle
    raw records or observations were created and the persisted daily series
    holds complete eligible bars inside the requested range, the analytics
    window keeps the maximum availability of the eligible observations and
    the analytics end the day after the last valid bar, bounded by the
    requested end. Any other situation keeps the caller-supplied cut.
    Corrupt or partial projections never invent coverage.
    """
    start, end = _requested_bounds(market_start, market_end, requested_end)
    if _is_explicit_cut(refresh_mode):
        return start, end, effective_known_at
    if fetch_created_inputs:
        return start, end, effective_known_at
    if _is_full_mode(refresh_mode):
        return start, end, effective_known_at
    if effective_known_at.tzinfo is None or effective_known_at.utcoffset() is None:
        raise MarketDailyCutError("effective_known_at must be timezone-aware")
    projected = _project_series_cut(
        storage,
        asset_id=asset_id,
        source_id=source_id,
        requested_start=start,
        requested_end=end,
        effective_known_at=effective_known_at,
    )
    if projected is None:
        return start, end, effective_known_at
    return projected


def _requested_bounds(
    market_start: date, market_end: date, requested_end: date
) -> tuple[datetime, datetime]:
    start_at = datetime.combine(market_start, time.min, tzinfo=UTC)
    end_at = datetime.combine(market_end + timedelta(days=1), time.min, tzinfo=UTC)
    requested_end_at = datetime.combine(requested_end + timedelta(days=1), time.min, tzinfo=UTC)
    if end_at > requested_end_at:
        end_at = requested_end_at
    return start_at, end_at


def _is_explicit_cut(refresh_mode: object) -> bool:
    return getattr(refresh_mode, "requested_known_at", None) is not None


def _is_full_mode(refresh_mode: object) -> bool:
    mode = getattr(refresh_mode, "refresh_mode", refresh_mode)
    return mode in (AaplRefreshMode.FULL, BtcRefreshMode.FULL)


def _project_series_cut(
    storage: LocalStorage,
    *,
    asset_id: str,
    source_id: str,
    requested_start: datetime,
    requested_end: datetime,
    effective_known_at: datetime,
) -> tuple[datetime, datetime, datetime] | None:
    earliest, latest = storage.observations.observed_at_bounds(
        asset_id=asset_id,
        source_id=source_id,
        frequency=DataFrequency.DAY_1,
        observed_from=requested_start,
        observed_before=requested_end,
        available_to=effective_known_at,
    )
    if earliest is None or latest is None:
        return None
    earliest = earliest.astimezone(UTC)
    latest = latest.astimezone(UTC)
    if earliest < requested_start or latest >= requested_end:
        return None
    if earliest.date() > latest.date():
        return None
    window_end_day = min(latest.date() + timedelta(days=1), requested_end.date())
    window_end = datetime.combine(window_end_day, time.min, tzinfo=UTC)
    window_start = max(requested_start, window_end - timedelta(days=_OPERATIONAL_ANALYTICS_DAYS))
    if not window_start < window_end <= requested_end:
        return None
    availability = storage.observations.maximum_available_at(
        asset_id=asset_id,
        source_id=source_id,
        frequency=DataFrequency.DAY_1,
        observed_from=requested_start,
        observed_before=requested_end,
        available_to=effective_known_at,
    )
    if availability is None:
        return None
    availability = availability.astimezone(UTC)
    if availability > effective_known_at:
        raise MarketDailyCutError("projected availability exceeds effective_known_at")
    return window_start, window_end, availability


__all__ = [
    "MARKET_DAILY_NO_NEW_INPUT_CUT_POLICY",
    "MarketDailyCutError",
    "resolve_market_daily_cut",
]
