"""Unit tests for deterministic indexed asset catalog queries."""

import json
from importlib import resources
from pathlib import Path

import pytest

from investment_analyst.catalog.models import (
    AssetCatalogDocument,
    CatalogAsset,
    ProviderBinding,
)
from investment_analyst.catalog.service import (
    AmbiguousAssetAliasError,
    AmbiguousProviderBindingError,
    AssetAliasNotFoundError,
    AssetCatalogFormatError,
    AssetCatalogService,
    AssetCatalogVersionError,
    AssetNotFoundError,
    ProviderBindingNotFoundError,
)
from investment_analyst.core.models import AssetClass

_DEFAULT_ASSET_IDS = [
    "crypto:ada-usd",
    "crypto:btc-usd",
    "crypto:eth-usd",
    "crypto:link-usd",
    "crypto:sol-usd",
    "equity:pe:bvl:bvn",
    "equity:pe:bvl:cverdec1",
    "equity:pe:bvl:minsuri1",
    "equity:pe:bvl:pomalcc1",
    "equity:pe:bvl:scco",
    "equity:pe:bvl:volcabc1",
    "equity:us:aapl",
    "equity:us:amd",
    "equity:us:amzn",
    "equity:us:b",
    "equity:us:bvn",
    "equity:us:cat",
    "equity:us:cde",
    "equity:us:cvx",
    "equity:us:hymc",
    "equity:us:intc",
    "equity:us:jnj",
    "equity:us:msft",
    "equity:us:mstr",
    "equity:us:mu",
    "equity:us:mux",
    "equity:us:nem",
    "equity:us:nvda",
    "equity:us:pltr",
    "equity:us:scco",
    "equity:us:tsm",
    "etf:us:gbtc",
    "etf:us:gld",
    "etf:us:ibit",
    "etf:us:qqq",
    "etf:us:spy",
    "etf:us:tlt",
]


def _catalog_asset(
    *,
    asset_id: str,
    symbol: str,
    alias: str,
    bindings: tuple[ProviderBinding, ...],
) -> CatalogAsset:
    return CatalogAsset(
        asset_id=asset_id,
        symbol=symbol,
        name=symbol,
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        provider_symbols={},
        aliases=(alias,),
        provider_bindings=bindings,
    )


def _binding(identifier: str) -> ProviderBinding:
    return ProviderBinding(
        provider="alpaca",
        namespace="symbol",
        identifier=identifier,
        capabilities=("market.daily_bars",),
    )


def test_default_catalog_loads_and_lists_deterministically() -> None:
    service = AssetCatalogService.load_default()
    assert service.catalog_version == 1
    assert [asset.asset_id for asset in service.list_assets()] == _DEFAULT_ASSET_IDS
    assert service.list_assets(asset_type=AssetClass.EQUITY)[0].symbol == "BVN"
    assert [asset.asset_id for asset in service.list_assets(capability="market.daily_bars")] == [
        asset_id for asset_id in _DEFAULT_ASSET_IDS if not asset_id.startswith("equity:pe:bvl:")
    ]
    assert [asset.asset_id for asset in service.list_assets(capability="market.minute_bars")] == [
        "crypto:btc-usd"
    ]


def test_default_catalog_resource_is_canonical_utf8_json() -> None:
    resource = resources.files("investment_analyst.catalog").joinpath("default_assets.v1.json")
    text = resource.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert text == f"{json.dumps(payload, indent=2, sort_keys=True)}\n"
    assert "https://" not in text
    assert "created_at" not in text
    assert "credential" not in text.casefold()


def test_default_resource_is_read_once(monkeypatch) -> None:
    calls = 0
    original = Path.read_text

    def counted(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    service = AssetCatalogService.load_default()
    service.get("equity:us:aapl")
    service.resolve_alias("aapl")
    assert calls == 1


def test_alias_binding_reverse_lookup_and_capabilities() -> None:
    service = AssetCatalogService.load_default()
    apple = service.resolve_alias("aApL")
    sec = service.get_binding(apple.asset_id, provider="SEC", namespace="CIK")
    assert sec.identifier == "0000320193"
    assert (
        service.resolve_external(
            provider="alpaca",
            namespace="symbol",
            identifier="AAPL",
        )
        == apple
    )
    assert (
        service.resolve_external(
            provider="sec",
            namespace="cik",
            identifier="0000320193",
        )
        == apple
    )
    assert service.supports(apple.asset_id, "fundamentals.company_facts") is True
    assert service.supports(apple.asset_id, "market.options") is False


def test_coinbase_asset_and_product_binding_are_available() -> None:
    service = AssetCatalogService.load_default()
    bitcoin = service.resolve_alias("bitcoin")
    binding = service.get_binding(
        bitcoin.asset_id,
        provider="coinbase",
        namespace="product_id",
    )
    assert binding.identifier == "BTC-USD"
    assert (
        service.resolve_external(
            provider="coinbase",
            namespace="product_id",
            identifier="BTC-USD",
        )
        == bitcoin
    )
    assert service.supports(bitcoin.asset_id, "market.minute_bars")
    ethereum = service.resolve_alias("ethereum")
    assert ethereum.asset_id == "crypto:eth-usd"
    assert ethereum.is_active is True
    assert (
        service.get_binding(
            ethereum.asset_id,
            provider="coinbase",
            namespace="product_id",
        ).identifier
        == "ETH-USD"
    )
    assert not service.supports(ethereum.asset_id, "market.minute_bars")


def test_deribit_bindings_are_exact_ordered_and_capability_scoped() -> None:
    service = AssetCatalogService.load_default()

    for asset_id, currency in (
        ("crypto:btc-usd", "BTC"),
        ("crypto:eth-usd", "ETH"),
    ):
        asset = service.get(asset_id)
        deribit = tuple(
            binding for binding in asset.provider_bindings if binding.provider == "deribit"
        )
        assert tuple(binding.namespace for binding in deribit) == (
            "currency",
            "instrument_name",
        )
        assert tuple(binding.identifier for binding in deribit) == (
            currency,
            f"{currency}-PERPETUAL",
        )
        assert service.supports(asset_id, "derivatives.funding.hourly")
        assert service.supports(asset_id, "derivatives.perpetual.snapshot")
        assert service.supports(asset_id, "derivatives.volatility_index.daily")
    assert [
        item.asset_id for item in service.list_assets(capability="derivatives.funding.hourly")
    ] == ["crypto:btc-usd", "crypto:eth-usd"]
    assert not service.supports("equity:us:aapl", "derivatives.funding.hourly")


def test_bvl_listings_have_separate_exchange_identity_and_corroborated_isin() -> None:
    service = AssetCatalogService.load_default()
    cerro = service.resolve_external(
        provider="bvl",
        namespace="mnemonic",
        identifier="CVERDEC1",
    )
    bvl_bvn = service.resolve_external(
        provider="bvl",
        namespace="mnemonic",
        identifier="BVN",
    )
    us_bvn = service.resolve_external(
        provider="alpaca",
        namespace="symbol",
        identifier="BVN",
    )

    assert cerro.asset_id == "equity:pe:bvl:cverdec1"
    assert cerro.exchange == "BVL"
    assert (
        service.get_binding(
            cerro.asset_id,
            provider="bvl",
            namespace="isin",
        ).identifier
        == "PEP646501002"
    )
    assert (
        service.get_binding(
            cerro.asset_id,
            provider="smv",
            namespace="security_code",
        ).identifier
        == "64650100"
    )
    assert bvl_bvn.asset_id == "equity:pe:bvl:bvn"
    assert us_bvn.asset_id == "equity:us:bvn"
    assert bvl_bvn != us_bvn
    assert len(service.list_assets(capability="registry.exchange_listing")) == 6


def test_typed_not_found_errors() -> None:
    service = AssetCatalogService.load_default()
    with pytest.raises(AssetNotFoundError):
        service.get("equity:us:missing")
    with pytest.raises(AssetAliasNotFoundError):
        service.resolve_alias("missing")
    with pytest.raises(ProviderBindingNotFoundError):
        service.get_binding("equity:us:aapl", provider="coinbase", namespace="product_id")
    with pytest.raises(ProviderBindingNotFoundError):
        service.resolve_external(provider="sec", namespace="cik", identifier="9999999999")


def test_ambiguous_alias_and_binding_fail_without_tie_breaking() -> None:
    first = _catalog_asset(
        asset_id="equity:us:first",
        symbol="FIRST",
        alias="SAME",
        bindings=(_binding("FIRST"),),
    )
    second = _catalog_asset(
        asset_id="equity:us:second",
        symbol="SECOND",
        alias="same",
        bindings=(_binding("SECOND"),),
    )
    service = AssetCatalogService(AssetCatalogDocument(catalog_version=1, assets=(first, second)))
    with pytest.raises(AmbiguousAssetAliasError):
        service.resolve_alias("same")

    multiple = _catalog_asset(
        asset_id="equity:us:multiple",
        symbol="MULTI",
        alias="MULTI",
        bindings=(_binding("FIRST"), _binding("SECOND")),
    )
    service = AssetCatalogService(AssetCatalogDocument(catalog_version=1, assets=(multiple,)))
    with pytest.raises(AmbiguousProviderBindingError):
        service.get_binding("equity:us:multiple", provider="alpaca", namespace="symbol")

    shared_issuer = ProviderBinding(
        provider="smv",
        namespace="legal_name",
        identifier="EMISOR S.A.A.",
        is_unique=False,
        capabilities=("registry.issuer",),
    )
    first_listing = _catalog_asset(
        asset_id="equity:pe:bvl:first",
        symbol="FIRST",
        alias="FIRST",
        bindings=(shared_issuer,),
    )
    second_listing = _catalog_asset(
        asset_id="equity:pe:bvl:second",
        symbol="SECOND",
        alias="SECOND",
        bindings=(shared_issuer,),
    )
    service = AssetCatalogService(
        AssetCatalogDocument(
            catalog_version=1,
            assets=(first_listing, second_listing),
        )
    )
    with pytest.raises(AmbiguousProviderBindingError):
        service.resolve_external(
            provider="smv",
            namespace="legal_name",
            identifier="EMISOR S.A.A.",
        )


def test_path_loading_rejects_malformed_and_unknown_versions(tmp_path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(AssetCatalogFormatError):
        AssetCatalogService(path=malformed)

    unknown = tmp_path / "unknown.json"
    unknown.write_text(
        '{"catalog_version":2,"assets":[]}',
        encoding="utf-8",
    )
    with pytest.raises(AssetCatalogFormatError):
        AssetCatalogService(path=unknown)

    document = AssetCatalogDocument(
        catalog_version=2,
        assets=(
            _catalog_asset(
                asset_id="equity:us:one",
                symbol="ONE",
                alias="ONE",
                bindings=(_binding("ONE"),),
            ),
        ),
    )
    with pytest.raises(AssetCatalogVersionError):
        AssetCatalogService(document)


def test_service_exposes_tuples_without_mutable_internal_lists() -> None:
    service = AssetCatalogService.load_default()
    assets = service.list_assets()
    assert isinstance(assets, tuple)
    assert isinstance(assets[0].aliases, tuple)
    assert isinstance(assets[0].provider_bindings, tuple)
