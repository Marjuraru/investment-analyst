"""Compose catalog-driven local scheduler jobs over the shared controller mutex."""

from datetime import date, datetime, time, timedelta
from typing import Protocol

from pydantic import ConfigDict, field_validator, model_validator

from investment_analyst.application.aapl_bootstrap_models import AaplRefreshMode
from investment_analyst.application.btc_intraday import BtcIntradayRefreshError
from investment_analyst.application.btc_intraday_models import (
    BtcIntradayRefreshRequest,
    BtcIntradayRefreshSummary,
)
from investment_analyst.application.btc_refresh import BtcMarketRefreshError
from investment_analyst.application.btc_refresh_models import (
    BtcMarketRefreshRequest,
    BtcMarketRefreshSummary,
    BtcRefreshMode,
)
from investment_analyst.application.listed_market_refresh import ListedMarketRefreshError
from investment_analyst.application.listed_market_refresh_models import (
    ListedMarketRefreshRequest,
    ListedMarketRefreshSummary,
)
from investment_analyst.application.market_universe import (
    MarketAssetDescriptor,
    MarketAssetUniverse,
)
from investment_analyst.application.multi_asset_scheduler import (
    RegisteredScheduledJob,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobFailure,
    ScheduledJobInvocation,
    ScheduledJobRunError,
)
from investment_analyst.application.peru_registry import (
    BvlRegistryRefreshRequest,
    BvlRegistryRefreshSummary,
)
from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalRefreshError,
)
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshSummary,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.providers.macro.fred_alfred import FredAlfredError
from investment_analyst.providers.macro.fred_catalog import (
    FRED_SERIES_CATALOG,
    FredSeriesCatalogEntry,
)
from investment_analyst.providers.macro.fred_catalog_refresh import (
    FredCatalogRefreshRequest,
    FredCatalogRefreshSummary,
)
from investment_analyst.providers.peru.smv_open_data import SmvOpenDataError
from investment_analyst.storage import StorageError


class _LocalScheduledOperations(Protocol):
    def listed_market_refresh_request(
        self,
        request: ListedMarketRefreshRequest,
    ) -> ListedMarketRefreshSummary:
        """Refresh one Alpaca market asset."""
        ...

    def btc_market_refresh_request(
        self,
        request: BtcMarketRefreshRequest,
    ) -> BtcMarketRefreshSummary:
        """Refresh the Coinbase daily market source."""
        ...

    def btc_intraday_refresh_request(
        self,
        request: BtcIntradayRefreshRequest,
    ) -> BtcIntradayRefreshSummary:
        """Refresh the bounded Coinbase minute source."""
        ...

    def sec_fundamental_refresh_request(
        self,
        request: SecIssuerFundamentalRefreshRequest,
    ) -> SecIssuerFundamentalRefreshSummary:
        """Refresh one SEC issuer."""
        ...

    def fred_catalog_refresh_request(
        self,
        request: FredCatalogRefreshRequest,
    ) -> FredCatalogRefreshSummary:
        """Refresh one bounded FRED catalog series."""
        ...

    def bvl_registry_refresh_request(
        self,
        request: BvlRegistryRefreshRequest,
    ) -> BvlRegistryRefreshSummary:
        """Refresh official SMV registry evidence."""
        ...


class LocalWatchlistScheduleConfig(ContractModel):
    """Explicit scope shared by every catalog-derived scheduled job."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    timezone: NonEmptyStr = "America/Lima"
    run_at: time = time(hour=7)
    market_start: date
    market_end_lag_days: int = 1
    fundamental_frequency: DataFrequency = DataFrequency.QUARTERLY
    refresh_mode: AaplRefreshMode = AaplRefreshMode.AUTO
    selected_asset_ids: tuple[NonEmptyStr, ...] = ()
    include_intraday: bool = True
    include_smv_registry: bool = False
    include_macro: bool = False

    @field_validator("market_start", mode="before")
    @classmethod
    def require_market_start(cls, value: object) -> object:
        """Accept ISO dates while rejecting datetimes."""
        if isinstance(value, datetime):
            raise ValueError("market_start must be a date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("market_start must use YYYY-MM-DD") from error
        if not isinstance(value, date):
            raise ValueError("market_start must be a date")
        return value

    @field_validator("run_at", mode="before")
    @classmethod
    def require_local_minute(cls, value: object) -> object:
        """Accept a whole timezone-naive minute."""
        if isinstance(value, str):
            try:
                value = time.fromisoformat(value)
            except ValueError as error:
                raise ValueError("run_at must use HH:MM") from error
        if not isinstance(value, time):
            raise ValueError("run_at must be a time")
        if value.tzinfo is not None or value.second != 0 or value.microsecond != 0:
            raise ValueError("run_at must be a timezone-naive whole minute")
        return value

    @field_validator("market_end_lag_days", mode="before")
    @classmethod
    def validate_lag(cls, value: object) -> object:
        """Bound the daily completed-market lag."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("market_end_lag_days must be an integer")
        if not 0 <= value <= 30:
            raise ValueError("market_end_lag_days must be between 0 and 30")
        return value

    @field_validator(
        "include_intraday",
        "include_smv_registry",
        "include_macro",
        mode="before",
    )
    @classmethod
    def require_intraday_boolean(cls, value: object) -> object:
        """Reject ambiguous truthy values."""
        if not isinstance(value, bool):
            raise ValueError("schedule inclusion flags must be bool")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> "LocalWatchlistScheduleConfig":
        """Require a deterministic optional asset selection."""
        if self.selected_asset_ids != tuple(sorted(set(self.selected_asset_ids))):
            raise ValueError("selected_asset_ids must be unique and sorted")
        if self.fundamental_frequency not in {
            DataFrequency.ANNUAL,
            DataFrequency.QUARTERLY,
        }:
            raise ValueError("fundamental_frequency must be annual or quarterly")
        return self


def build_local_watchlist_jobs(
    controller: _LocalScheduledOperations,
    universe: MarketAssetUniverse,
    config: LocalWatchlistScheduleConfig,
) -> tuple[RegisteredScheduledJob, ...]:
    """Build provider/domain jobs without symbol-specific application routes."""
    known_ids = {item.asset_id for item in universe.assets}
    requested = set(config.selected_asset_ids) if config.selected_asset_ids else known_ids
    unknown = requested - known_ids
    if unknown:
        raise ValueError(f"scheduled asset_id is not supported: {sorted(unknown)[0]}")
    descriptors = tuple(item for item in universe.assets if item.asset_id in requested)
    jobs: list[RegisteredScheduledJob] = []
    for descriptor in descriptors:
        jobs.append(_market_job(controller, descriptor, config))
        if descriptor.has_fundamentals:
            jobs.append(_fundamental_job(controller, descriptor, config))
        if descriptor.supports_intraday and config.include_intraday:
            jobs.append(_intraday_job(controller, descriptor, config))
    if config.include_smv_registry:
        jobs.append(_smv_registry_job(controller, config))
    if config.include_macro:
        for index, entry in enumerate(FRED_SERIES_CATALOG.automated_entries()):
            jobs.append(_fred_catalog_job(controller, entry, index, config))
    if not jobs:
        raise ValueError("scheduled watchlist must produce at least one job")
    return tuple(sorted(jobs, key=lambda item: item.definition.job_id))


def _smv_registry_job(
    controller: _LocalScheduledOperations,
    config: LocalWatchlistScheduleConfig,
) -> RegisteredScheduledJob:
    definition = ScheduledJobDefinition(
        job_id="smv:bvl:registry",
        provider="smv-open-data",
        domain=ScheduledJobDomain.CATALOG,
        data_frequency="daily-check",
        timezone=config.timezone,
        run_at=_offset_minute(config.run_at, 45),
        freshness_threshold_seconds=604_800,
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        del invocation
        try:
            summary = controller.bvl_registry_refresh_request(BvlRegistryRefreshRequest())
        except (SmvOpenDataError, StorageError, ValueError, OSError) as error:
            raise _retryable_provider_error(error) from error
        source_ids = tuple(
            sorted(
                {
                    source_id
                    for asset in summary.assets
                    for source_id in (
                        asset.registry.company.source_id,
                        asset.registry.securities.source_id,
                    )
                }
            )
        )
        if not source_ids or not summary.assets:
            raise ScheduledJobRunError(
                ScheduledJobFailure(
                    category="empty_smv_registry_scope",
                    message="scheduled SMV registry refresh returned no configured evidence",
                    retryable=False,
                )
            )
        checked_at = max(
            max(
                asset.registry.company.retrieved_at,
                asset.registry.securities.retrieved_at,
            )
            for asset in summary.assets
        )
        return ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=checked_at,
            evidence_changed=summary.raw_records_created > 0,
            source_ids=source_ids,
            created_count=summary.raw_records_created,
            reused_count=summary.raw_records_reused,
        )

    return RegisteredScheduledJob(definition, run)


def _fred_catalog_job(
    controller: _LocalScheduledOperations,
    entry: FredSeriesCatalogEntry,
    index: int,
    config: LocalWatchlistScheduleConfig,
) -> RegisteredScheduledJob:
    definition = ScheduledJobDefinition(
        job_id=f"fred-alfred:{entry.series_id}:macro-vintages",
        provider="fred-alfred",
        domain=ScheduledJobDomain.MACRO,
        data_frequency=entry.data_frequency,
        timezone=config.timezone,
        run_at=_offset_minute(config.run_at, 60 + (index * 5)),
        freshness_threshold_seconds=entry.freshness_threshold_seconds,
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        try:
            summary = controller.fred_catalog_refresh_request(
                FredCatalogRefreshRequest(
                    series_id=entry.series_id,
                    run_date=invocation.local_date,
                )
            )
        except (FredAlfredError, StorageError, ValueError, OSError) as error:
            raise _retryable_provider_error(error) from error
        return ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=summary.checked_at,
            evidence_changed=summary.raw_records_created > 0,
            source_ids=(summary.source_id,),
            created_count=summary.raw_records_created,
            reused_count=summary.raw_records_reused,
            coverage_complete=summary.update_coverage_complete,
        )

    return RegisteredScheduledJob(definition, run)


def _market_job(
    controller: _LocalScheduledOperations,
    descriptor: MarketAssetDescriptor,
    config: LocalWatchlistScheduleConfig,
) -> RegisteredScheduledJob:
    definition = ScheduledJobDefinition(
        job_id=f"{descriptor.provider}:{descriptor.asset_id}:market-daily",
        asset_id=descriptor.asset_id,
        provider=descriptor.provider,
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone=config.timezone,
        run_at=config.run_at,
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        market_end = invocation.local_date - timedelta(days=config.market_end_lag_days)
        market_start = max(config.market_start, descriptor.default_market_start)
        if market_end < market_start:
            raise ScheduledJobRunError(
                ScheduledJobFailure(
                    category="invalid_market_range",
                    message="scheduled market end would be earlier than market start",
                    retryable=False,
                )
            )
        try:
            if descriptor.provider == "alpaca":
                summary = controller.listed_market_refresh_request(
                    ListedMarketRefreshRequest(
                        asset_id=descriptor.asset_id,
                        market_start=market_start,
                        market_end=market_end,
                        refresh_mode=config.refresh_mode,
                    )
                )
                return _listed_market_execution(definition.job_id, summary)
            if descriptor.provider == "coinbase":
                summary = controller.btc_market_refresh_request(
                    BtcMarketRefreshRequest(
                        asset_id=descriptor.asset_id,
                        market_start=market_start,
                        market_end=market_end,
                        refresh_mode=(
                            BtcRefreshMode.FULL
                            if config.refresh_mode is AaplRefreshMode.FULL
                            else BtcRefreshMode.AUTO
                        ),
                    )
                )
                return _btc_market_execution(definition.job_id, summary)
        except (ListedMarketRefreshError, BtcMarketRefreshError) as error:
            raise _retryable_provider_error(error) from error
        raise ScheduledJobRunError(
            ScheduledJobFailure(
                category="unsupported_market_provider",
                message="scheduled market provider is not supported",
                retryable=False,
            )
        )

    return RegisteredScheduledJob(definition, run)


def _fundamental_job(
    controller: _LocalScheduledOperations,
    descriptor: MarketAssetDescriptor,
    config: LocalWatchlistScheduleConfig,
) -> RegisteredScheduledJob:
    frequency = (
        config.fundamental_frequency
        if config.fundamental_frequency in descriptor.fundamental_frequencies
        else descriptor.fundamental_frequencies[0]
    )
    definition = ScheduledJobDefinition(
        job_id=f"sec:{descriptor.asset_id}:fundamentals-{frequency.value}",
        asset_id=descriptor.asset_id,
        provider="sec-edgar",
        domain=ScheduledJobDomain.FUNDAMENTALS,
        data_frequency=frequency.value,
        timezone=config.timezone,
        run_at=_offset_minute(config.run_at, 15),
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        del invocation
        try:
            summary = controller.sec_fundamental_refresh_request(
                SecIssuerFundamentalRefreshRequest(
                    asset_id=descriptor.asset_id,
                    frequency=frequency,
                )
            )
        except SecIssuerFundamentalRefreshError as error:
            raise _retryable_provider_error(error) from error
        created = (
            summary.raw_records_created
            + summary.observations_created
            + summary.metric_results_created
            + summary.diagnostics_created
        )
        reused = (
            summary.raw_records_reused
            + summary.observations_reused
            + summary.metric_results_reused
            + summary.diagnostics_reused
        )
        return ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=summary.effective_known_at,
            evidence_changed=created > 0,
            source_ids=(summary.source_id,),
            created_count=created,
            reused_count=reused,
        )

    return RegisteredScheduledJob(definition, run)


def _intraday_job(
    controller: _LocalScheduledOperations,
    descriptor: MarketAssetDescriptor,
    config: LocalWatchlistScheduleConfig,
) -> RegisteredScheduledJob:
    if descriptor.intraday_source_id is None:
        raise ValueError("intraday descriptor is missing its source identity")
    definition = ScheduledJobDefinition(
        job_id=f"{descriptor.provider}:{descriptor.asset_id}:market-intraday",
        asset_id=descriptor.asset_id,
        provider=descriptor.provider,
        domain=ScheduledJobDomain.MARKET_INTRADAY,
        data_frequency="minute_1",
        timezone=config.timezone,
        run_at=_offset_minute(config.run_at, 30),
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        requested_end = invocation.started_at.replace(second=0, microsecond=0)
        try:
            summary = controller.btc_intraday_refresh_request(
                BtcIntradayRefreshRequest(
                    asset_id=descriptor.asset_id,
                    requested_end=requested_end,
                )
            )
        except BtcIntradayRefreshError as error:
            raise _retryable_provider_error(error) from error
        created = summary.raw_records_created + summary.observations_created
        reused = summary.raw_records_reused + summary.observations_reused
        return ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=summary.retrieved_at,
            evidence_changed=created > 0,
            source_ids=(summary.source_id,),
            created_count=created,
            reused_count=reused,
        )

    return RegisteredScheduledJob(definition, run)


def _listed_market_execution(
    job_id: str,
    summary: ListedMarketRefreshSummary,
) -> ScheduledJobExecution:
    created = (
        summary.raw_records_created
        + summary.observations_created
        + summary.coverage_receipts_created
        + summary.metric_results_created
        + summary.diagnostics_created
    )
    reused = (
        summary.raw_records_reused
        + summary.observations_reused
        + summary.coverage_receipts_reused
        + summary.metric_results_reused
        + summary.diagnostics_reused
    )
    return ScheduledJobExecution(
        job_id=job_id,
        effective_known_at=summary.effective_known_at,
        evidence_changed=created > 0,
        source_ids=(summary.source_id,),
        created_count=created,
        reused_count=reused,
    )


def _btc_market_execution(
    job_id: str,
    summary: BtcMarketRefreshSummary,
) -> ScheduledJobExecution:
    created = (
        summary.raw_records_created
        + summary.observations_created
        + summary.metric_results_created
        + summary.diagnostics_created
    )
    reused = (
        summary.raw_records_reused
        + summary.observations_reused
        + summary.metric_results_reused
        + summary.diagnostics_reused
    )
    return ScheduledJobExecution(
        job_id=job_id,
        effective_known_at=summary.effective_known_at,
        evidence_changed=created > 0,
        source_ids=(summary.source_id,),
        created_count=created,
        reused_count=reused,
    )


def _retryable_provider_error(error: Exception) -> ScheduledJobRunError:
    message = str(error).strip()[:500] or "scheduled provider refresh failed"
    return ScheduledJobRunError(
        ScheduledJobFailure(
            category=type(error).__name__,
            message=message,
            retryable=True,
        )
    )


def _offset_minute(value: time, minutes: int) -> time:
    anchor = datetime.combine(date(2000, 1, 1), value)
    return (anchor + timedelta(minutes=minutes)).time()


__all__ = [
    "LocalWatchlistScheduleConfig",
    "build_local_watchlist_jobs",
]
