"""Offline integration tests for one finite weekly operational cycle."""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import MarketDiagnosticMetricSelector
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.aapl_bootstrap_models import (
    AaplMarketRefreshMode,
    AaplRefreshMode,
)
from investment_analyst.application.aapl_refresh_planner import AaplMarketRefreshPlanner
from investment_analyst.application.crypto_spot_daily import CryptoSpotDailyRefreshPipeline
from investment_analyst.application.crypto_spot_daily_models import (
    CryptoSpotDailyRefreshRequest,
    CryptoSpotDailyRefreshSummary,
)
from investment_analyst.application.crypto_spot_daily_planner import CryptoSpotDailyRefreshPlanner
from investment_analyst.application.listed_market_refresh import (
    ListedMarketRefreshError,
    ListedMarketRefreshPipeline,
)
from investment_analyst.application.listed_market_refresh_models import (
    ListedMarketRefreshRequest,
    ListedMarketRefreshSummary,
)
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetScheduleStateStore,
    RegisteredScheduledJob,
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobFailureCategory,
    ScheduledJobHealth,
    ScheduledJobInvocation,
    ScheduledJobRunError,
    scheduled_job_failure,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalRefreshPipeline,
)
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshSummary,
)
from investment_analyst.catalog.provider_configuration import resolve_coinbase_configuration
from investment_analyst.core.models import AssetClass, DataFrequency
from investment_analyst.providers.asset_config import (
    AlpacaAssetConfiguration,
    SecAssetConfiguration,
)
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID as BTC_ASSET_ID,
)
from investment_analyst.providers.crypto.coinbase_normalizer import (
    SOURCE_ID as BTC_SOURCE_ID,
)
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseHistoricalPipeline
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_pipeline import (
    SecIssuerFundamentalDiagnosticPipeline,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_metric_engine import SecFundamentalMetricEngine
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecIssuerFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecIssuerObservationPipeline,
)
from investment_analyst.providers.fundamentals.sec_pipeline import SecIssuerFundamentalsPipeline
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecIssuerFundamentalPointInTimeService,
)
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_pipeline import (
    ALPACA_FETCH_RECEIPT_SCHEMA,
    AlpacaHistoricalPipeline,
    alpaca_fetch_receipt_from_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import (
    AlpacaCredentials,
    AlpacaStockClient,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_ALPACA = AlpacaAssetConfiguration(
    asset_id="equity:us:bvn",
    symbol="BVN",
    feed="iex",
    adjustment="all",
    source_id="alpaca-market-data:iex:bvn:daily-bars:adjustment-all",
    name="Compañía de Minas Buenaventura S.A.A.",
    asset_class=AssetClass.EQUITY,
    quote_currency="USD",
    exchange="NYSE",
)
_SEC = SecAssetConfiguration(
    asset_id="equity:us:amd",
    cik="0000002488",
    ticker="AMD",
    submissions_source_id="sec-edgar:amd:submissions",
    companyfacts_source_id="sec-edgar:amd:companyfacts",
    name="Advanced Micro Devices, Inc.",
    asset_class=AssetClass.EQUITY,
    quote_currency="USD",
    exchange="NASDAQ",
)
_SEC_IDENTITY = SecEdgarIdentity("Investment Analyst weekly-cycle@example.com")
_ANNUAL_ACCN = "0000002488-26-000001"
_QUARTERLY_ACCN = "0000002488-26-000002"
_REVISION_ACCN = "0000002488-26-000003"
_MARKET_START = date(2026, 10, 1)
_DAILY_BARS: dict[date, str] = {
    date(2026, 10, 13): "10.00",
    date(2026, 10, 14): "10.20",
    date(2026, 10, 15): "10.40",
    date(2026, 10, 16): "10.60",
    date(2026, 10, 19): "10.80",
}


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _AlpacaTransport:
    """Deterministic Alpaca pages with optional bounded failure injection."""

    def __init__(
        self,
        bars: dict[date, str],
        *,
        clock: _MutableClock,
        fail_pagination: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.bars = dict(bars)
        self.clock = clock
        self.fail_pagination = fail_pagination
        self.status_code = status_code
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del headers, timeout_seconds
        self.calls.append(url)
        if self.status_code is not None:
            return HttpResponse(self.status_code, b"{}", {}, url)
        query = parse_qs(urlsplit(url).query)
        if "page_token" in query:
            return HttpResponse(500, b"{}", {}, url)
        start = datetime.fromisoformat(query["start"][0].replace("Z", "+00:00"))
        end = datetime.fromisoformat(query["end"][0].replace("Z", "+00:00"))
        bars = [
            self._bar(timestamp, close)
            for timestamp, close in sorted(self.bars.items())
            if start <= datetime.combine(timestamp, time(4), tzinfo=UTC) < end
        ]
        body: dict[str, object] = {
            "symbol": _ALPACA.symbol,
            "bars": bars,
            "next_page_token": None,
        }
        if self.fail_pagination and bars:
            body["next_page_token"] = "page-2"
        return HttpResponse(
            200,
            json.dumps(body, separators=(",", ":")).encode("utf-8"),
            {},
            url,
        )

    @staticmethod
    def _bar(timestamp: date, close: str) -> dict[str, str]:
        instant = datetime.combine(timestamp, time(4), tzinfo=UTC)
        return {
            "t": instant.isoformat().replace("+00:00", "Z"),
            "o": close,
            "h": close,
            "l": close,
            "c": close,
            "v": "1000",
            "n": "10",
            "vw": close,
        }


class _BtcTransport:
    """Deterministic Coinbase daily candles for every calendar day requested."""

    def __init__(self, *, clock: _MutableClock) -> None:
        self.clock = clock
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del headers, timeout_seconds
        self.calls.append(url)
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        start = datetime.fromisoformat(query["start"][0].replace("Z", "+00:00"))
        end = datetime.fromisoformat(query["end"][0].replace("Z", "+00:00"))
        candles: list[list[float | int]] = []
        cursor = start
        offset = 0
        while cursor < end:
            low = 100_000.0 + offset
            candles.append([int(cursor.timestamp()), low, low + 10, low, low + 5, 100.5])
            cursor += timedelta(days=1)
            offset += 1
        return HttpResponse(
            200,
            json.dumps(candles).encode("utf-8"),
            {},
            url,
        )


class _SecTransport:
    """Serve one mutable submissions/companyfacts pair without network access."""

    def __init__(self) -> None:
        self.submissions, self.companyfacts = _sec_documents()
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del headers, timeout_seconds
        self.calls.append(url)
        body = self.companyfacts if "companyfacts" in url else self.submissions
        return HttpResponse(200, body, {}, url)


def _sec_documents(*, revised: bool = False) -> tuple[bytes, bytes]:
    annual_accn = _ANNUAL_ACCN
    accessions = [_ANNUAL_ACCN, _QUARTERLY_ACCN]
    filing_dates = ["2026-02-04", "2026-04-29"]
    report_dates = ["2025-12-27", "2026-03-28"]
    acceptance = ["2026-02-04T21:00:00Z", "2026-04-29T21:00:00Z"]
    forms = ["10-K", "10-Q"]
    documents = ["amd-20251227.htm", "amd-20260328.htm"]
    if revised:
        accessions.append(_REVISION_ACCN)
        filing_dates.append("2026-06-15")
        report_dates.append("2025-12-27")
        acceptance.append("2026-06-15T21:00:00Z")
        forms.append("10-K/A")
        documents.append("amd-20251227a.htm")
    submissions = {
        "cik": _SEC.cik,
        "name": _SEC.name,
        "tickers": [_SEC.ticker],
        "exchanges": [_SEC.exchange],
        "filings": {
            "recent": {
                "accessionNumber": accessions,
                "filingDate": filing_dates,
                "reportDate": report_dates,
                "acceptanceDateTime": acceptance,
                "form": forms,
                "primaryDocument": documents,
            },
            "files": [],
        },
    }

    def duration(*, annual: bool, value: str, revised_value: str | None = None) -> dict[str, str]:
        return {
            "start": "2024-12-29" if annual else "2025-12-28",
            "end": "2025-12-27" if annual else "2026-03-28",
            "val": value,
            "accn": annual_accn if annual else _QUARTERLY_ACCN,
            "fy": "2025" if annual else "2026",
            "fp": "FY" if annual else "Q1",
            "form": "10-K" if annual else "10-Q",
            "filed": "2026-02-04" if annual else "2026-04-29",
        }

    def instant(*, annual: bool, value: str) -> dict[str, str]:
        result = duration(annual=annual, value=value)
        del result["start"]
        return result

    annual_revenue = "25000"
    annual_net_income = "4000"
    facts = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {
                "USD": [
                    duration(annual=True, value=annual_revenue),
                    duration(annual=False, value="7000"),
                ]
            }
        },
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    duration(annual=True, value=annual_net_income),
                    duration(annual=False, value="1200"),
                ]
            }
        },
        "Assets": {
            "units": {
                "USD": [
                    instant(annual=True, value="70000"),
                    instant(annual=False, value="72000"),
                ]
            }
        },
        "Liabilities": {
            "units": {
                "USD": [
                    instant(annual=True, value="25000"),
                    instant(annual=False, value="26000"),
                ]
            }
        },
        "StockholdersEquity": {
            "units": {
                "USD": [
                    instant(annual=True, value="45000"),
                    instant(annual=False, value="46000"),
                ]
            }
        },
    }
    if revised:
        facts["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"].append(
            {
                "start": "2024-12-29",
                "end": "2025-12-27",
                "val": "26000",
                "accn": _REVISION_ACCN,
                "fy": "2025",
                "fp": "FY",
                "form": "10-K/A",
                "filed": "2026-06-15",
            }
        )
    companyfacts = {
        "cik": _SEC.cik,
        "entityName": _SEC.name,
        "facts": {"us-gaap": facts},
    }
    return (
        json.dumps(submissions, separators=(",", ":"), sort_keys=True).encode(),
        json.dumps(companyfacts, separators=(",", ":"), sort_keys=True).encode(),
    )


def _configure_coinbase():
    return resolve_coinbase_configuration(
        ApplicationRuntime.create_default().provider_resolver,
        asset_id="crypto:btc-usd",
    )


def _alpaca_pipeline(
    storage: LocalStorage,
    transport: _AlpacaTransport,
    clock: _MutableClock,
) -> ListedMarketRefreshPipeline:
    history = HistoricalMarketDataService(storage)
    return ListedMarketRefreshPipeline(
        configuration=_ALPACA,
        refresh_planner=AaplMarketRefreshPlanner(_ALPACA, storage),
        market_pipeline=AlpacaHistoricalPipeline(
            storage,
            AlpacaStockClient(
                transport,
                AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
                clock=clock,
            ),
            configuration=_ALPACA,
            clock=clock,
        ),
        statistics_pipeline=MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=clock,
        ),
        diagnostic_pipeline=MarketDiagnosticPipeline(
            storage,
            MarketDiagnosticMetricSelector(storage),
            MarketDiagnosticEngine(),
            clock=clock,
        ),
        clock=clock,
    )


def _btc_pipeline(
    storage: LocalStorage,
    transport: _BtcTransport,
    clock: _MutableClock,
) -> CryptoSpotDailyRefreshPipeline:
    configuration = _configure_coinbase()
    history = HistoricalMarketDataService(storage)
    return CryptoSpotDailyRefreshPipeline(
        asset_id=configuration.asset_id,
        source_id=configuration.source_id,
        refresh_planner=CryptoSpotDailyRefreshPlanner(
            storage,
            asset_id=configuration.asset_id,
            source_id=configuration.source_id,
        ),
        market_pipeline=CoinbaseHistoricalPipeline(
            storage,
            CoinbaseExchangeClient(transport, sleep=lambda _: None, clock=clock),
            configuration=configuration,
            clock=clock,
        ),
        statistics_pipeline=MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=clock,
        ),
        diagnostic_pipeline=MarketDiagnosticPipeline(
            storage,
            MarketDiagnosticMetricSelector(storage),
            MarketDiagnosticEngine(),
            clock=clock,
        ),
        clock=clock,
    )


def _sec_pipeline(
    storage: LocalStorage,
    transport: _SecTransport,
    clock: _MutableClock,
) -> SecIssuerFundamentalRefreshPipeline:
    client = SecEdgarClient(
        transport,
        _SEC_IDENTITY,
        cik=_SEC.cik,
        ticker=_SEC.ticker,
        sleep=lambda _: None,
        clock=clock,
    )
    return SecIssuerFundamentalRefreshPipeline(
        storage,
        configuration=_SEC,
        fetch_pipeline=SecIssuerFundamentalsPipeline(
            storage,
            client,
            configuration=_SEC,
        ),
        observation_pipeline=SecIssuerObservationPipeline(
            storage,
            SecCompanyFactsNormalizer(_SEC),
            configuration=_SEC,
            clock=clock,
        ),
        metric_pipeline=SecIssuerFundamentalMetricPipeline(
            storage,
            SecIssuerFundamentalPointInTimeService(storage, _SEC),
            SecFundamentalMetricEngine(_SEC),
            configuration=_SEC,
            clock=clock,
        ),
        diagnostic_pipeline=SecIssuerFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage, _SEC),
            SecFundamentalDiagnosticEngine(_SEC),
            configuration=_SEC,
            clock=clock,
        ),
        clock=clock,
    )


def _failure_category(error: BaseException) -> ScheduledJobFailureCategory:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if status == 429:
            return ScheduledJobFailureCategory.RATE_LIMIT
        if status in {408, 500, 502, 503, 504}:
            return ScheduledJobFailureCategory.TRANSIENT_HTTP
        if isinstance(current, (TimeoutError, ConnectionError)):
            return ScheduledJobFailureCategory.TRANSPORT
        current = current.__cause__
    return ScheduledJobFailureCategory.PROVIDER_CONTRACT


def _definition(
    *,
    job_id: str,
    asset_id: str,
    provider: str,
    domain: ScheduledJobDomain,
    data_frequency: str,
    max_attempts: int = 3,
    backoff: int = 60,
) -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id=job_id,
        asset_id=asset_id,
        provider=provider,
        domain=domain,
        data_frequency=data_frequency,
        timezone="America/Lima",
        run_at=time(hour=7),
        max_attempts_per_day=max_attempts,
        retry_backoff_seconds=backoff,
    )


def _listed_execution(
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
        analytical_inputs_changed=(
            summary.raw_records_created + summary.observations_created > 0
            or (summary.metric_results_created - summary.valuation_metric_results_created) > 0
        ),
    )


def _btc_execution(
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
        analytical_inputs_changed=(
            summary.raw_records_created + summary.observations_created > 0
            or summary.metric_results_created > 0
        ),
    )


def _sec_execution(
    job_id: str,
    summary: SecIssuerFundamentalRefreshSummary,
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


def _market_job(
    pipeline: ListedMarketRefreshPipeline,
    *,
    max_attempts: int = 3,
    backoff: int = 60,
) -> RegisteredScheduledJob:
    definition = _definition(
        job_id="alpaca:equity:us:bvn:market-daily",
        asset_id=_ALPACA.asset_id,
        provider="alpaca",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        max_attempts=max_attempts,
        backoff=backoff,
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        market_end = invocation.local_date - timedelta(days=1)
        try:
            summary = pipeline.run(
                ListedMarketRefreshRequest(
                    asset_id=_ALPACA.asset_id,
                    market_start=_MARKET_START,
                    market_end=market_end,
                )
            )
        except (ListedMarketRefreshError, TimeoutError, ValueError) as error:
            raise ScheduledJobRunError(
                scheduled_job_failure(
                    _failure_category(error),
                    "scheduled listed market refresh failed",
                )
            ) from error
        return _listed_execution(definition.job_id, summary)

    return RegisteredScheduledJob(definition, run)


def _crypto_job(pipeline: CryptoSpotDailyRefreshPipeline) -> RegisteredScheduledJob:
    definition = _definition(
        job_id="coinbase:crypto:btc-usd:market-daily",
        asset_id=BTC_ASSET_ID,
        provider="coinbase",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        market_end = invocation.local_date - timedelta(days=1)
        try:
            summary = pipeline.run(
                CryptoSpotDailyRefreshRequest(
                    asset_id=BTC_ASSET_ID,
                    market_start=_MARKET_START,
                    market_end=market_end,
                )
            )
        except (TimeoutError, ValueError) as error:
            raise ScheduledJobRunError(
                scheduled_job_failure(
                    _failure_category(error),
                    "scheduled crypto daily refresh failed",
                )
            ) from error
        return _btc_execution(definition.job_id, summary)

    return RegisteredScheduledJob(definition, run)


def _fundamental_job(pipeline: SecIssuerFundamentalRefreshPipeline) -> RegisteredScheduledJob:
    definition = _definition(
        job_id="sec:equity:us:amd:fundamentals-annual",
        asset_id=_SEC.asset_id,
        provider="sec-edgar",
        domain=ScheduledJobDomain.FUNDAMENTALS,
        data_frequency="annual",
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        del invocation
        try:
            summary = pipeline.run(
                SecIssuerFundamentalRefreshRequest(
                    asset_id=_SEC.asset_id,
                    frequency=DataFrequency.ANNUAL,
                )
            )
        except (TimeoutError, ValueError) as error:
            raise ScheduledJobRunError(
                scheduled_job_failure(
                    _failure_category(error),
                    "scheduled SEC fundamental refresh failed",
                )
            ) from error
        return _sec_execution(definition.job_id, summary)

    return RegisteredScheduledJob(definition, run)


def _scheduler(
    jobs: tuple[RegisteredScheduledJob, ...],
    state_path: Path,
    clock: _MutableClock,
) -> MultiAssetScheduler:
    return MultiAssetScheduler(
        jobs,
        MultiAssetScheduleStateStore(state_path),
        clock=clock,
    )


def _attempt_for_job(
    clock: _MutableClock,
    definition: ScheduledJobDefinition,
    *,
    status: ScheduledJobAttemptStatus,
) -> ScheduledJobAttempt:
    local_date = clock().astimezone(ZoneInfo(definition.timezone)).date()
    return ScheduledJobAttempt(
        attempt_id=uuid4(),
        definition=definition,
        local_date=local_date,
        scheduled_for=definition.scheduled_for(local_date),
        attempt_number=1,
        status=status,
        started_at=clock(),
    )


def _receipts(storage: LocalStorage) -> tuple[object, ...]:
    receipts = [
        receipt
        for receipt in (
            alpaca_fetch_receipt_from_raw_record(record)
            for record in storage.raw_records.list(
                asset_id=_ALPACA.asset_id,
                source_id=_ALPACA.source_id,
                schema_version=ALPACA_FETCH_RECEIPT_SCHEMA,
            )
        )
        if receipt is not None
    ]
    return tuple(sorted(receipts, key=lambda item: item.requested_start))


def test_weekly_cycle_covers_weekends_empty_receipts_and_next_session(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 10, 16, 12, 5, tzinfo=UTC))
    alpaca_transport = _AlpacaTransport(_DAILY_BARS, clock=clock)
    btc_transport = _BtcTransport(clock=clock)
    sec_transport = _SecTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        scheduler = _scheduler(
            (
                _market_job(_alpaca_pipeline(storage, alpaca_transport, clock)),
                _crypto_job(_btc_pipeline(storage, btc_transport, clock)),
                _fundamental_job(_sec_pipeline(storage, sec_transport, clock)),
            ),
            tmp_path / "schedule.json",
            clock,
        )

        friday = scheduler.tick()
        assert len(friday) == 3
        assert all(item.status is ScheduledJobAttemptStatus.SUCCEEDED for item in friday)
        friday_receipts = _receipts(storage)
        assert len(friday_receipts) == 1
        assert friday_receipts[0].bar_count == 3
        assert friday_receipts[0].requested_start == datetime(2026, 10, 1, tzinfo=UTC)
        assert friday_receipts[0].requested_end == datetime(2026, 10, 16, tzinfo=UTC)

        clock.value = datetime(2026, 10, 17, 12, 5, tzinfo=UTC)
        saturday = scheduler.tick()
        assert len(saturday) == 3
        assert all(item.status is ScheduledJobAttemptStatus.SUCCEEDED for item in saturday)

        clock.value = datetime(2026, 10, 18, 12, 5, tzinfo=UTC)
        sunday = scheduler.tick()
        assert len(sunday) == 3
        assert all(item.status is ScheduledJobAttemptStatus.SUCCEEDED for item in sunday)

        clock.value = datetime(2026, 10, 19, 12, 5, tzinfo=UTC)
        monday = scheduler.tick()
        assert len(monday) == 3
        assert all(item.status is ScheduledJobAttemptStatus.SUCCEEDED for item in monday)

        clock.value = datetime(2026, 10, 20, 12, 5, tzinfo=UTC)
        tuesday = scheduler.tick()
        assert len(tuesday) == 3
        assert all(item.status is ScheduledJobAttemptStatus.SUCCEEDED for item in tuesday)

        receipts = _receipts(storage)
        assert [receipt.bar_count for receipt in receipts] == [3, 1, 0, 0, 1]
        assert [receipt.requested_start.date() for receipt in receipts] == [
            date(2026, 10, 1),
            date(2026, 10, 16),
            date(2026, 10, 17),
            date(2026, 10, 18),
            date(2026, 10, 19),
        ]
        assert [receipt.requested_end.date() for receipt in receipts] == [
            date(2026, 10, 16),
            date(2026, 10, 17),
            date(2026, 10, 18),
            date(2026, 10, 19),
            date(2026, 10, 20),
        ]

        market_timestamps = sorted(
            item.observed_at
            for item in storage.observations.list(
                asset_id=_ALPACA.asset_id,
                source_id=_ALPACA.source_id,
                frequency=DataFrequency.DAY_1,
            )
            if item.field_name == "close"
        )
        assert [item.date() for item in market_timestamps] == [
            date(2026, 10, 13),
            date(2026, 10, 14),
            date(2026, 10, 15),
            date(2026, 10, 16),
            date(2026, 10, 19),
        ]
        assert not any(
            item.date() in {date(2026, 10, 17), date(2026, 10, 18)} for item in market_timestamps
        )

        btc_sunday = [
            item
            for item in storage.observations.list(
                asset_id=BTC_ASSET_ID,
                source_id=BTC_SOURCE_ID,
                frequency=DataFrequency.DAY_1,
            )
            if item.observed_at.date() == date(2026, 10, 18)
        ]
        assert btc_sunday
        assert {item.field_name for item in btc_sunday} >= {"close"}

        assert len(sec_transport.calls) == 10
        assert len(storage.raw_records.list(source_id=_SEC.companyfacts_source_id)) == 1
        assert len(storage.raw_records.list(source_id=_SEC.submissions_source_id)) == 1

        calls_before = len(alpaca_transport.calls)
        attempts_before = len(_receipts(storage))
        assert scheduler.tick() == ()
        assert len(alpaca_transport.calls) == calls_before
        assert len(_receipts(storage)) == attempts_before
        status = scheduler.status()
        assert status.due_count == 0
        assert all(item.health is ScheduledJobHealth.CURRENT for item in status.jobs)

        assert len(alpaca_transport.calls) == 5
        assert len(btc_transport.calls) == 5


def test_sec_new_filing_runs_while_market_is_already_current(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 10, 20, 12, 5, tzinfo=UTC))
    alpaca_transport = _AlpacaTransport(_DAILY_BARS, clock=clock)
    sec_transport = _SecTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        market = _alpaca_pipeline(storage, alpaca_transport, clock)
        first = market.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=_MARKET_START,
                market_end=date(2026, 10, 19),
            )
        )
        assert first.refresh_plan.mode is AaplMarketRefreshMode.INITIAL
        assert len(alpaca_transport.calls) == 1

        sec_pipeline = _sec_pipeline(storage, sec_transport, clock)
        first_sec = sec_pipeline.run(
            SecIssuerFundamentalRefreshRequest(
                asset_id=_SEC.asset_id,
                frequency=DataFrequency.ANNUAL,
            )
        )
        assert first_sec.raw_records_created == 2

        sec_transport.submissions, sec_transport.companyfacts = _sec_documents(revised=True)
        clock.value = datetime(2026, 10, 20, 13, 5, tzinfo=UTC)
        second_sec = _sec_pipeline(storage, sec_transport, clock).run(
            SecIssuerFundamentalRefreshRequest(
                asset_id=_SEC.asset_id,
                frequency=DataFrequency.ANNUAL,
            )
        )
        assert second_sec.raw_records_created == 2
        assert second_sec.metric_results_created > 0

        current = market.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=_MARKET_START,
                market_end=date(2026, 10, 19),
            )
        )
        assert current.refresh_plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
        assert len(alpaca_transport.calls) == 1
        assert len(sec_transport.calls) == 4


def test_failed_pagination_does_not_credit_interval_and_preserves_progress(
    tmp_path: Path,
) -> None:
    clock = _MutableClock(datetime(2026, 10, 16, 12, 5, tzinfo=UTC))
    transport = _AlpacaTransport(_DAILY_BARS, clock=clock)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _alpaca_pipeline(storage, transport, clock)
        first = pipeline.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=_MARKET_START,
                market_end=date(2026, 10, 14),
            )
        )
        assert first.raw_records_created == 2
        assert len(_receipts(storage)) == 1

        transport.fail_pagination = True
        with pytest.raises(ListedMarketRefreshError, match="market fetch failed"):
            pipeline.run(
                ListedMarketRefreshRequest(
                    asset_id=_ALPACA.asset_id,
                    market_start=_MARKET_START,
                    market_end=date(2026, 10, 15),
                )
            )
        assert len(_receipts(storage)) == 1
        assert len(storage.raw_records.list(source_id=_ALPACA.source_id)) == 3
        assert not any(
            item.observed_at.date() == date(2026, 10, 15)
            for item in storage.observations.list(asset_id=_ALPACA.asset_id)
        )

        transport.fail_pagination = False
        recovered = pipeline.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=_MARKET_START,
                market_end=date(2026, 10, 15),
            )
        )
        assert recovered.refresh_plan.mode is AaplMarketRefreshMode.INCREMENTAL
        assert recovered.raw_records_created == 1
        assert len(_receipts(storage)) == 2


def test_scheduler_bounds_rate_limit_and_transport_retries(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 10, 16, 12, 5, tzinfo=UTC))
    transport = _AlpacaTransport(_DAILY_BARS, clock=clock, status_code=429)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        scheduler = _scheduler(
            (_market_job(_alpaca_pipeline(storage, transport, clock)),),
            tmp_path / "schedule.json",
            clock,
        )
        first = scheduler.tick()
        assert len(first) == 1
        assert first[0].status is ScheduledJobAttemptStatus.FAILED
        assert first[0].failure is not None
        assert first[0].failure.category is ScheduledJobFailureCategory.RATE_LIMIT
        assert first[0].failure.retryable is True

        clock.value = datetime(2026, 10, 16, 12, 5, 30, tzinfo=UTC)
        assert scheduler.tick() == ()

        clock.value = datetime(2026, 10, 16, 12, 6, 10, tzinfo=UTC)
        transport.status_code = None
        second = scheduler.tick()
        assert len(second) == 1
        assert second[0].status is ScheduledJobAttemptStatus.SUCCEEDED
        assert len(transport.calls) == 2
        assert scheduler.status().jobs[0].health is ScheduledJobHealth.CURRENT

    exhausted_clock = _MutableClock(datetime(2026, 10, 16, 12, 5, tzinfo=UTC))
    exhausted_transport = _AlpacaTransport(
        _DAILY_BARS,
        clock=exhausted_clock,
        status_code=429,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path / "exhausted")) as storage:
        scheduler = _scheduler(
            (
                _market_job(
                    _alpaca_pipeline(storage, exhausted_transport, exhausted_clock),
                    max_attempts=1,
                ),
            ),
            tmp_path / "exhausted.json",
            exhausted_clock,
        )
        assert len(scheduler.tick()) == 1
        exhausted_clock.value = datetime(2026, 10, 16, 12, 30, tzinfo=UTC)
        assert scheduler.tick() == ()
        status = scheduler.status()
        assert status.jobs[0].health is ScheduledJobHealth.BLOCKED
        assert any("daily retry budget exhausted" in issue for issue in status.issues)
        assert len(exhausted_transport.calls) == 1

    timeout_clock = _MutableClock(datetime(2026, 10, 16, 12, 5, tzinfo=UTC))

    class _TimeoutTransport(_AlpacaTransport):
        def get(self, url, *, headers, timeout_seconds):
            del headers, timeout_seconds
            self.calls.append(url)
            raise TimeoutError("synthetic timeout")

    timeout_transport = _TimeoutTransport(_DAILY_BARS, clock=timeout_clock)
    with LocalStorage(StoragePaths.from_root(tmp_path / "timeout")) as storage:
        scheduler = _scheduler(
            (_market_job(_alpaca_pipeline(storage, timeout_transport, timeout_clock)),),
            tmp_path / "timeout.json",
            timeout_clock,
        )
        attempt = scheduler.tick()[0]
        assert attempt.failure is not None
        assert attempt.failure.category is ScheduledJobFailureCategory.TRANSPORT
        assert attempt.failure.retryable is True
        timeout_clock.value = datetime(2026, 10, 16, 12, 6, 10, tzinfo=UTC)
        assert len(scheduler.tick()) == 1


def test_scheduler_restart_reuses_success_and_recovers_interruption(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 10, 16, 12, 5, tzinfo=UTC))
    transport = _AlpacaTransport(_DAILY_BARS, clock=clock)
    state_path = tmp_path / "schedule.json"
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _alpaca_pipeline(storage, transport, clock)
        job = _market_job(pipeline)
        scheduler = _scheduler((job,), state_path, clock)
        assert len(scheduler.tick()) == 1
        calls_after_success = len(transport.calls)

        restarted = _scheduler((job,), state_path, clock)
        assert restarted.tick() == ()
        assert len(transport.calls) == calls_after_success

        clock.value = datetime(2026, 10, 17, 7, 10, tzinfo=UTC)
        interrupted = _attempt_for_job(
            clock,
            job.definition,
            status=ScheduledJobAttemptStatus.RUNNING,
        )
        MultiAssetScheduleStateStore(state_path).write_attempt(interrupted)
        recovered = restarted.tick()
        assert len(recovered) == 1
        assert recovered[0].status is ScheduledJobAttemptStatus.FAILED
        assert recovered[0].failure is not None
        assert recovered[0].failure.category is ScheduledJobFailureCategory.INTERRUPTED
        assert len(transport.calls) == calls_after_success

        clock.value = datetime(2026, 10, 17, 12, 6, tzinfo=UTC)
        retried = restarted.tick()
        assert len(retried) == 1
        assert retried[0].status is ScheduledJobAttemptStatus.SUCCEEDED
        assert len(transport.calls) == calls_after_success + 1


def test_late_market_revision_with_same_count_preserves_history(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 10, 16, 12, 5, tzinfo=UTC))
    transport = _AlpacaTransport(_DAILY_BARS, clock=clock)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _alpaca_pipeline(storage, transport, clock)
        first = pipeline.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=_MARKET_START,
                market_end=date(2026, 10, 15),
            )
        )
        first_known_at = first.effective_known_at
        old_bar = (
            HistoricalMarketDataService(storage)
            .query(
                HistoricalBarQuery(
                    asset_id=_ALPACA.asset_id,
                    source_id=_ALPACA.source_id,
                    start=datetime(2026, 10, 15, tzinfo=UTC),
                    end=datetime(2026, 10, 16, tzinfo=UTC),
                    known_at=first_known_at,
                )
            )
            .bars[-1]
        )

        transport.bars[date(2026, 10, 15)] = "10.55"
        clock.value = datetime(2026, 10, 16, 13, 5, tzinfo=UTC)
        revised = pipeline.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=_MARKET_START,
                market_end=date(2026, 10, 15),
                refresh_mode=AaplRefreshMode.FULL,
            )
        )
        assert revised.bars_received == 3
        assert revised.raw_records_created == 1
        assert revised.observations_created > 0
        new_bar = (
            HistoricalMarketDataService(storage)
            .query(
                HistoricalBarQuery(
                    asset_id=_ALPACA.asset_id,
                    source_id=_ALPACA.source_id,
                    start=datetime(2026, 10, 15, tzinfo=UTC),
                    end=datetime(2026, 10, 16, tzinfo=UTC),
                    known_at=revised.effective_known_at,
                )
            )
            .bars[-1]
        )
        assert old_bar.close == Decimal("10.40")
        assert new_bar.close == Decimal("10.55")
        assert new_bar.raw_record_id != old_bar.raw_record_id
        assert new_bar.observation_ids["close"] != old_bar.observation_ids["close"]
        assert any(
            item.observation_id == old_bar.observation_ids["close"]
            for item in storage.observations.list(asset_id=_ALPACA.asset_id)
        )


def test_scheduler_uses_utc_bounds_for_the_local_daily_cut(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 10, 16, 11, 59, tzinfo=UTC))
    transport = _AlpacaTransport(_DAILY_BARS, clock=clock)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        scheduler = _scheduler(
            (_market_job(_alpaca_pipeline(storage, transport, clock)),),
            tmp_path / "schedule.json",
            clock,
        )
        job = scheduler.registered_job_definitions()[0]
        assert job.scheduled_for(date(2026, 10, 16)) == datetime(2026, 10, 16, 12, tzinfo=UTC)
        assert scheduler.tick() == ()
        assert transport.calls == []

        clock.value = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        attempts = scheduler.tick()
        assert len(attempts) == 1
        assert len(transport.calls) == 1
        query = parse_qs(urlsplit(transport.calls[0]).query)
        assert query["start"] == ["2026-10-01T00:00:00+00:00"]
        assert query["end"] == ["2026-10-16T00:00:00+00:00"]


def test_holiday_empty_response_and_internal_gaps_are_not_inferred(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 12, 29, 12, 5, tzinfo=UTC))
    transport = _AlpacaTransport(
        {
            date(2026, 12, 24): "11.00",
            date(2026, 12, 28): "11.20",
        },
        clock=clock,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _alpaca_pipeline(storage, transport, clock)
        holiday = pipeline.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=date(2026, 12, 24),
                market_end=date(2026, 12, 25),
            )
        )
        assert holiday.refresh_plan.mode is AaplMarketRefreshMode.INITIAL
        assert holiday.bars_received == 1
        assert holiday.coverage_receipts_created == 1
        assert holiday.market_as_of == datetime(2026, 12, 24, 4, tzinfo=UTC)
        assert not any(
            item.observed_at.date() == date(2026, 12, 25)
            for item in storage.observations.list(asset_id=_ALPACA.asset_id)
        )
        receipt = _receipts(storage)[0]
        assert receipt.bar_count == 1
        assert receipt.requested_start == datetime(2026, 12, 24, tzinfo=UTC)
        assert receipt.requested_end == datetime(2026, 12, 26, tzinfo=UTC)

        next_session = pipeline.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=date(2026, 12, 28),
                market_end=date(2026, 12, 28),
            )
        )
        assert next_session.refresh_plan.mode is AaplMarketRefreshMode.INCREMENTAL
        assert next_session.bars_received == 1
        assert next_session.market_as_of == datetime(2026, 12, 28, 4, tzinfo=UTC)

        calls_before = len(transport.calls)
        internal_gap = pipeline.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=date(2026, 12, 24),
                market_end=date(2026, 12, 28),
            )
        )
        assert internal_gap.refresh_plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
        assert internal_gap.intervals_executed == 0
        assert len(transport.calls) == calls_before


def test_empty_initial_interval_is_legitimate_coverage(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 12, 26, 12, 5, tzinfo=UTC))
    transport = _AlpacaTransport({}, clock=clock)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _alpaca_pipeline(storage, transport, clock)
        first = pipeline.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=date(2026, 12, 25),
                market_end=date(2026, 12, 25),
            )
        )
        assert first.refresh_plan.mode is AaplMarketRefreshMode.INITIAL
        assert first.bars_received == 0
        assert first.observations_created == 0
        assert first.metric_results_created == 0
        assert first.market_as_of is None
        assert first.coverage_receipts_created == 1
        receipt = _receipts(storage)[0]
        assert receipt.bar_count == 0
        assert receipt.requested_start == datetime(2026, 12, 25, tzinfo=UTC)
        assert receipt.requested_end == datetime(2026, 12, 26, tzinfo=UTC)

        repeated = pipeline.run(
            ListedMarketRefreshRequest(
                asset_id=_ALPACA.asset_id,
                market_start=date(2026, 12, 25),
                market_end=date(2026, 12, 25),
            )
        )
        assert repeated.refresh_plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
        assert repeated.intervals_executed == 0
        assert len(_receipts(storage)) == 1
        assert len(transport.calls) == 1


def test_only_one_writer_runs_per_tick(tmp_path: Path) -> None:
    import threading

    started = threading.Event()
    release = threading.Event()
    calls: list[datetime] = []
    definition = _definition(
        job_id="test:one-writer",
        asset_id="equity:test:one-writer",
        provider="test",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
    )

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        calls.append(invocation.started_at)
        started.set()
        assert release.wait(5)
        return ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=invocation.started_at,
            evidence_changed=False,
            source_ids=("test:source",),
            created_count=0,
            reused_count=0,
        )

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, run),),
        MultiAssetScheduleStateStore(tmp_path / "one-writer.json"),
        clock=lambda: datetime(2026, 10, 16, 12, 5, tzinfo=UTC),
    )
    results: dict[str, tuple[ScheduledJobAttempt, ...]] = {}

    def first_tick() -> None:
        results["first"] = scheduler.tick()

    worker = threading.Thread(target=first_tick)
    worker.start()
    try:
        assert started.wait(5)
        results["second"] = scheduler.tick()
    finally:
        release.set()
        worker.join(5)
    assert results["second"] == ()
    assert len(results["first"]) == 1
    assert len(calls) == 1
