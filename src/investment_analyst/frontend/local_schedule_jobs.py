"""Compose catalog-driven local scheduler jobs over the shared controller mutex."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from urllib.error import URLError

from pydantic import ConfigDict, ValidationError, field_validator, model_validator

from investment_analyst.application.aapl_bootstrap_models import AaplRefreshMode
from investment_analyst.application.btc_intraday import BtcIntradayRefreshError
from investment_analyst.application.btc_intraday_models import (
    BtcIntradayRefreshRequest,
    BtcIntradayRefreshSummary,
)
from investment_analyst.application.btc_refresh import (
    BtcMarketKnownAtTooEarlyError,
    BtcMarketRefreshError,
)
from investment_analyst.application.btc_refresh_models import (
    BtcMarketRefreshRequest,
    BtcMarketRefreshSummary,
    BtcRefreshMode,
)
from investment_analyst.application.crypto_derivatives import (
    CryptoDerivativesRefreshError,
)
from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesRefreshMode,
    CryptoDerivativesRefreshRequest,
    CryptoDerivativesRefreshSummary,
)
from investment_analyst.application.crypto_spot_daily import CryptoSpotDailyRefreshError
from investment_analyst.application.crypto_spot_daily_models import (
    CryptoSpotDailyRefreshRequest,
    CryptoSpotDailyRefreshSummary,
)
from investment_analyst.application.listed_market_refresh import (
    ListedMarketKnownAtTooEarlyError,
    ListedMarketRefreshError,
)
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
    ScheduledJobFailureCategory,
    ScheduledJobInvocation,
    ScheduledJobRunError,
    scheduled_job_failure,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.application.peru_registry import (
    BvlRegistryRefreshRequest,
    BvlRegistryRefreshSummary,
)
from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalKnownAtTooEarlyError,
    SecIssuerFundamentalRefreshError,
)
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshSummary,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.providers.asset_config import ProviderConfigurationError
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeError
from investment_analyst.providers.crypto.deribit import DeribitError
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarError
from investment_analyst.providers.http import (
    RETRYABLE_HTTP_STATUS_CODES,
    HttpRequestError,
    HttpRequestFailureKind,
)
from investment_analyst.providers.macro.fred_alfred import FredAlfredError
from investment_analyst.providers.macro.fred_catalog import (
    FRED_SERIES_CATALOG,
    FredSeriesCatalogEntry,
)
from investment_analyst.providers.macro.fred_catalog_refresh import (
    FredCatalogRefreshRequest,
    FredCatalogRefreshSummary,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockError
from investment_analyst.providers.peru.smv_open_data import (
    SmvOpenDataError,
    SmvOpenDataNotFoundError,
)
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError


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

    def crypto_spot_daily_refresh_request(
        self,
        request: CryptoSpotDailyRefreshRequest,
    ) -> CryptoSpotDailyRefreshSummary:
        """Refresh one catalog-scoped Coinbase daily source."""
        ...

    def crypto_derivatives_refresh_request(
        self,
        request: CryptoDerivativesRefreshRequest,
    ) -> CryptoDerivativesRefreshSummary:
        """Refresh one complete Deribit derivatives family."""
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
    selection_is_explicit: bool = False
    include_intraday: bool = True
    include_smv_registry: bool = False
    include_macro: bool = False
    crypto_derivatives_asset_ids: tuple[NonEmptyStr, ...] = ()

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
        "selection_is_explicit",
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
        if self.crypto_derivatives_asset_ids != tuple(
            sorted(set(self.crypto_derivatives_asset_ids))
        ):
            raise ValueError("crypto_derivatives_asset_ids must be unique and sorted")
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
    requested = (
        set(config.selected_asset_ids)
        if config.selection_is_explicit or config.selected_asset_ids
        else known_ids
    )
    unknown = requested - known_ids
    if unknown:
        raise ValueError(f"scheduled asset_id is not supported: {sorted(unknown)[0]}")
    descriptors = tuple(item for item in universe.assets if item.asset_id in requested)
    jobs: list[RegisteredScheduledJob] = []
    for descriptor in descriptors:
        jobs.append(_market_job(controller, descriptor, config))
        if descriptor.asset_id in config.crypto_derivatives_asset_ids:
            jobs.append(_crypto_derivatives_job(controller, descriptor, config))
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
            raise _classified_provider_error(error) from error
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
                scheduled_job_failure(
                    ScheduledJobFailureCategory.PROVIDER_CONTRACT,
                    "scheduled SMV registry refresh returned no configured evidence",
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
            raise _classified_provider_error(error) from error
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
                scheduled_job_failure(
                    ScheduledJobFailureCategory.VALIDATION,
                    "scheduled market end would be earlier than market start",
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
                mode = (
                    BtcRefreshMode.FULL
                    if config.refresh_mode is AaplRefreshMode.FULL
                    else BtcRefreshMode.AUTO
                )
                if descriptor.asset_id == "crypto:btc-usd":
                    summary = controller.btc_market_refresh_request(
                        BtcMarketRefreshRequest(
                            asset_id=descriptor.asset_id,
                            market_start=market_start,
                            market_end=market_end,
                            refresh_mode=mode,
                        )
                    )
                    return _btc_market_execution(definition.job_id, summary)
                summary = controller.crypto_spot_daily_refresh_request(
                    CryptoSpotDailyRefreshRequest(
                        asset_id=descriptor.asset_id,
                        market_start=market_start,
                        market_end=market_end,
                        refresh_mode=mode,
                    )
                )
                return _crypto_spot_daily_execution(definition.job_id, summary)
        except (
            ListedMarketRefreshError,
            BtcMarketRefreshError,
            CryptoSpotDailyRefreshError,
        ) as error:
            raise _classified_provider_error(error) from error
        raise ScheduledJobRunError(
            scheduled_job_failure(
                ScheduledJobFailureCategory.UNSUPPORTED_CAPABILITY,
                "scheduled market provider is not supported",
            )
        )

    return RegisteredScheduledJob(definition, run)


def _crypto_derivatives_job(
    controller: _LocalScheduledOperations,
    descriptor: MarketAssetDescriptor,
    config: LocalWatchlistScheduleConfig,
) -> RegisteredScheduledJob:
    definition = ScheduledJobDefinition(
        job_id=f"deribit:{descriptor.asset_id}:crypto-derivatives",
        asset_id=descriptor.asset_id,
        provider="deribit",
        domain=ScheduledJobDomain.CRYPTO_DERIVATIVES,
        data_frequency="hour_1/day_1/event",
        timezone=config.timezone,
        run_at=_offset_minute(config.run_at, 10),
        freshness_threshold_seconds=129_600,
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        latest_closed_utc_date = invocation.scheduled_for.astimezone(UTC).date() - timedelta(days=1)
        try:
            summary = controller.crypto_derivatives_refresh_request(
                CryptoDerivativesRefreshRequest(
                    asset_id=descriptor.asset_id,
                    start_date=latest_closed_utc_date - timedelta(days=89),
                    end_date=latest_closed_utc_date,
                    refresh_mode=CryptoDerivativesRefreshMode.AUTO,
                )
            )
        except (CryptoDerivativesRefreshError, DeribitError, StorageError, ValueError) as error:
            raise _classified_provider_error(error) from error
        return ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=summary.effective_known_at,
            evidence_changed=summary.created_count > 0,
            source_ids=summary.source_ids,
            created_count=summary.created_count,
            reused_count=summary.reused_count,
            coverage_complete=summary.traceability_verified,
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
            raise _classified_provider_error(error) from error
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
            raise _classified_provider_error(error) from error
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


def _crypto_spot_daily_execution(
    job_id: str,
    summary: CryptoSpotDailyRefreshSummary,
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


def _classified_provider_error(error: Exception) -> ScheduledJobRunError:
    """Map typed provider causes to one safe scheduler policy without parsing messages."""
    chain = _exception_chain(error)
    request_error = next(
        (item for item in chain if isinstance(item, HttpRequestError)),
        None,
    )
    if request_error is not None:
        failure = _http_failure(
            status_code=request_error.status_code,
            failure_kind=request_error.failure_kind,
        )
    else:
        fred_error = next(
            (
                item
                for item in chain
                if isinstance(item, FredAlfredError) and item.failure_kind is not None
            ),
            None,
        )
        if fred_error is not None:
            failure = _http_failure(
                status_code=fred_error.status_code,
                failure_kind=fred_error.failure_kind,
            )
        else:
            status_code = _provider_status_code(chain)
            failure = (
                _http_failure(
                    status_code=status_code,
                    failure_kind=HttpRequestFailureKind.HTTP_STATUS,
                )
                if status_code is not None
                else _non_http_failure(chain)
            )
    return ScheduledJobRunError(failure)


def _http_failure(
    *,
    status_code: int | None,
    failure_kind: HttpRequestFailureKind,
) -> ScheduledJobFailure:
    if failure_kind is HttpRequestFailureKind.CONFIGURATION:
        return _safe_failure(
            ScheduledJobFailureCategory.CONFIGURATION,
            "scheduled provider request configuration is invalid",
        )
    if failure_kind is HttpRequestFailureKind.TRANSPORT:
        return _safe_failure(
            ScheduledJobFailureCategory.TRANSPORT,
            "scheduled provider transport failed after bounded internal retries",
        )
    if failure_kind is HttpRequestFailureKind.UNEXPECTED:
        return _safe_failure(
            ScheduledJobFailureCategory.UNEXPECTED,
            "scheduled provider request failed unexpectedly",
        )
    if status_code in {401, 403}:
        return _safe_failure(
            ScheduledJobFailureCategory.AUTHENTICATION,
            "scheduled provider authentication or authorization failed",
        )
    if status_code == 429:
        return _safe_failure(
            ScheduledJobFailureCategory.RATE_LIMIT,
            "scheduled provider rate limit remained active after bounded internal retries",
        )
    if status_code in RETRYABLE_HTTP_STATUS_CODES:
        return _safe_failure(
            ScheduledJobFailureCategory.TRANSIENT_HTTP,
            "scheduled provider returned a transient HTTP failure after bounded internal retries",
        )
    return _safe_failure(
        ScheduledJobFailureCategory.HTTP,
        "scheduled provider returned a permanent HTTP failure",
    )


def _non_http_failure(chain: tuple[BaseException, ...]) -> ScheduledJobFailure:
    if any(isinstance(item, ProviderConfigurationError) for item in chain):
        return _safe_failure(
            ScheduledJobFailureCategory.CONFIGURATION,
            "scheduled provider credentials or configuration are invalid",
        )
    if any(isinstance(item, SmvOpenDataNotFoundError) for item in chain):
        return _safe_failure(
            ScheduledJobFailureCategory.UNSUPPORTED_CAPABILITY,
            "scheduled provider does not support the configured asset or capability",
        )
    if any(isinstance(item, (TimeoutError, ConnectionError, URLError)) for item in chain):
        return _safe_failure(
            ScheduledJobFailureCategory.TRANSPORT,
            "scheduled provider transport failed after bounded internal retries",
        )
    if any(
        isinstance(item, (StorageError, WorkspaceError, AaplOperationalStateError, OSError))
        for item in chain
    ):
        return _safe_failure(
            ScheduledJobFailureCategory.STORAGE_STATE,
            "scheduled workspace or persisted state is incompatible or unavailable",
        )
    if any(
        isinstance(
            item,
            (
                ListedMarketKnownAtTooEarlyError,
                BtcMarketKnownAtTooEarlyError,
                SecIssuerFundamentalKnownAtTooEarlyError,
                ValidationError,
            ),
        )
        for item in chain
    ):
        return _safe_failure(
            ScheduledJobFailureCategory.VALIDATION,
            "scheduled provider result failed point-in-time or model validation",
        )
    if any(
        type(item) is SecIssuerFundamentalRefreshError and item.__cause__ is None for item in chain
    ):
        return _safe_failure(
            ScheduledJobFailureCategory.UNSUPPORTED_CAPABILITY,
            "scheduled provider does not support the configured asset or capability",
        )
    if any(
        isinstance(
            item,
            (
                AlpacaStockError,
                CoinbaseExchangeError,
                DeribitError,
                SecEdgarError,
                FredAlfredError,
                SmvOpenDataError,
                ListedMarketRefreshError,
                BtcMarketRefreshError,
                BtcIntradayRefreshError,
                ValueError,
            ),
        )
        for item in chain
    ):
        return _safe_failure(
            ScheduledJobFailureCategory.PROVIDER_CONTRACT,
            "scheduled provider payload or refresh contract is invalid",
        )
    return _safe_failure(
        ScheduledJobFailureCategory.UNEXPECTED,
        "scheduled provider refresh failed unexpectedly",
    )


def _provider_status_code(chain: tuple[BaseException, ...]) -> int | None:
    for item in chain:
        if (
            isinstance(
                item,
                (
                    AlpacaStockError,
                    CoinbaseExchangeError,
                    DeribitError,
                    SecEdgarError,
                    FredAlfredError,
                    SmvOpenDataError,
                ),
            )
            and item.status_code is not None
        ):
            return item.status_code
    return None


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        nested = current.__cause__
        if nested is None and isinstance(current, HttpRequestError):
            nested = current.cause
        current = nested
    return tuple(chain)


def _safe_failure(
    category: ScheduledJobFailureCategory,
    message: str,
) -> ScheduledJobFailure:
    return scheduled_job_failure(category, message)


def _offset_minute(value: time, minutes: int) -> time:
    anchor = datetime.combine(date(2000, 1, 1), value)
    return (anchor + timedelta(minutes=minutes)).time()


__all__ = [
    "LocalWatchlistScheduleConfig",
    "build_local_watchlist_jobs",
]
