"""Tests for asset contracts and public model imports."""

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import Asset, AssetClass


def test_create_equity_and_crypto_assets() -> None:
    equity = Asset(
        asset_id="equity:us:aapl",
        symbol=" AAPL ",
        name=" Apple Inc. ",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
        provider_symbols={"alpaca": "AAPL"},
        is_active=True,
    )
    crypto = Asset(
        asset_id="crypto:btc-usd",
        symbol="BTC",
        name="Bitcoin",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        provider_symbols={"coinbase": "BTC-USD"},
        is_active=True,
    )

    assert equity.symbol == "AAPL"
    assert equity.asset_class is AssetClass.EQUITY
    assert crypto.asset_id == "crypto:btc-usd"
    assert crypto.asset_class is AssetClass.CRYPTO


def test_asset_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Asset(
            asset_id="equity:us:aapl",
            symbol="AAPL",
            name="Apple Inc.",
            asset_class=AssetClass.EQUITY,
            quote_currency="USD",
            unexpected="value",
        )
