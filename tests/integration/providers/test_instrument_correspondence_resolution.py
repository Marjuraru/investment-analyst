from investment_analyst.catalog.provider_configuration import resolve_sec_cusip_binding
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService


def test_default_catalog_exposes_declared_sec_cusip_binding() -> None:
    resolver = ProviderAssetContextResolver(AssetCatalogService.load_default())

    assert resolve_sec_cusip_binding(resolver) == "037833100"
