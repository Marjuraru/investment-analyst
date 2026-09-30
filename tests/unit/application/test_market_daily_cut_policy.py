"""Unit tests for the shared daily market cut policy."""

from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investment_analyst.application.aapl_bootstrap_models import AaplRefreshMode
from investment_analyst.application.btc_refresh_models import BtcRefreshMode
from investment_analyst.application.market_daily_cut import (
    MarketDailyCutError,
    resolve_market_daily_cut,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.storage import LocalStorage, StoragePaths


def _seed_observation(
    storage: LocalStorage,
    *,
    asset_id: str,
    source_id: str,
    observed_at: datetime,
    available_at: datetime,
) -> None:
    reference = SourceReference(
        source_id=source_id,
        record_key=f"{asset_id}:{observed_at.date().isoformat()}",
        retrieved_at=available_at,
    )
    obs = NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=uuid4(),
        asset_id=asset_id,
        field_name="close",
        value="100.0",
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=observed_at,
        available_at=available_at,
        normalized_at=available_at,
        source=reference,
        quality=DataQuality.VALID,
        transformation_version="test-v1",
    )
    storage.observations.save(obs)


def test_operational_bounds_cover_all_resolver_branches(tmp_path) -> None:
    asset_id = "equity:us:aapl"
    source_id = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
    clock_utc = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

    # Timezone with non-UTC offset (+02:00) to test normalization
    offset_tz = timezone(timedelta(hours=2))
    clock_with_offset = datetime(2026, 9, 29, 14, 0, tzinfo=offset_tz)

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        # Branch 1: Short range (3 days) -> [2026-07-07, 2026-07-10), window is 3 days (<90)
        start_3d, end_3d, known_3d = resolve_market_daily_cut(
            storage,
            asset_id=asset_id,
            source_id=source_id,
            market_start=date(2026, 7, 7),
            market_end=date(2026, 7, 9),
            requested_end=date(2026, 7, 9),
            refresh_mode=AaplRefreshMode.AUTO,
            fetch_created_inputs=True,
            effective_known_at=clock_utc,
        )
        assert start_3d == datetime(2026, 7, 7, 0, 0, tzinfo=UTC)
        assert end_3d == datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
        assert known_3d == clock_utc

        # Branch 2: Exact range (90 days) -> [2026-04-12, 2026-07-11)
        # 90 calendar days inclusive (April 12 to July 10 = 90 days)
        start_90d, end_90d, known_90d = resolve_market_daily_cut(
            storage,
            asset_id=asset_id,
            source_id=source_id,
            market_start=date(2026, 4, 12),
            market_end=date(2026, 7, 10),
            requested_end=date(2026, 7, 10),
            refresh_mode=AaplRefreshMode.AUTO,
            fetch_created_inputs=True,
            effective_known_at=clock_utc,
        )
        assert end_90d == datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
        assert start_90d == datetime(2026, 4, 12, 0, 0, tzinfo=UTC)
        assert end_90d - start_90d == timedelta(days=90)
        assert known_90d == clock_utc

        # Long range (>600 days): 2025-01-01 to 2026-09-28 (636 days)
        # Operational bounds: requested_end=2026-09-28 -> end=2026-09-29T00:00:00Z
        # operational_start = 2026-09-29 - 90d = 2026-07-01T00:00:00Z
        expected_long_start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        expected_long_end = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)

        # Branch 3: Long range + AUTO with new inputs (fetch_created_inputs=True)
        start_new, end_new, known_new = resolve_market_daily_cut(
            storage,
            asset_id=asset_id,
            source_id=source_id,
            market_start=date(2025, 1, 1),
            market_end=date(2026, 9, 28),
            requested_end=date(2026, 9, 28),
            refresh_mode=AaplRefreshMode.AUTO,
            fetch_created_inputs=True,
            effective_known_at=clock_with_offset,
        )
        assert start_new == expected_long_start
        assert end_new == expected_long_end
        assert known_new == clock_utc  # Normalized to UTC
        assert known_new.tzinfo == UTC

        # Branch 4: Long range + FULL mode (AaplRefreshMode.FULL & BtcRefreshMode.FULL)
        start_full, end_full, known_full = resolve_market_daily_cut(
            storage,
            asset_id=asset_id,
            source_id=source_id,
            market_start=date(2025, 1, 1),
            market_end=date(2026, 9, 28),
            requested_end=date(2026, 9, 28),
            refresh_mode=AaplRefreshMode.FULL,
            fetch_created_inputs=False,
            effective_known_at=clock_utc,
        )
        assert start_full == expected_long_start
        assert end_full == expected_long_end
        assert known_full == clock_utc

        start_btc_full, end_btc_full, _ = resolve_market_daily_cut(
            storage,
            asset_id="crypto:btc-usd",
            source_id="coinbase-exchange:btc-usd:daily-candles",
            market_start=date(2025, 1, 1),
            market_end=date(2026, 9, 28),
            requested_end=date(2026, 9, 28),
            refresh_mode=BtcRefreshMode.FULL,
            fetch_created_inputs=False,
            effective_known_at=clock_utc,
        )
        assert start_btc_full == expected_long_start
        assert end_btc_full == expected_long_end

        # Branch 5: Long range + Explicit cut
        explicit_mode = SimpleNamespace(
            requested_known_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
            refresh_mode=AaplRefreshMode.AUTO,
        )
        start_exp, end_exp, known_exp = resolve_market_daily_cut(
            storage,
            asset_id=asset_id,
            source_id=source_id,
            market_start=date(2025, 1, 1),
            market_end=date(2026, 9, 28),
            requested_end=date(2026, 9, 28),
            refresh_mode=explicit_mode,
            fetch_created_inputs=False,
            effective_known_at=clock_utc,
        )
        assert start_exp == expected_long_start
        assert end_exp == expected_long_end
        assert known_exp == clock_utc

        # Branch 6: Long range + Fallback without projection (storage is empty)
        start_fb, end_fb, known_fb = resolve_market_daily_cut(
            storage,
            asset_id=asset_id,
            source_id=source_id,
            market_start=date(2025, 1, 1),
            market_end=date(2026, 9, 28),
            requested_end=date(2026, 9, 28),
            refresh_mode=AaplRefreshMode.AUTO,
            fetch_created_inputs=False,
            effective_known_at=clock_utc,
        )
        assert start_fb == expected_long_start
        assert end_fb == expected_long_end
        assert known_fb == clock_utc

        # Branch 7: Long range + AUTO without new data, with stored observations ->
        # projection succeeds
        obs_date = datetime(2026, 9, 25, 0, 0, tzinfo=UTC)
        obs_avail = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
        _seed_observation(
            storage,
            asset_id=asset_id,
            source_id=source_id,
            observed_at=obs_date,
            available_at=obs_avail,
        )
        start_proj, end_proj, known_proj = resolve_market_daily_cut(
            storage,
            asset_id=asset_id,
            source_id=source_id,
            market_start=date(2025, 1, 1),
            market_end=date(2026, 9, 28),
            requested_end=date(2026, 9, 28),
            refresh_mode=AaplRefreshMode.AUTO,
            fetch_created_inputs=False,
            effective_known_at=clock_utc,
        )
        # Projected end is day after latest bar (2026-09-25 + 1d = 2026-09-26)
        # Window start is bounded to 90 days before projected end: 2026-09-26 - 90d = 2026-06-28
        assert end_proj == datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
        assert start_proj == datetime(2026, 6, 28, 0, 0, tzinfo=UTC)
        assert end_proj - start_proj == timedelta(days=90)
        assert known_proj == obs_avail


def test_invalid_clock_is_rejected_before_early_returns(tmp_path) -> None:
    asset_id = "equity:us:aapl"
    source_id = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
    naive_clock = datetime(2026, 9, 29, 12, 0)  # No tzinfo

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        # 1. Rejected even when explicit cut is present
        explicit_mode = SimpleNamespace(requested_known_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
        with pytest.raises(MarketDailyCutError, match="timezone-aware"):
            resolve_market_daily_cut(
                storage,
                asset_id=asset_id,
                source_id=source_id,
                market_start=date(2026, 7, 7),
                market_end=date(2026, 7, 9),
                requested_end=date(2026, 7, 9),
                refresh_mode=explicit_mode,
                fetch_created_inputs=False,
                effective_known_at=naive_clock,
            )

        # 2. Rejected even when fetch_created_inputs is True
        with pytest.raises(MarketDailyCutError, match="timezone-aware"):
            resolve_market_daily_cut(
                storage,
                asset_id=asset_id,
                source_id=source_id,
                market_start=date(2026, 7, 7),
                market_end=date(2026, 7, 9),
                requested_end=date(2026, 7, 9),
                refresh_mode=AaplRefreshMode.AUTO,
                fetch_created_inputs=True,
                effective_known_at=naive_clock,
            )

        # 3. Rejected even when in FULL mode
        with pytest.raises(MarketDailyCutError, match="timezone-aware"):
            resolve_market_daily_cut(
                storage,
                asset_id=asset_id,
                source_id=source_id,
                market_start=date(2026, 7, 7),
                market_end=date(2026, 7, 9),
                requested_end=date(2026, 7, 9),
                refresh_mode=AaplRefreshMode.FULL,
                fetch_created_inputs=False,
                effective_known_at=naive_clock,
            )

        # 4. Rejected in fallback / AUTO mode without inputs
        with pytest.raises(MarketDailyCutError, match="timezone-aware"):
            resolve_market_daily_cut(
                storage,
                asset_id=asset_id,
                source_id=source_id,
                market_start=date(2026, 7, 7),
                market_end=date(2026, 7, 9),
                requested_end=date(2026, 7, 9),
                refresh_mode=AaplRefreshMode.AUTO,
                fetch_created_inputs=False,
                effective_known_at=naive_clock,
            )
