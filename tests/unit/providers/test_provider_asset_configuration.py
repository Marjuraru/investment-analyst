"""Unit coverage for typed configurations built from the central catalog."""

import pytest
from pydantic import ValidationError

from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_coinbase_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import (
    ProviderAssetContextResolver,
    ProviderAssetNotConfiguredError,
)
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.asset_config import (
    AlpacaAssetConfiguration,
    CoinbaseAssetConfiguration,
    SecAssetConfiguration,
)
from investment_analyst.providers.crypto.coinbase_exchange import DAILY_GRANULARITY_SECONDS
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID as COINBASE_ASSET_ID,
)
from investment_analyst.providers.crypto.coinbase_normalizer import PRODUCT_ID, SOURCE_ID
from investment_analyst.providers.fundamentals.sec_edgar import APPLE_CIK, APPLE_TICKER
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID as APPLE_ASSET_ID
from investment_analyst.providers.fundamentals.sec_raw_records import (
    COMPANY_FACTS_SOURCE_ID,
    SUBMISSIONS_SOURCE_ID,
)
from investment_analyst.providers.market.alpaca_normalizer import (
    SOURCE_ID as ALPACA_SOURCE_ID,
)
from investment_analyst.providers.market.alpaca_stock import ADJUSTMENT, FEED


def _resolver() -> ProviderAssetContextResolver:
    return ProviderAssetContextResolver(AssetCatalogService.load_default())


def test_factories_preserve_current_provider_and_persisted_identities() -> None:
    alpaca = resolve_alpaca_configuration(_resolver())
    coinbase = resolve_coinbase_configuration(_resolver())
    sec = resolve_sec_configuration(_resolver())

    assert alpaca == AlpacaAssetConfiguration(
        asset_id=APPLE_ASSET_ID,
        symbol=APPLE_TICKER,
        feed=FEED,
        adjustment=ADJUSTMENT,
        source_id=ALPACA_SOURCE_ID,
    )
    assert coinbase == CoinbaseAssetConfiguration(
        asset_id=COINBASE_ASSET_ID,
        product_id=PRODUCT_ID,
        source_id=SOURCE_ID,
        granularity_seconds=DAILY_GRANULARITY_SECONDS,
    )
    assert sec == SecAssetConfiguration(
        asset_id=APPLE_ASSET_ID,
        cik=APPLE_CIK,
        ticker=APPLE_TICKER,
        submissions_source_id=SUBMISSIONS_SOURCE_ID,
        companyfacts_source_id=COMPANY_FACTS_SOURCE_ID,
    )
    assert len(sec.cik) == 10


def test_configurations_are_strict_frozen_and_preserve_identifier_text() -> None:
    configuration = resolve_sec_configuration(_resolver())

    with pytest.raises(ValidationError):
        configuration.cik = "1"
    with pytest.raises(ValidationError):
        SecAssetConfiguration(
            asset_id=APPLE_ASSET_ID,
            cik="320193",
            ticker=APPLE_TICKER,
            submissions_source_id=SUBMISSIONS_SOURCE_ID,
            companyfacts_source_id=COMPANY_FACTS_SOURCE_ID,
        )
    with pytest.raises(ValidationError):
        CoinbaseAssetConfiguration(
            asset_id=COINBASE_ASSET_ID,
            product_id=PRODUCT_ID,
            source_id=SOURCE_ID,
            granularity_seconds=True,
        )


def test_invalid_provider_asset_pairs_fail_before_client_construction() -> None:
    resolver = _resolver()

    with pytest.raises(ProviderAssetNotConfiguredError):
        resolve_coinbase_configuration(resolver, asset_id=APPLE_ASSET_ID)
    with pytest.raises(ProviderAssetNotConfiguredError):
        resolve_sec_configuration(resolver, asset_id=COINBASE_ASSET_ID)
