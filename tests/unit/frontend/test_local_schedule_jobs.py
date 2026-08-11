"""Tests for catalog-driven local watchlist scheduling composition."""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import pytest

import investment_analyst.frontend.local_schedule_jobs as schedule_jobs_module
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.listed_market_refresh import (
    ListedMarketKnownAtTooEarlyError,
    ListedMarketRefreshError,
)
from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobDomain,
    ScheduledJobFailureCategory,
    ScheduledJobInvocation,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalKnownAtTooEarlyError,
)
from investment_analyst.frontend.local_schedule_jobs import (
    LocalWatchlistScheduleConfig,
    build_local_watchlist_jobs,
)
from investment_analyst.providers.asset_config import ProviderConfigurationError
from investment_analyst.providers.crypto.deribit import DeribitError
from investment_analyst.providers.http import HttpRequestError
from investment_analyst.providers.macro.fred_alfred import FredAlfredError
from investment_analyst.providers.macro.fred_catalog import FRED_SERIES_CATALOG
from investment_analyst.providers.market.alpaca_stock import AlpacaStockError
from investment_analyst.providers.peru.smv_open_data import SmvOpenDataNotFoundError
from investment_analyst.storage import StorageError


class _UnusedController:
    def listed_market_refresh_request(self, request):
        raise AssertionError(request)

    def btc_market_refresh_request(self, request):
        raise AssertionError(request)

    def btc_intraday_refresh_request(self, request):
        raise AssertionError(request)

    def crypto_spot_daily_refresh_request(self, request):
        raise AssertionError(request)

    def crypto_derivatives_refresh_request(self, request):
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


def test_derivatives_job_is_capability_opt_in_offset_and_requests_rolling_90_days() -> None:
    class _Controller(_UnusedController):
        def __init__(self) -> None:
            self.request = None

        def crypto_derivatives_refresh_request(self, request):
            self.request = request
            return SimpleNamespace(
                effective_known_at=datetime(2026, 8, 11, 12, 11, tzinfo=UTC),
                created_count=7,
                reused_count=3,
                source_ids=(
                    "deribit:btc-perpetual:book-summary",
                    "deribit:btc-perpetual:funding-rate-history",
                    "deribit:btc:dvol:daily",
                ),
                traceability_verified=True,
            )

    controller = _Controller()
    config = _config("crypto:btc-usd").model_copy(
        update={"crypto_derivatives_asset_ids": ("crypto:btc-usd", "crypto:eth-usd")}
    )
    jobs = build_local_watchlist_jobs(controller, _universe(), config)
    job = next(
        item for item in jobs if item.definition.domain is ScheduledJobDomain.CRYPTO_DERIVATIVES
    )
    invocation = ScheduledJobInvocation(
        definition=job.definition,
        local_date=date(2026, 8, 11),
        scheduled_for=job.definition.scheduled_for(date(2026, 8, 11)),
        started_at=datetime(2026, 8, 11, 12, 10, tzinfo=UTC),
        attempt_number=1,
    )

    execution = job.run(invocation)

    assert job.definition.job_id == "deribit:crypto:btc-usd:crypto-derivatives"
    assert job.definition.run_at == time(hour=7, minute=10)
    assert job.definition.freshness_threshold_seconds == 129_600
    assert controller.request.start_date == date(2026, 5, 13)
    assert controller.request.end_date == date(2026, 8, 10)
    assert controller.request.refresh_mode.value == "auto"
    assert execution.created_count == 7
    assert execution.reused_count == 3
    assert execution.coverage_complete is True


def test_selected_asset_must_exist_in_visible_catalog() -> None:
    with pytest.raises(ValueError, match="not supported"):
        build_local_watchlist_jobs(
            _UnusedController(),
            _universe(),
            _config("equity:unknown:nope"),
        )


def test_explicit_empty_selection_does_not_fall_back_to_every_catalog_asset() -> None:
    config = _config().model_copy(update={"selection_is_explicit": True})

    with pytest.raises(ValueError, match="at least one job"):
        build_local_watchlist_jobs(
            _UnusedController(),
            _universe(),
            config,
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


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (
            ProviderConfigurationError("invalid credential simulated-secret"),
            ScheduledJobFailureCategory.CONFIGURATION,
            False,
        ),
        (
            HttpRequestError(
                "https://provider.test/data?api_key=simulated-secret",
                "denied",
                status_code=401,
            ),
            ScheduledJobFailureCategory.AUTHENTICATION,
            False,
        ),
        (
            AlpacaStockError("forbidden simulated-secret", status_code=403),
            ScheduledJobFailureCategory.AUTHENTICATION,
            False,
        ),
        (
            DeribitError("limited simulated-secret", status_code=429),
            ScheduledJobFailureCategory.RATE_LIMIT,
            True,
        ),
        (
            SmvOpenDataNotFoundError("unsupported simulated-secret"),
            ScheduledJobFailureCategory.UNSUPPORTED_CAPABILITY,
            False,
        ),
        (
            FredAlfredError("malformed payload simulated-secret"),
            ScheduledJobFailureCategory.PROVIDER_CONTRACT,
            False,
        ),
        (
            ListedMarketKnownAtTooEarlyError("invalid cut simulated-secret"),
            ScheduledJobFailureCategory.VALIDATION,
            False,
        ),
        (
            SecIssuerFundamentalKnownAtTooEarlyError(
                requested_known_at=datetime(2026, 1, 1, tzinfo=UTC),
                minimum_known_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
            ScheduledJobFailureCategory.VALIDATION,
            False,
        ),
        (
            StorageError("manifest simulated-secret"),
            ScheduledJobFailureCategory.STORAGE_STATE,
            False,
        ),
        (
            HttpRequestError(
                "https://provider.test/data?api_key=simulated-secret",
                "limited",
                status_code=429,
            ),
            ScheduledJobFailureCategory.RATE_LIMIT,
            True,
        ),
        (
            HttpRequestError(
                "https://provider.test/data?api_key=simulated-secret",
                "unavailable",
                status_code=503,
            ),
            ScheduledJobFailureCategory.TRANSIENT_HTTP,
            True,
        ),
        (
            HttpRequestError(
                "https://provider.test/data?api_key=simulated-secret",
                "timeout",
                cause=TimeoutError("simulated-secret"),
            ),
            ScheduledJobFailureCategory.TRANSPORT,
            True,
        ),
        (
            RuntimeError("unexpected simulated-secret"),
            ScheduledJobFailureCategory.UNEXPECTED,
            False,
        ),
    ],
)
def test_provider_failure_classification_is_structured_bounded_and_secret_safe(
    error: Exception,
    category: ScheduledJobFailureCategory,
    retryable: bool,
) -> None:
    failure = schedule_jobs_module._classified_provider_error(error).failure

    assert failure.category == category
    assert failure.retryable is retryable
    assert len(failure.message) <= 500
    assert "simulated-secret" not in failure.message


def test_provider_failure_classification_does_not_parse_free_text() -> None:
    failure = schedule_jobs_module._classified_provider_error(
        ListedMarketRefreshError("provider returned HTTP 503 with simulated-secret")
    ).failure

    assert failure.category == ScheduledJobFailureCategory.PROVIDER_CONTRACT
    assert failure.retryable is False
    assert "simulated-secret" not in failure.message
