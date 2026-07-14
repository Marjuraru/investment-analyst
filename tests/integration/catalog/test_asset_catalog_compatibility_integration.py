"""Compatibility checks between the packaged catalog and current provider constants."""

from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID as COINBASE_ASSET_ID,
)
from investment_analyst.providers.crypto.coinbase_normalizer import PRODUCT_ID
from investment_analyst.providers.fundamentals.sec_edgar import APPLE_CIK, APPLE_TICKER
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID as APPLE_ASSET_ID
from investment_analyst.providers.market.alpaca_normalizer import SYMBOL as ALPACA_SYMBOL


def test_default_catalog_matches_existing_provider_constants() -> None:
    service = AssetCatalogService.load_default()

    apple = service.get(APPLE_ASSET_ID)
    assert APPLE_TICKER in apple.aliases
    assert (
        service.get_binding(
            APPLE_ASSET_ID,
            provider="alpaca",
            namespace="symbol",
        ).identifier
        == ALPACA_SYMBOL
    )
    assert (
        service.get_binding(
            APPLE_ASSET_ID,
            provider="sec",
            namespace="cik",
        ).identifier
        == APPLE_CIK
    )

    coinbase = service.get(COINBASE_ASSET_ID)
    assert (
        service.get_binding(
            COINBASE_ASSET_ID,
            provider="coinbase",
            namespace="product_id",
        ).identifier
        == PRODUCT_ID
    )
    assert apple.asset_id != coinbase.asset_id


def test_catalog_capabilities_match_current_working_integrations() -> None:
    service = AssetCatalogService.load_default()
    assert service.supports(APPLE_ASSET_ID, "market.daily_bars")
    assert service.supports(APPLE_ASSET_ID, "fundamentals.company_facts")
    assert service.supports(APPLE_ASSET_ID, "fundamentals.submissions")
    assert service.supports(COINBASE_ASSET_ID, "market.daily_bars")
    assert not service.supports(COINBASE_ASSET_ID, "fundamentals.company_facts")
