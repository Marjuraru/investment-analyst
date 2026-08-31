"""Acceptance coverage for the twelve catalog additions."""

from investment_analyst.catalog.service import AssetCatalogService


def test_catalog_contains_exact_new_assets_with_expected_daily_routes() -> None:
    catalog = AssetCatalogService.load_default()
    added = {
        "equity:us:msft",
        "equity:us:nvda",
        "equity:us:amzn",
        "equity:us:cvx",
        "equity:us:jnj",
        "equity:us:cat",
        "etf:us:spy",
        "etf:us:qqq",
        "etf:us:tlt",
        "crypto:sol-usd",
        "crypto:ada-usd",
        "crypto:link-usd",
    }
    assert added <= {asset.asset_id for asset in catalog.list_assets()}
    assert len(catalog.list_assets()) == 37
    assert len(catalog.list_assets(capability="market.daily_bars")) == 31
