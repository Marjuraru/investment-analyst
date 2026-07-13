"""Deterministic fixtures for the simulated vertical pipeline."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from investment_analyst.core.models import Asset, AssetClass, SourceDefinition, SourceType


@dataclass(frozen=True, slots=True)
class SimulatedBar:
    """One fictitious daily market bar used only by the demonstration."""

    asset_id: str
    provider_symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: Decimal


AAPL_ASSET_ID = "equity:us:aapl"
BTC_ASSET_ID = "crypto:btc-usd"
SIMULATED_SOURCE_ID = "simulated:daily-bars"


def simulated_assets() -> tuple[Asset, Asset]:
    """Return the two deterministic assets used by the simulation."""
    return (
        Asset(
            asset_id=AAPL_ASSET_ID,
            symbol="AAPL",
            name="Apple Inc.",
            asset_class=AssetClass.EQUITY,
            quote_currency="USD",
            exchange="NASDAQ",
            provider_symbols={"simulated": "AAPL"},
            is_active=True,
        ),
        Asset(
            asset_id=BTC_ASSET_ID,
            symbol="BTC",
            name="Bitcoin",
            asset_class=AssetClass.CRYPTO,
            quote_currency="USD",
            exchange="SIMULATED",
            provider_symbols={"simulated": "BTC-USD"},
            is_active=True,
        ),
    )


def simulated_source() -> SourceDefinition:
    """Return the explicitly fictitious market-bar source definition."""
    return SourceDefinition(
        source_id=SIMULATED_SOURCE_ID,
        provider_name="Simulated Provider",
        dataset_name="Simulated Daily Market Bars",
        source_type=SourceType.MARKET,
        is_official=False,
        coverage_notes="Fictitious deterministic data for local technical demonstrations only.",
    )


def simulated_bars() -> tuple[SimulatedBar, ...]:
    """Return three consecutive UTC daily bars for each simulated asset."""
    return (
        SimulatedBar(
            asset_id=AAPL_ASSET_ID,
            provider_symbol="AAPL",
            timestamp=datetime(2026, 7, 8, 20, 0, tzinfo=UTC),
            open=Decimal("203.00"),
            high=Decimal("206.00"),
            low=Decimal("202.50"),
            close=Decimal("205.00"),
            volume=Decimal("50000000"),
            trade_count=Decimal("650000"),
        ),
        SimulatedBar(
            asset_id=AAPL_ASSET_ID,
            provider_symbol="AAPL",
            timestamp=datetime(2026, 7, 9, 20, 0, tzinfo=UTC),
            open=Decimal("205.50"),
            high=Decimal("208.20"),
            low=Decimal("204.80"),
            close=Decimal("207.05"),
            volume=Decimal("55000000"),
            trade_count=Decimal("710000"),
        ),
        SimulatedBar(
            asset_id=AAPL_ASSET_ID,
            provider_symbol="AAPL",
            timestamp=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
            open=Decimal("208.00"),
            high=Decimal("212.00"),
            low=Decimal("207.50"),
            close=Decimal("211.19"),
            volume=Decimal("82500000"),
            trade_count=Decimal("980000"),
        ),
        SimulatedBar(
            asset_id=BTC_ASSET_ID,
            provider_symbol="BTC-USD",
            timestamp=datetime(2026, 7, 8, 20, 0, tzinfo=UTC),
            open=Decimal("107500.00"),
            high=Decimal("109200.00"),
            low=Decimal("106800.00"),
            close=Decimal("108000.00"),
            volume=Decimal("12000"),
            trade_count=Decimal("180000"),
        ),
        SimulatedBar(
            asset_id=BTC_ASSET_ID,
            provider_symbol="BTC-USD",
            timestamp=datetime(2026, 7, 9, 20, 0, tzinfo=UTC),
            open=Decimal("108100.00"),
            high=Decimal("108500.00"),
            low=Decimal("105900.00"),
            close=Decimal("106920.00"),
            volume=Decimal("13200"),
            trade_count=Decimal("195000"),
        ),
        SimulatedBar(
            asset_id=BTC_ASSET_ID,
            provider_symbol="BTC-USD",
            timestamp=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
            open=Decimal("107000.00"),
            high=Decimal("110000.00"),
            low=Decimal("106700.00"),
            close=Decimal("109058.40"),
            volume=Decimal("19800"),
            trade_count=Decimal("260000"),
        ),
    )
