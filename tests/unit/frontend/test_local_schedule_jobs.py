"""Tests for catalog-driven local watchlist scheduling composition."""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from uuid import UUID

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
    ScheduledJobRunError,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.application.sec_declared_activity_refresh import (
    SecDeclaredActivityRefreshError,
)
from investment_analyst.application.sec_declared_activity_refresh_models import (
    SecDeclaredActivityFamilySummary,
    SecDeclaredActivityRefreshRequest,
    SecDeclaredActivityRefreshSummary,
)
from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalKnownAtTooEarlyError,
)
from investment_analyst.application.sec_institutional_cycle import (
    SecInstitutionalCycleError,
)
from investment_analyst.application.sec_institutional_cycle_models import (
    SecInstitutionalCycleSummary,
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

    def sec_primary_document_refresh_request(self, request):
        raise AssertionError(request)

    def sec_declared_activity_refresh_request(self, request):
        raise AssertionError(request)

    def fred_catalog_refresh_request(self, request):
        raise AssertionError(request)

    def bvl_registry_refresh_request(self, request):
        raise AssertionError(request)

    def sec_institutional_cycle_request(self, request):
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
        + (3 * sum(item.has_fundamentals for item in universe.assets))
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
        ScheduledJobDomain.EVENTS,
    }
    fundamental = next(
        item for item in equity_jobs if item.definition.domain is ScheduledJobDomain.FUNDAMENTALS
    )
    assert fundamental.definition.data_frequency == "annual"
    by_id = {item.definition.job_id: item for item in equity_jobs}
    documents = by_id["sec:equity:us:tsm:primary-documents"]
    assert documents.definition.data_frequency == "daily-check"
    assert documents.definition.run_at == time(hour=8)
    activity = by_id["sec:equity:us:tsm:declared-activity"]
    assert activity.definition.provider == "sec-edgar"
    assert activity.definition.domain is ScheduledJobDomain.EVENTS
    assert activity.definition.data_frequency == "daily-check"
    assert activity.definition.asset_id == "equity:us:tsm"
    assert activity.definition.timezone == "America/Lima"
    assert activity.definition.run_at == time(hour=8, minute=15)
    assert activity.definition.run_at > documents.definition.run_at
    assert {item.definition.domain for item in crypto_jobs} == {
        ScheduledJobDomain.MARKET_DAILY,
        ScheduledJobDomain.MARKET_INTRADAY,
    }
    crypto_job_ids = {item.definition.job_id for item in crypto_jobs}
    assert not any(job_id.endswith(":declared-activity") for job_id in crypto_job_ids)


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


def _family_summary(
    *,
    family: str,
    source_id: str,
    accessions_imported: tuple[str, ...],
    accessions_reused: tuple[str, ...],
) -> SecDeclaredActivityFamilySummary:
    return SecDeclaredActivityFamilySummary(
        family=family,
        source_id=source_id,
        forms_evaluated=("3", "4", "5") if family == "insider" else ("SC 13D", "SC 13G"),
        forms_missing=(),
        accessions_selected=(*accessions_imported, *accessions_reused),
        accessions_imported=accessions_imported,
        accessions_reused=accessions_reused,
        accessions_rejected=(),
        accessions_incomplete=(),
        backlog_count=0,
        statements_created=len(accessions_imported),
        statements_reused=len(accessions_reused),
    )


def _declared_activity_summary(*, created: bool) -> SecDeclaredActivityRefreshSummary:
    insider = _family_summary(
        family="insider",
        source_id="sec-edgar:section16-ownership",
        accessions_imported=("0000320193-25-000001",) if created else (),
        accessions_reused=() if created else ("0000320193-25-000001",),
    )
    beneficial = _family_summary(
        family="beneficial",
        source_id="sec-edgar:beneficial-ownership-13d-13g",
        accessions_imported=(),
        accessions_reused=(),
    )
    return SecDeclaredActivityRefreshSummary(
        asset_id="equity:us:tsm",
        request=SecDeclaredActivityRefreshRequest(asset_id="equity:us:tsm"),
        submissions_source_id="sec-edgar:tsm:submissions",
        submissions_raw_record_id="f0e0e5f4-6b1f-4d5b-9a4d-6b1f4d5b9a4d",
        submissions_checked_at=datetime(2026, 8, 11, 13, 15, tzinfo=UTC),
        submissions_record_available_at=datetime(2026, 8, 11, 13, 15, tzinfo=UTC),
        submissions_created=1 if created else 0,
        submissions_reused=0 if created else 1,
        insider=insider,
        beneficial=beneficial,
        observations_created=2 if created else 0,
        observations_reused=0 if created else 3,
        observations_skipped=0,
        metrics_created=1 if created else 0,
        metrics_reused=0 if created else 1,
        metrics_skipped=0,
        backlog_count=0,
        coverage_complete=True,
        traceability_verified=True,
    )


def test_declared_activity_job_publishes_separated_source_counts_and_cut() -> None:
    class _Controller(_UnusedController):
        def __init__(self, summary: SecDeclaredActivityRefreshSummary) -> None:
            self.requests: list[SecDeclaredActivityRefreshRequest] = []
            self._summary = summary

        def sec_declared_activity_refresh_request(self, request):
            self.requests.append(request)
            return self._summary

    controller = _Controller(_declared_activity_summary(created=True))
    jobs = build_local_watchlist_jobs(controller, _universe(), _config("equity:us:tsm"))
    job = next(
        item for item in jobs if item.definition.job_id == "sec:equity:us:tsm:declared-activity"
    )
    invocation = ScheduledJobInvocation(
        definition=job.definition,
        local_date=date(2026, 8, 11),
        scheduled_for=job.definition.scheduled_for(date(2026, 8, 11)),
        started_at=datetime(2026, 8, 11, 13, 15, tzinfo=UTC),
        attempt_number=1,
    )

    execution = job.run(invocation)

    assert controller.requests == [SecDeclaredActivityRefreshRequest(asset_id="equity:us:tsm")]
    assert execution.effective_known_at == datetime(2026, 8, 11, 13, 15, tzinfo=UTC)
    assert execution.source_ids == (
        "sec-edgar:beneficial-ownership-13d-13g",
        "sec-edgar:section16-ownership",
        "sec-edgar:tsm:submissions",
    )
    assert execution.created_count == 1 + 1 + 2 + 1
    assert execution.reused_count == 0
    assert execution.evidence_changed is True
    assert execution.coverage_complete is True

    controller._summary = _declared_activity_summary(created=False)
    unchanged = job.run(invocation)

    assert unchanged.created_count == 0
    assert unchanged.reused_count == 1 + 0 + 1 + 3 + 1
    assert unchanged.evidence_changed is False


def test_declared_activity_job_reports_a_typed_non_retryable_failure() -> None:
    class _Controller(_UnusedController):
        def sec_declared_activity_refresh_request(self, request):
            raise SecDeclaredActivityRefreshError("refresh contract simulated-secret")

    jobs = build_local_watchlist_jobs(_Controller(), _universe(), _config("equity:us:tsm"))
    job = next(
        item for item in jobs if item.definition.job_id == "sec:equity:us:tsm:declared-activity"
    )
    invocation = ScheduledJobInvocation(
        definition=job.definition,
        local_date=date(2026, 8, 11),
        scheduled_for=job.definition.scheduled_for(date(2026, 8, 11)),
        started_at=datetime(2026, 8, 11, 13, 15, tzinfo=UTC),
        attempt_number=1,
    )

    with pytest.raises(ScheduledJobRunError) as error:
        job.run(invocation)

    failure = error.value.failure
    assert failure.category == ScheduledJobFailureCategory.PROVIDER_CONTRACT
    assert failure.retryable is False
    assert "simulated-secret" not in failure.message


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


def test_sec_institutional_cycle_job_composition() -> None:
    config_without = _config("equity:us:aapl")
    jobs_without = build_local_watchlist_jobs(_UnusedController(), _universe(), config_without)
    assert not any(item.definition.job_id == "sec:institutional:13f-cycle" for item in jobs_without)

    config_with = _config("equity:us:aapl").model_copy(
        update={"sec_cusip_asset_ids": ("equity:us:aapl",)}
    )
    jobs_with = build_local_watchlist_jobs(_UnusedController(), _universe(), config_with)
    cycle_jobs = [
        item for item in jobs_with if item.definition.job_id == "sec:institutional:13f-cycle"
    ]
    assert len(cycle_jobs) == 1
    job = cycle_jobs[0]
    assert job.definition.asset_id is None
    assert job.definition.provider == "sec-edgar"
    assert job.definition.domain is ScheduledJobDomain.EVENTS
    assert job.definition.data_frequency == "daily-check"
    assert job.definition.run_at == time(hour=8, minute=45)


def test_sec_institutional_cycle_job_run_success() -> None:
    class _CycleSuccessController(_UnusedController):
        def sec_institutional_cycle_request(self, request):
            assert request.known_at == datetime(2026, 8, 11, 13, 45, tzinfo=UTC)
            return SecInstitutionalCycleSummary(
                schema_version="sec-institutional-scheduled-cycle-v1",
                policy_version="sec-institutional-cycle-policy-v1",
                effective_known_at=datetime(2026, 8, 11, 13, 45, tzinfo=UTC),
                status="processed",
                reason_code=None,
                catalog_calls=1,
                zip_calls=0,
                submissions_calls=1,
                archives_calls=2,
                dataset_period_start=date(2026, 3, 1),
                dataset_period_end=date(2026, 5, 31),
                dataset_url="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip",
                dataset_sha256="a" * 64,
                snapshot_id=UUID("ddaf3ca6-d25a-5073-a10e-762c47abaf6d"),
                manager_cursor_before=0,
                manager_cursor_after=1,
                total_managers=2,
                coverage_complete=False,
                manager_cik="0001067983",
                manager_name="BERKSHIRE HATHAWAY INC",
                report_period=date(2026, 3, 31),
                created_accessions=("0001067983-26-000010",),
                reused_accessions=(),
                rejected_accessions=(),
                failed_accessions=(),
                backlog_after=0,
                observations_created=1,
                observations_reused=0,
                traceability_verified=True,
                source_ids=(
                    "sec-edgar:form-13f-data-sets",
                    "sec-edgar:institutional-holdings-13f",
                    "sec-edgar:institutional-holdings-observations",
                ),
            )

    config = _config("equity:us:aapl").model_copy(
        update={"sec_cusip_asset_ids": ("equity:us:aapl",)}
    )
    jobs = build_local_watchlist_jobs(_CycleSuccessController(), _universe(), config)
    job = next(item for item in jobs if item.definition.job_id == "sec:institutional:13f-cycle")
    invocation = ScheduledJobInvocation(
        definition=job.definition,
        local_date=date(2026, 8, 11),
        scheduled_for=job.definition.scheduled_for(date(2026, 8, 11)),
        started_at=datetime(2026, 8, 11, 13, 45, tzinfo=UTC),
        attempt_number=1,
    )
    execution = job.run(invocation)
    assert execution.job_id == "sec:institutional:13f-cycle"
    assert execution.evidence_changed is True
    assert execution.created_count == 2
    assert execution.coverage_complete is False


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
            SecInstitutionalCycleError("malformed cycle simulated-secret"),
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
