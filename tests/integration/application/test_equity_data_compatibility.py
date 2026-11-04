"""End-to-end compatibility evidence for exceptional Alpaca and SEC data."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import duckdb
import pytest

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.chart_models import (
    AaplMarketChartInterval,
    AaplMarketChartPeriod,
    AaplMarketChartRequest,
)
from investment_analyst.analytics.market.chart_service import ListedMarketChartService
from investment_analyst.analytics.market.history_service import (
    HistoricalMarketDataService,
    IncompleteMarketBarError,
)
from investment_analyst.analytics.market.history_v2 import HistoricalMarketDataV2Service
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.core.models import (
    AssetClass,
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.providers.asset_config import (
    AlpacaAssetConfiguration,
    SecAssetConfiguration,
    sec_source_ids,
)
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    sec_transformation_version,
)
from investment_analyst.providers.fundamentals.sec_fact_models import SEC_FACT_DEFINITIONS
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricRequest,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecIssuerFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    AmbiguousSecFundamentalRevisionError,
    SecIssuerFundamentalPointInTimeService,
)
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalAmbiguityPolicy,
    SecFundamentalQuery,
)
from investment_analyst.providers.market.alpaca_normalizer import (
    alpaca_source_id,
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.raw_v2 import RawV2Staging

_MARKET_ASSETS = (
    ("equity:us:mog.a", "MOG.A"),
    ("equity:us:aapl", "AAPL"),
)
_SEC_ASSETS = (
    ("equity:us:mog.a", "MOG.A", "0000067887", "0000067887-14-000005"),
    ("equity:us:aapl", "AAPL", "0000320193", "0000320193-14-000005"),
)
_SEC_NAMESPACE = UUID("b6f8e91d-2d63-4a0c-8f5f-0bca2b1f8f6e")
_EXCEPTION_TIMESTAMP = datetime(2025, 11, 28, 5, tzinfo=UTC)
_NEIGHBOR_TIMESTAMP = datetime(2025, 11, 27, 5, tzinfo=UTC)
_LATER_TIMESTAMP = datetime(2025, 12, 1, 5, tzinfo=UTC)
_MARKET_KNOWN_AT = datetime(2026, 6, 1, 12, tzinfo=UTC)


def _alpaca_configuration(asset_id: str, symbol: str) -> AlpacaAssetConfiguration:
    return AlpacaAssetConfiguration(
        asset_id=asset_id,
        symbol=symbol,
        feed="iex",
        adjustment="all",
        source_id=alpaca_source_id(symbol),
        name=f"{symbol} compatibility fixture",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NYSE",
    )


def _market_bar(
    configuration: AlpacaAssetConfiguration,
    timestamp: datetime,
    *,
    close: Decimal,
    zero_activity: bool = False,
) -> AlpacaStockBar:
    volume = Decimal("0") if zero_activity else Decimal("1000")
    trade_count = Decimal("0") if zero_activity else Decimal("100")
    vwap = Decimal("0") if zero_activity else close
    raw_values = {
        "t": timestamp.isoformat().replace("+00:00", "Z"),
        "o": str(close),
        "h": str(close + Decimal("1")),
        "l": str(close - Decimal("1")),
        "c": str(close),
        "v": str(volume),
        "n": str(trade_count),
        "vw": str(vwap),
    }
    return AlpacaStockBar(
        symbol=configuration.symbol,
        timestamp=timestamp,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=volume,
        trade_count=trade_count,
        vwap=vwap,
        raw_values=raw_values,
    )


def _market_context(timestamp: datetime) -> tuple[datetime, datetime]:
    return timestamp + timedelta(hours=2), timestamp + timedelta(hours=2, minutes=1)


def _save_market_bar(
    storage: LocalStorage,
    configuration: AlpacaAssetConfiguration,
    bar: AlpacaStockBar,
    *,
    omit: str | None = None,
) -> tuple[RawRecord, tuple[NormalizedObservation, ...]]:
    retrieved_at, normalized_at = _market_context(bar.timestamp)
    raw = bar_to_raw_record(
        bar,
        retrieved_at=retrieved_at,
        request_url="https://data.alpaca.markets/test",
        configuration=configuration,
    )
    storage.raw_records.save(raw)
    observations = bar_to_observations(
        bar,
        raw,
        normalized_at=normalized_at,
        configuration=configuration,
    )
    selected = tuple(item for item in observations if item.field_name != omit)
    storage.observations.save_many(list(selected))
    return raw, selected


def _stage_v2_bar(
    staging: RawV2Staging,
    configuration: AlpacaAssetConfiguration,
    bar: AlpacaStockBar,
    *,
    omit: str | None = None,
) -> tuple[RawRecord, tuple[NormalizedObservation, ...]]:
    retrieved_at, normalized_at = _market_context(bar.timestamp)
    raw = bar_to_raw_record(
        bar,
        retrieved_at=retrieved_at,
        request_url="https://data.alpaca.markets/test",
        configuration=configuration,
    )
    staging.save(raw)
    observations = bar_to_observations(
        bar,
        raw,
        normalized_at=normalized_at,
        configuration=configuration,
    )
    selected = tuple(item for item in observations if item.field_name != omit)
    staging.save_observations(selected)
    return raw, selected


def _market_query(
    configuration: AlpacaAssetConfiguration,
    *,
    start: datetime = datetime(2025, 11, 1, tzinfo=UTC),
    end: datetime = datetime(2025, 12, 15, tzinfo=UTC),
    known_at: datetime = _MARKET_KNOWN_AT,
) -> HistoricalBarQuery:
    return HistoricalBarQuery(
        asset_id=configuration.asset_id,
        source_id=configuration.source_id,
        start=start,
        end=end,
        known_at=known_at,
    )


@pytest.mark.parametrize(("asset_id", "symbol"), _MARKET_ASSETS)
def test_market_chain_preserves_vwap_absence_across_legacy_v2_and_chart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asset_id: str,
    symbol: str,
) -> None:
    configuration = _alpaca_configuration(asset_id, symbol)
    control = _alpaca_configuration("equity:us:control", "CTRL")
    exceptional = _market_bar(
        configuration,
        _EXCEPTION_TIMESTAMP,
        close=Decimal("227.41"),
        zero_activity=True,
    )
    neighbor = _market_bar(configuration, _NEIGHBOR_TIMESTAMP, close=Decimal("226.00"))
    later = _market_bar(configuration, _LATER_TIMESTAMP, close=Decimal("228.00"))
    control_bar = _market_bar(control, _EXCEPTION_TIMESTAMP, close=Decimal("99.00"))

    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        exceptional_raw, exceptional_observations = _save_market_bar(
            storage, configuration, exceptional
        )
        _save_market_bar(storage, configuration, neighbor)
        _save_market_bar(storage, configuration, later)
        _save_market_bar(storage, control, control_bar)

        series = HistoricalMarketDataService(storage).query(_market_query(configuration))
        assert [bar.timestamp for bar in series.bars] == [
            _NEIGHBOR_TIMESTAMP,
            _EXCEPTION_TIMESTAMP,
            _LATER_TIMESTAMP,
        ]
        bar = series.bars[1]
        assert bar.asset_id == configuration.asset_id
        assert bar.source_id == configuration.source_id
        assert bar.vwap is None
        assert "vwap" not in bar.observation_ids
        assert set(bar.observation_ids) == {
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
        }
        assert bar.observation_ids == {
            observation.field_name: observation.observation_id
            for observation in exceptional_observations
        }
        assert bar.close == Decimal("227.41")
        assert bar.volume == Decimal("0")
        assert bar.trade_count == Decimal("0")
        assert bar.available_at == _EXCEPTION_TIMESTAMP + timedelta(hours=2)
        assert all(
            item.asset_id == configuration.asset_id
            for item in storage.observations.list(
                asset_id=configuration.asset_id,
                source_id=configuration.source_id,
            )
        )
        assert all(
            item.unit != "USD" or item.value != 0
            for item in storage.observations.list(
                asset_id=configuration.asset_id,
                source_id=configuration.source_id,
            )
        )
        assert not any(
            item.field_name == "vwap" and item.raw_record_id == exceptional_raw.record_id
            for item in storage.observations.list(
                asset_id=configuration.asset_id,
                source_id=configuration.source_id,
            )
        )

        chart = ListedMarketChartService(
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
        ).query(
            AaplMarketChartRequest(
                known_at=_MARKET_KNOWN_AT,
                period=AaplMarketChartPeriod.ONE_YEAR,
            ),
            asset_id=configuration.asset_id,
            source_id=configuration.source_id,
        )
        daily = next(point for point in chart.points if point.timestamp == _EXCEPTION_TIMESTAMP)
        assert daily.vwap is None
        assert daily.vwap_input_observation_ids == ()
        assert daily.close == Decimal("227.41")
        assert daily.close_observation_id == exceptional_observations[3].observation_id
        assert daily.bar_available_at == _EXCEPTION_TIMESTAMP + timedelta(hours=2)
        normal_daily = next(
            point for point in chart.points if point.timestamp == _NEIGHBOR_TIMESTAMP
        )
        assert normal_daily.vwap is not None

        weekly = ListedMarketChartService(
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
        ).query(
            AaplMarketChartRequest(
                known_at=_MARKET_KNOWN_AT,
                period=AaplMarketChartPeriod.ONE_YEAR,
                interval=AaplMarketChartInterval.ONE_WEEK,
            ),
            asset_id=configuration.asset_id,
            source_id=configuration.source_id,
        )
        aggregate = next(
            point for point in weekly.points if point.timestamp == _EXCEPTION_TIMESTAMP
        )
        assert aggregate.source_session_count == 2
        assert aggregate.vwap is None
        assert aggregate.vwap_input_observation_ids == ()
        assert aggregate.volume == Decimal("1000")
        assert aggregate.trade_count == Decimal("100")

    v2_destination = (tmp_path / "v2").absolute()
    v2_destination.mkdir()
    connection = duckdb.connect(str(v2_destination / "raw-v2-index.duckdb"))
    staging = RawV2Staging(v2_destination, connection)
    with staging:
        _stage_v2_bar(staging, configuration, exceptional)
        _stage_v2_bar(staging, configuration, neighbor)
        _stage_v2_bar(staging, control, control_bar)

        def reject_raw_hydration(*args, **kwargs):
            pytest.fail("v2 reader hydrated raw records")

        monkeypatch.setattr(staging, "get_many", reject_raw_hydration)
        projection = HistoricalMarketDataV2Service(staging).query(_market_query(configuration))

        assert [item.asset_id for item in projection.bars] == [
            configuration.asset_id,
            configuration.asset_id,
        ]
        exceptional_projection = next(
            item for item in projection.bars if item.timestamp == _EXCEPTION_TIMESTAMP
        )
        assert "vwap" not in exceptional_projection.values
        assert "vwap" not in exceptional_projection.observation_ids
        assert exceptional_projection.to_market_bar().vwap is None
        assert (
            "vwap"
            in next(
                item for item in projection.bars if item.timestamp == _NEIGHBOR_TIMESTAMP
            ).values
        )


@pytest.mark.parametrize(("asset_id", "symbol"), _MARKET_ASSETS)
@pytest.mark.parametrize("omitted", ["vwap", "close", "high"])
def test_market_readers_reject_unjustified_missing_fields(
    tmp_path: Path,
    asset_id: str,
    symbol: str,
    omitted: str,
) -> None:
    configuration = _alpaca_configuration(asset_id, symbol)
    bar = _market_bar(
        configuration,
        _EXCEPTION_TIMESTAMP if omitted != "vwap" else _NEIGHBOR_TIMESTAMP,
        close=Decimal("227.41"),
        zero_activity=omitted != "vwap",
    )

    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _save_market_bar(storage, configuration, bar, omit=omitted)
        with pytest.raises(IncompleteMarketBarError, match=omitted):
            HistoricalMarketDataService(storage).query(_market_query(configuration))

    destination = (tmp_path / "v2").absolute()
    destination.mkdir()
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    staging = RawV2Staging(destination, connection)
    with staging:
        _stage_v2_bar(staging, configuration, bar, omit=omitted)
        with pytest.raises(IncompleteMarketBarError, match=omitted):
            HistoricalMarketDataV2Service(staging).query(_market_query(configuration))


def _sec_configuration(
    asset_id: str,
    ticker: str,
    cik: str,
) -> SecAssetConfiguration:
    submissions, companyfacts = sec_source_ids(ticker)
    return SecAssetConfiguration(
        asset_id=asset_id,
        cik=cik,
        ticker=ticker,
        submissions_source_id=submissions,
        companyfacts_source_id=companyfacts,
        name=f"{ticker} compatibility fixture",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _save_sec_observation(
    storage: LocalStorage,
    configuration: SecAssetConfiguration,
    *,
    field_name: str,
    value: str,
    period_end: datetime,
    period_start: datetime | None,
    available_at: datetime,
    accession: str,
    frequency: DataFrequency = DataFrequency.QUARTERLY,
    form: str = "10-Q",
    fiscal_year: str = "2013",
    fiscal_period: str = "Q3",
    identity_suffix: str = "",
) -> NormalizedObservation:
    identity = "|".join(
        (
            configuration.asset_id,
            field_name,
            period_end.isoformat(),
            period_start.isoformat() if period_start else "",
            value,
            accession,
            identity_suffix,
        )
    )
    raw_record_id = uuid5(_SEC_NAMESPACE, f"raw|{identity}")
    observation_id = uuid5(_SEC_NAMESPACE, f"observation|{identity}")
    submissions_record_id = uuid5(
        _SEC_NAMESPACE,
        f"submissions|{configuration.asset_id}|{accession}",
    )
    tag = next(item.tag for item in SEC_FACT_DEFINITIONS if item.field_name == field_name)
    record_key = json.dumps(
        {
            "accession_number": accession,
            "taxonomy": "us-gaap",
            "tag": tag,
            "unit": "USD",
            "period": period_end.date().isoformat(),
            "form": form,
            "fiscal_year": fiscal_year,
            "fiscal_period": fiscal_period,
            "companyfacts_record_id": str(raw_record_id),
            "submissions_record_id": str(submissions_record_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    source = SourceReference(
        source_id=configuration.companyfacts_source_id,
        record_key=record_key,
        retrieved_at=available_at,
        raw_uri="https://data.sec.gov/test",
        checksum_sha256="b" * 64,
    )
    storage.raw_records.save(
        RawRecord(
            record_id=raw_record_id,
            asset_id=configuration.asset_id,
            source=source,
            event_time=period_end,
            available_at=available_at,
            received_at=available_at,
            payload={"field_name": field_name, "value": value},
            schema_version="sec-equity-compatibility-v1",
        )
    )
    observation = NormalizedObservation(
        observation_id=observation_id,
        raw_record_id=raw_record_id,
        asset_id=configuration.asset_id,
        field_name=field_name,
        value=Decimal(value),
        unit="USD",
        frequency=frequency,
        observed_at=period_end,
        period_start=period_start,
        period_end=period_end,
        available_at=available_at,
        normalized_at=available_at + timedelta(minutes=1),
        source=source,
        quality=DataQuality.VALID,
        transformation_version=sec_transformation_version(configuration),
    )
    storage.observations.save(observation)
    return observation


def _seed_sec_conflict(
    storage: LocalStorage,
    configuration: SecAssetConfiguration,
    *,
    accession: str,
) -> tuple[NormalizedObservation, NormalizedObservation]:
    _save_sec_observation(
        storage,
        configuration,
        field_name="fundamental.revenue",
        value="800000",
        period_end=datetime(2012, 12, 29, tzinfo=UTC),
        period_start=datetime(2012, 9, 30, tzinfo=UTC),
        available_at=datetime(2013, 1, 30, 15, tzinfo=UTC),
        accession=f"{configuration.cik}-13-000001",
        form="10-Q",
        fiscal_year="2012",
        fiscal_period="Q3",
    )
    _save_sec_observation(
        storage,
        configuration,
        field_name="fundamental.net_income",
        value="100000",
        period_end=datetime(2012, 12, 29, tzinfo=UTC),
        period_start=datetime(2012, 9, 30, tzinfo=UTC),
        available_at=datetime(2013, 1, 30, 15, tzinfo=UTC),
        accession=f"{configuration.cik}-13-000001",
        form="10-Q",
        fiscal_year="2012",
        fiscal_period="Q3",
    )
    for field_name, value, period_start in (
        ("fundamental.assets", "4000000", None),
        ("fundamental.liabilities", "1800000", None),
        ("fundamental.stockholders_equity", "2200000", None),
    ):
        _save_sec_observation(
            storage,
            configuration,
            field_name=field_name,
            value=value,
            period_end=datetime(2012, 12, 29, tzinfo=UTC),
            period_start=period_start,
            available_at=datetime(2013, 1, 30, 15, tzinfo=UTC),
            accession=f"{configuration.cik}-13-000001",
            form="10-Q",
            fiscal_year="2012",
            fiscal_period="Q3",
        )

    for field_name, value, period_start in (
        ("fundamental.revenue", "1000000", datetime(2013, 9, 29, tzinfo=UTC)),
        ("fundamental.assets", "5000000", None),
        ("fundamental.liabilities", "2000000", None),
        ("fundamental.stockholders_equity", "3000000", None),
    ):
        _save_sec_observation(
            storage,
            configuration,
            field_name=field_name,
            value=value,
            period_end=datetime(2013, 12, 28, tzinfo=UTC),
            period_start=period_start,
            available_at=datetime(2014, 1, 29, 15, 24, 27, tzinfo=UTC),
            accession=accession,
            form="10-Q",
            fiscal_year="2013",
            fiscal_period="Q3",
        )
    first = _save_sec_observation(
        storage,
        configuration,
        field_name="fundamental.net_income",
        value="32097000",
        period_end=datetime(2013, 12, 28, tzinfo=UTC),
        period_start=datetime(2013, 9, 29, tzinfo=UTC),
        available_at=datetime(2014, 1, 29, 15, 24, 27, tzinfo=UTC),
        accession=accession,
        form="10-Q",
        fiscal_year="2013",
        fiscal_period="Q3",
    )
    second = _save_sec_observation(
        storage,
        configuration,
        field_name="fundamental.net_income",
        value="32097000",
        period_end=datetime(2013, 12, 28, tzinfo=UTC),
        period_start=datetime(2013, 9, 30, tzinfo=UTC),
        available_at=datetime(2014, 1, 29, 15, 24, 27, tzinfo=UTC),
        accession=accession,
        form="10-Q",
        fiscal_year="2013",
        fiscal_period="Q3",
    )
    return first, second


@pytest.mark.parametrize(
    ("asset_id", "ticker", "cik", "accession"),
    _SEC_ASSETS,
)
def test_sec_conflict_is_explicit_and_metrics_exclude_only_dependent_inputs(
    tmp_path: Path,
    asset_id: str,
    ticker: str,
    cik: str,
    accession: str,
) -> None:
    configuration = _sec_configuration(asset_id, ticker, cik)
    known_at = datetime(2014, 6, 1, tzinfo=UTC)
    request = SecFundamentalMetricRequest(
        asset_id=asset_id,
        known_at=known_at,
        frequency=DataFrequency.QUARTERLY,
        start_period_end=date(2013, 1, 1),
    )
    with LocalStorage(StoragePaths.from_root(tmp_path / ticker)) as storage:
        first, second = _seed_sec_conflict(storage, configuration, accession=accession)
        service = SecIssuerFundamentalPointInTimeService(storage, configuration)

        with pytest.raises(AmbiguousSecFundamentalRevisionError):
            service.query(
                SecFundamentalQuery(
                    asset_id=asset_id,
                    known_at=known_at,
                    frequency=DataFrequency.QUARTERLY,
                )
            )
        report = service.query(
            SecFundamentalQuery(
                asset_id=asset_id,
                known_at=known_at,
                frequency=DataFrequency.QUARTERLY,
                ambiguity_policy=SecFundamentalAmbiguityPolicy.REPORT,
            )
        )
        assert report.has_conflicts is True
        assert len(report.conflicts) == 1
        conflict = report.conflicts[0]
        assert conflict.field_name == "fundamental.net_income"
        assert conflict.reason == "equal_available_at_semantic_conflict"
        assert conflict.tied_available_at == (datetime(2014, 1, 29, 15, 24, 27, tzinfo=UTC),)
        assert {candidate.value for candidate in conflict.candidates} == {Decimal("32097000")}
        assert {candidate.period_start for candidate in conflict.candidates} == {
            datetime(2013, 9, 29, tzinfo=UTC),
            datetime(2013, 9, 30, tzinfo=UTC),
        }
        current_period = next(
            period
            for period in report.periods
            if period.period_end == datetime(2013, 12, 28, tzinfo=UTC)
        )
        assert current_period.conflict_fields == ("fundamental.net_income",)
        assert "fundamental.net_income" in current_period.missing_fields
        assert "fundamental.net_income" not in current_period.available_fields
        assert all(fact.field_name != "fundamental.net_income" for fact in current_period.facts)
        assert {candidate.observation_id for candidate in conflict.candidates} == {
            first.observation_id,
            second.observation_id,
        }
        payload = report.to_json_dict()
        assert payload["query"]["ambiguity_policy"] == "report"
        assert payload["has_conflicts"] is True
        assert payload["conflicts"][0]["field_name"] == "fundamental.net_income"

        pipeline = SecIssuerFundamentalMetricPipeline(
            storage,
            service,
            SecFundamentalMetricEngine(configuration),
            configuration=configuration,
            clock=lambda: datetime(2014, 6, 2, tzinfo=UTC),
        )
        summary = pipeline.run(request)
        assert summary.has_conflicts is True
        assert summary.conflict_count == 1
        assert summary.conflicts == report.conflicts
        assert summary.skipped_counts["ambiguous_current_input"] == 2
        assert set(summary.metric_counts) == {
            "fundamental.liabilities_to_assets",
            "fundamental.liabilities_to_equity",
            "fundamental.revenue_yoy_growth",
        }
        conflict_ids = {candidate.observation_id for candidate in conflict.candidates}
        assert all(
            not set(result.input_observation_ids) & conflict_ids
            for result in storage.metric_results.list(asset_id=asset_id)
        )
        assert summary.to_json_dict()["conflict_count"] == 1
        assert summary.to_json_dict()["conflicts"][0]["field_name"] == ("fundamental.net_income")

        before = service.query(
            SecFundamentalQuery(
                asset_id=asset_id,
                known_at=datetime(2014, 1, 29, 15, 24, 26, tzinfo=UTC),
                frequency=DataFrequency.QUARTERLY,
                start_period_end=date(2013, 10, 1),
                ambiguity_policy=SecFundamentalAmbiguityPolicy.REPORT,
            )
        )
        assert before.has_conflicts is False
        assert before.periods == ()


@pytest.mark.parametrize(
    ("asset_id", "ticker", "cik", "accession"),
    _SEC_ASSETS,
)
def test_sec_conflict_only_period_is_represented_without_invented_facts(
    tmp_path: Path,
    asset_id: str,
    ticker: str,
    cik: str,
    accession: str,
) -> None:
    configuration = _sec_configuration(asset_id, ticker, cik)
    period_end = datetime(2014, 3, 29, tzinfo=UTC)
    available_at = datetime(2014, 4, 29, 15, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path / ticker)) as storage:
        for period_start in (
            datetime(2013, 12, 29, tzinfo=UTC),
            datetime(2013, 12, 30, tzinfo=UTC),
        ):
            _save_sec_observation(
                storage,
                configuration,
                field_name="fundamental.net_income",
                value="32097000",
                period_end=period_end,
                period_start=period_start,
                available_at=available_at,
                accession=accession,
                form="10-Q",
                fiscal_year="2014",
                fiscal_period="Q3",
            )
        result = SecIssuerFundamentalPointInTimeService(
            storage,
            configuration,
        ).query(
            SecFundamentalQuery(
                asset_id=asset_id,
                known_at=datetime(2014, 6, 1, tzinfo=UTC),
                frequency=DataFrequency.QUARTERLY,
                start_period_end=date(2014, 1, 1),
                end_period_end=date(2014, 12, 31),
                ambiguity_policy=SecFundamentalAmbiguityPolicy.REPORT,
            )
        )

    assert result.periods_returned == 1
    period = result.periods[0]
    assert period.facts == ()
    assert period.available_fields == ()
    assert period.conflict_fields == ("fundamental.net_income",)
    assert period.is_complete is False
    assert period.latest_available_at == available_at
    assert result.observations_selected == 0
    assert len(result.conflicts) == 1


@pytest.mark.parametrize(
    ("case", "expected_conflict"),
    [
        ("equivalent", False),
        ("different_available", False),
        ("different_value", True),
    ],
)
def test_sec_equivalent_and_separate_revisions_remain_deterministic(
    tmp_path: Path,
    case: str,
    expected_conflict: bool,
) -> None:
    configuration = _sec_configuration("equity:us:mog.a", "MOG.A", "0000067887")
    period_end = datetime(2013, 12, 28, tzinfo=UTC)
    available_at = datetime(2014, 1, 29, 15, 24, 27, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path / case)) as storage:
        if case == "equivalent":
            for suffix in ("a", "b"):
                _save_sec_observation(
                    storage,
                    configuration,
                    field_name="fundamental.revenue",
                    value="1000000",
                    period_end=period_end,
                    period_start=datetime(2013, 9, 29, tzinfo=UTC),
                    available_at=available_at,
                    accession="0000067887-14-000005",
                    identity_suffix=suffix,
                )
        elif case == "different_available":
            _save_sec_observation(
                storage,
                configuration,
                field_name="fundamental.revenue",
                value="1000000",
                period_end=period_end,
                period_start=datetime(2013, 9, 29, tzinfo=UTC),
                available_at=available_at,
                accession="0000067887-14-000005",
            )
            _save_sec_observation(
                storage,
                configuration,
                field_name="fundamental.revenue",
                value="1100000",
                period_end=period_end,
                period_start=datetime(2013, 9, 29, tzinfo=UTC),
                available_at=available_at + timedelta(days=1),
                accession="0000067887-14-000006",
            )
        else:
            for value in ("1000000", "1100000"):
                _save_sec_observation(
                    storage,
                    configuration,
                    field_name="fundamental.revenue",
                    value=value,
                    period_end=period_end,
                    period_start=datetime(2013, 9, 29, tzinfo=UTC),
                    available_at=available_at,
                    accession="0000067887-14-000005",
                )

        result = SecIssuerFundamentalPointInTimeService(
            storage,
            configuration,
        ).query(
            SecFundamentalQuery(
                asset_id=configuration.asset_id,
                known_at=datetime(2014, 6, 1, tzinfo=UTC),
                frequency=DataFrequency.QUARTERLY,
                ambiguity_policy=SecFundamentalAmbiguityPolicy.REPORT,
            )
        )

    assert result.has_conflicts is expected_conflict
    if expected_conflict:
        assert result.observations_selected == 0
        assert len(result.conflicts) == 1
    else:
        assert result.conflicts == ()
        assert result.observations_selected == 1
        expected_value = Decimal("1100000") if case == "different_available" else Decimal("1000000")
        assert result.periods[0].facts[0].value == expected_value


def test_market_reader_1x_10x_foreign_corpus_preserves_identity(tmp_path: Path) -> None:
    target = _alpaca_configuration("equity:us:mog.a", "MOG.A")
    foreign = _alpaca_configuration("equity:us:aapl", "AAPL")
    target_bars = (
        _market_bar(target, _NEIGHBOR_TIMESTAMP, close=Decimal("226.00")),
        _market_bar(
            target,
            _EXCEPTION_TIMESTAMP,
            close=Decimal("227.41"),
            zero_activity=True,
        ),
        _market_bar(target, _LATER_TIMESTAMP, close=Decimal("228.00")),
    )

    with LocalStorage(StoragePaths.from_root(tmp_path / "1x")) as one_x:
        for bar in target_bars:
            _save_market_bar(one_x, target, bar)
        one_series = HistoricalMarketDataService(one_x).query(_market_query(target))

    with LocalStorage(StoragePaths.from_root(tmp_path / "10x")) as ten_x:
        for bar in target_bars:
            _save_market_bar(ten_x, target, bar)
        foreign_raw_ids: set[UUID] = set()
        foreign_observation_ids: set[UUID] = set()
        for index in range(10):
            raw, observations = _save_market_bar(
                ten_x,
                foreign,
                _market_bar(
                    foreign,
                    datetime(2025, 10, 1, 5, tzinfo=UTC) + timedelta(days=index),
                    close=Decimal("50") + Decimal(index),
                ),
            )
            foreign_raw_ids.add(raw.record_id)
            foreign_observation_ids.update(item.observation_id for item in observations)
        raw_calls: list[tuple[UUID, ...]] = []
        original_get_many = ten_x.raw_records.get_many

        def spy_get_many(identifiers):
            raw_calls.append(tuple(identifiers))
            return original_get_many(identifiers)

        ten_x.raw_records.get_many = spy_get_many  # type: ignore[method-assign]
        ten_series = HistoricalMarketDataService(ten_x).query(_market_query(target))

    assert ten_series == one_series
    assert raw_calls
    assert all(not set(identifiers) & foreign_raw_ids for identifiers in raw_calls)
    assert (
        not {identifier for bar in ten_series.bars for identifier in bar.observation_ids.values()}
        & foreign_observation_ids
    )


def test_sec_selector_1x_10x_foreign_corpus_preserves_identity_and_pit(tmp_path: Path) -> None:
    target = _sec_configuration("equity:us:mog.a", "MOG.A", "0000067887")
    foreign = _sec_configuration("equity:us:aapl", "AAPL", "0000320193")
    known_at = datetime(2014, 6, 1, tzinfo=UTC)
    query_values = {
        "asset_id": target.asset_id,
        "known_at": known_at,
        "frequency": DataFrequency.QUARTERLY,
        "ambiguity_policy": SecFundamentalAmbiguityPolicy.REPORT,
    }

    with LocalStorage(StoragePaths.from_root(tmp_path / "1x")) as one_x:
        _seed_sec_conflict(one_x, target, accession="0000067887-14-000005")
        one_x_result = SecIssuerFundamentalPointInTimeService(one_x, target).query(
            SecFundamentalQuery(**query_values)
        )

    with LocalStorage(StoragePaths.from_root(tmp_path / "10x")) as ten_x:
        _seed_sec_conflict(ten_x, target, accession="0000067887-14-000005")
        for index in range(10):
            _save_sec_observation(
                ten_x,
                foreign,
                field_name="fundamental.revenue",
                value=str(900000 + index),
                period_end=datetime(2013, 12, 28, tzinfo=UTC),
                period_start=datetime(2013, 9, 29, tzinfo=UTC),
                available_at=datetime(2014, 1, 29, 15, 24, 27, tzinfo=UTC),
                accession="0000320193-14-000005",
            )
            _save_sec_observation(
                ten_x,
                foreign,
                field_name="fundamental.net_income",
                value=str(300000 + index),
                period_end=datetime(2013, 12, 28, tzinfo=UTC),
                period_start=datetime(2013, 9, 30, tzinfo=UTC),
                available_at=datetime(2014, 1, 29, 15, 24, 27, tzinfo=UTC),
                accession="0000320193-14-000005",
            )
        calls: list[dict[str, object]] = []
        original_list = ten_x.observations.list

        def spy_list(**kwargs):
            calls.append(kwargs)
            return original_list(**kwargs)

        ten_x.observations.list = spy_list  # type: ignore[method-assign]
        ten_x_result = SecIssuerFundamentalPointInTimeService(ten_x, target).query(
            SecFundamentalQuery(**query_values)
        )
        returned = original_list(**calls[0]) if calls else []

    assert len(calls) == 1
    assert all(item.asset_id == target.asset_id for item in returned)
    assert ten_x_result.to_json_dict() == one_x_result.to_json_dict()
    assert ten_x_result.observations_examined == one_x_result.observations_examined
    assert ten_x_result.conflicts == one_x_result.conflicts
    assert [fact.observation_id for period in ten_x_result.periods for fact in period.facts] == [
        fact.observation_id for period in one_x_result.periods for fact in period.facts
    ]
