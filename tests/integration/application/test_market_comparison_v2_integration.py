"""End-to-end HTTP comparison over one real v2 workspace and application facade."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from investment_analyst.core.models import AssetClass
from investment_analyst.frontend.local_web import (
    AaplLocalController,
    AaplLocalHttpServer,
    AaplLocalWebApplication,
)
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_normalizer import (
    alpaca_source_id,
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials, AlpacaStockBar
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_BASE = datetime(2025, 1, 2, 16, tzinfo=UTC)
_KNOWN_AT = datetime(2025, 2, 6, tzinfo=UTC)
_BAR_COUNT = 25


def _configuration(asset_id: str, symbol: str) -> AlpacaAssetConfiguration:
    return AlpacaAssetConfiguration(
        asset_id=asset_id,
        symbol=symbol,
        feed="iex",
        adjustment="all",
        source_id=alpaca_source_id(symbol),
        name=f"{symbol} Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _bar(symbol: str, index: int) -> AlpacaStockBar:
    timestamp = _BASE + timedelta(days=index)
    close = Decimal("150") + Decimal(index) / Decimal("8")
    open_value = close - Decimal("0.25")
    return AlpacaStockBar(
        symbol=symbol,
        timestamp=timestamp,
        open=open_value,
        high=close + Decimal("1"),
        low=open_value - Decimal("1"),
        close=close,
        volume=Decimal("10000") + Decimal(index * 11),
        trade_count=Decimal("100") + Decimal(index),
        vwap=close,
        raw_values={
            "t": timestamp.isoformat().replace("+00:00", "Z"),
            "o": str(open_value),
            "h": str(close + Decimal("1")),
            "l": str(open_value - Decimal("1")),
            "c": str(close),
            "v": str(Decimal("10000") + Decimal(index * 11)),
            "n": str(Decimal("100") + Decimal(index)),
            "vw": str(close),
        },
    )


def _seed(storage: LocalStorage, configurations: tuple[AlpacaAssetConfiguration, ...]) -> None:
    for configuration in configurations:
        for index in range(_BAR_COUNT):
            bar = _bar(configuration.symbol, index)
            available_at = bar.timestamp + timedelta(hours=2)
            raw = bar_to_raw_record(
                bar,
                retrieved_at=available_at,
                request_url="https://data.alpaca.markets/comparison-v2",
                configuration=configuration,
            )
            storage.raw_records.save(raw)
            storage.observations.save_many(
                bar_to_observations(
                    bar,
                    raw,
                    normalized_at=available_at + timedelta(minutes=1),
                    configuration=configuration,
                )
            )


def _request(port: int) -> dict[str, object]:
    query = urlencode(
        [
            ("asset_id", "equity:us:aapl"),
            ("asset_id", "equity:us:amd"),
            ("benchmark_id", "equity:us:aapl"),
            ("start", "2025-01-02"),
            ("end", "2025-02-05"),
            ("known_at", _KNOWN_AT.isoformat().replace("+00:00", "Z")),
        ]
    )
    with urlopen(
        f"http://127.0.0.1:{port}/api/v1/market-comparison?{query}",
        timeout=30,
    ) as response:
        return {
            "status": response.status,
            "payload": json.loads(response.read()),
        }


def test_real_http_facade_comparison_uses_v2_read_only_without_writes(tmp_path: Path) -> None:
    initialized = WorkspaceService().initialize(
        tmp_path / "workspace",
        format_version=2,
    )
    workspace = initialized.paths.root
    paths = StoragePaths.from_workspace_root(
        workspace,
        format_version=initialized.manifest.format_version,
        workspace_id=initialized.manifest.workspace_id,
    )
    configurations = (
        _configuration("equity:us:aapl", "AAPL"),
        _configuration("equity:us:amd", "AMD"),
    )
    with LocalStorage(paths) as storage:
        _seed(storage, configurations)
    before = paths.database_path.read_bytes()

    controller = AaplLocalController.create_default(
        workspace=workspace,
        alpaca_credentials=AlpacaCredentials(api_key="comparison", secret_key="comparison"),
        sec_identity=SecEdgarIdentity("Investment Analyst comparison@example.invalid"),
    )
    server = AaplLocalHttpServer(("127.0.0.1", 0), AaplLocalWebApplication(controller, None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _request(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["status"] == 200
    payload = result["payload"]
    assert payload["schema_version"] == "market-multi-asset-comparison-v1"
    assert payload["benchmark_id"] == "equity:us:aapl"
    assert payload["asset_ids"] == ["equity:us:aapl", "equity:us:amd"]
    assert len(payload["common_dates"]) == _BAR_COUNT
    assert len(payload["series"]) == 2
    for series in payload["series"]:
        assert len(series["points"]) == _BAR_COUNT
        assert series["points"][0]["available_at"]
        assert series["points"][0]["close_observation_id"]
    assert paths.database_path.read_bytes() == before
