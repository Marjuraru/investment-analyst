"""Tests for catalog/provider/domain/frequency runtime resolution."""

import pytest

from investment_analyst.application.capability_runtime import (
    RuntimeOperationRoute,
    build_capability_runtime_plan,
)
from investment_analyst.application.facade import InvestmentAnalystApplication


def test_default_runtime_routes_aapl_other_sec_and_btc_without_symbol_dispatch() -> None:
    universe = InvestmentAnalystApplication.create_default().list_market_assets()
    plan = build_capability_runtime_plan(universe)

    apple = plan.resolve(
        asset_id="equity:us:aapl",
        provider="alpaca",
        domain="market",
        frequency="day_1",
    )
    amd = plan.resolve(
        asset_id="equity:us:amd",
        provider="sec-edgar",
        domain="fundamentals",
        frequency="quarterly",
    )
    bitcoin_daily = plan.resolve(
        asset_id="crypto:btc-usd",
        provider="coinbase",
        domain="market",
        frequency="day_1",
    )
    bitcoin_intraday = plan.resolve(
        asset_id="crypto:btc-usd",
        provider="coinbase",
        domain="market",
        frequency="minute_1",
    )

    assert apple.route is RuntimeOperationRoute.LISTED_MARKET_DAILY
    assert apple.source_ids == ("alpaca-market-data:iex:aapl:daily-bars:adjustment-all",)
    assert amd.route is RuntimeOperationRoute.CORPORATE_FUNDAMENTALS
    assert amd.source_ids == ("sec-edgar:amd:companyfacts", "sec-edgar:amd:submissions")
    assert bitcoin_daily.route is RuntimeOperationRoute.CRYPTO_SPOT_DAILY
    assert bitcoin_daily.source_ids == ("coinbase-exchange:btc-usd:daily-candles",)
    assert bitcoin_intraday.route is RuntimeOperationRoute.CRYPTO_SPOT_INTRADAY


def test_runtime_rejects_missing_capability_before_provider_work() -> None:
    plan = build_capability_runtime_plan(
        InvestmentAnalystApplication.create_default().list_market_assets()
    )

    with pytest.raises(ValueError, match="not configured"):
        plan.resolve(
            asset_id="crypto:btc-usd",
            provider="sec-edgar",
            domain="fundamentals",
            frequency="quarterly",
        )
