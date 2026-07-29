"""Tests for catalog-driven local watchlist scheduling composition."""

from datetime import date, time

import pytest

from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.multi_asset_scheduler import ScheduledJobDomain
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.frontend.local_schedule_jobs import (
    LocalWatchlistScheduleConfig,
    build_local_watchlist_jobs,
)
from investment_analyst.providers.macro.fred_catalog import FRED_SERIES_CATALOG


class _UnusedController:
    def listed_market_refresh_request(self, request):
        raise AssertionError(request)

    def btc_market_refresh_request(self, request):
        raise AssertionError(request)

    def btc_intraday_refresh_request(self, request):
        raise AssertionError(request)

    def sec_fundamental_refresh_request(self, request):
        raise AssertionError(request)

    def fred_catalog_refresh_request(self, request):
        raise AssertionError(request)

    def bvl_registry_refresh_request(self, request):
        raise AssertionError(request)


def _universe():
    return InvestmentAnalystApplication(ApplicationRuntime.create_default()).list_market_assets()


def _config(*asset_ids: str) -> LocalWatchlistScheduleConfig:
    return LocalWatchlistScheduleConfig(
        timezone="America/Lima",
        run_at=time(hour=7),
        market_start=date(2025, 1, 1),
        selected_asset_ids=tuple(sorted(asset_ids)),
    )


def test_watchlist_jobs_are_derived_by_capability_not_symbol() -> None:
    universe = _universe()

    jobs = build_local_watchlist_jobs(_UnusedController(), universe, _config())

    expected = (
        len(universe.assets)
        + sum(item.has_fundamentals for item in universe.assets)
        + sum(item.supports_intraday for item in universe.assets)
    )
    assert len(jobs) == expected
    assert tuple(item.definition.job_id for item in jobs) == tuple(
        sorted(item.definition.job_id for item in jobs)
    )
    assert len({item.definition.job_id for item in jobs}) == len(jobs)
    assert {item.definition.domain for item in jobs} >= {
        ScheduledJobDomain.MARKET_DAILY,
        ScheduledJobDomain.MARKET_INTRADAY,
        ScheduledJobDomain.FUNDAMENTALS,
    }


def test_selected_equity_and_crypto_receive_only_compatible_jobs() -> None:
    universe = _universe()

    equity_jobs = build_local_watchlist_jobs(
        _UnusedController(),
        universe,
        _config("equity:us:tsm"),
    )
    crypto_jobs = build_local_watchlist_jobs(
        _UnusedController(),
        universe,
        _config("crypto:btc-usd"),
    )

    assert {item.definition.domain for item in equity_jobs} == {
        ScheduledJobDomain.MARKET_DAILY,
        ScheduledJobDomain.FUNDAMENTALS,
    }
    fundamental = next(
        item for item in equity_jobs if item.definition.domain is ScheduledJobDomain.FUNDAMENTALS
    )
    assert fundamental.definition.data_frequency == "annual"
    assert {item.definition.domain for item in crypto_jobs} == {
        ScheduledJobDomain.MARKET_DAILY,
        ScheduledJobDomain.MARKET_INTRADAY,
    }


def test_selected_asset_must_exist_in_visible_catalog() -> None:
    with pytest.raises(ValueError, match="not supported"):
        build_local_watchlist_jobs(
            _UnusedController(),
            _universe(),
            _config("equity:unknown:nope"),
        )


def test_optional_macro_and_smv_jobs_have_independent_provider_scopes() -> None:
    config = _config("equity:us:aapl").model_copy(
        update={"include_smv_registry": True, "include_macro": True}
    )

    jobs = build_local_watchlist_jobs(_UnusedController(), _universe(), config)
    infrastructure = tuple(
        item
        for item in jobs
        if item.definition.domain in {ScheduledJobDomain.CATALOG, ScheduledJobDomain.MACRO}
    )

    assert len(infrastructure) == 1 + len(FRED_SERIES_CATALOG.automated_entries())
    assert {item.definition.provider for item in infrastructure} == {
        "fred-alfred",
        "smv-open-data",
    }
    assert all(item.definition.asset_id is None for item in infrastructure)
