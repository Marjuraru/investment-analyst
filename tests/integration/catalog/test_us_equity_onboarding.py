"""Acceptance coverage for the MOG.A, GOOGL and NKE catalog onboarding."""

from __future__ import annotations

import hashlib
import json
from datetime import date, time
from decimal import Decimal
from importlib import resources
from pathlib import Path

import pytest

from investment_analyst.application.asset_preferences import (
    AssetPreferencesService,
    AssetPreferencesStore,
    AssetPreferencesUpdate,
    cli_seed_asset_preferences,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.multi_asset_scheduler import ScheduledJobDomain
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.application.sec_institutional_universe import (
    resolve_catalog_sec_cusip_mappings,
)
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
    resolve_sec_cusip_binding,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import (
    AssetAliasNotFoundError,
    AssetCatalogService,
    ProviderBindingNotFoundError,
)
from investment_analyst.core.models import AssetClass, DataFrequency
from investment_analyst.frontend.local_schedule_jobs import (
    LocalWatchlistScheduleConfig,
    build_local_watchlist_jobs,
)
from investment_analyst.providers.asset_config import SecAccountingStandard

_NEW_ASSETS = {
    "equity:us:mog.a": {
        "symbol": "MOG.A",
        "name": "Moog Inc. Class A",
        "exchange": "NYSE",
        "cik": "0000067887",
        "cusip": "615394202",
        "sec_ticker": "MOG-A",
        "alpaca_source_id": "alpaca-market-data:iex:mog.a:daily-bars:adjustment-all",
        "sec_submissions_source_id": "sec-edgar:mog-a:submissions",
        "sec_companyfacts_source_id": "sec-edgar:mog-a:companyfacts",
    },
    "equity:us:googl": {
        "symbol": "GOOGL",
        "name": "Alphabet Inc. Class A",
        "exchange": "NASDAQ",
        "cik": "0001652044",
        "cusip": "02079K305",
        "sec_ticker": "GOOGL",
        "alpaca_source_id": "alpaca-market-data:iex:googl:daily-bars:adjustment-all",
        "sec_submissions_source_id": "sec-edgar:googl:submissions",
        "sec_companyfacts_source_id": "sec-edgar:googl:companyfacts",
    },
    "equity:us:nke": {
        "symbol": "NKE",
        "name": "NIKE, Inc. Class B",
        "exchange": "NYSE",
        "cik": "0000320187",
        "cusip": "654106103",
        "sec_ticker": "NKE",
        "alpaca_source_id": "alpaca-market-data:iex:nke:daily-bars:adjustment-all",
        "sec_submissions_source_id": "sec-edgar:nke:submissions",
        "sec_companyfacts_source_id": "sec-edgar:nke:companyfacts",
    },
}

_PRIOR_ASSET_SHA256 = {
    "crypto:ada-usd": "c52b872e8f56b5d800ac2e47504e4c0f6a9c5163140d7649343bc3c273c1ff60",
    "crypto:btc-usd": "509c743a3e7650cec1bc5121e080f9e1c3efd857661d39f4d8d23bde192eda33",
    "crypto:eth-usd": "b67a778ba271d0dff016cb03659afb7dd7ccd698a54e16ba094a41c5db05d913",
    "crypto:link-usd": "90cf03c11b6ba38a37a51a56c463642c9915f2bf3f2baf6f74942b123018534b",
    "crypto:sol-usd": "a709a09c89f7b208fce04756d1f52c7cd835dcfec7789d511c92de1603644086",
    "equity:pe:bvl:bvn": "e25b81e0b6802ce2eaa4cc24a2f7387b54ae92a2294a8c1024a729dc754d33af",
    "equity:pe:bvl:cverdec1": "e23fe90486123dc8ebf455f599b2b2baae78cdf41eb95979bb1ae93a937b9151",
    "equity:pe:bvl:minsuri1": "a7b0bb2a09513d69aa3952f33e30709ead96c5ab9bb771f3e85a2a1472f63b3b",
    "equity:pe:bvl:pomalcc1": "4ad0aed1649923dee1fcc17e987de971bc8009d57da41970b7831cc8979484af",
    "equity:pe:bvl:scco": "cf968286b84138539c6afa501b35e9608b41f9b7fb07af78a7bd20869ef0cfa2",
    "equity:pe:bvl:volcabc1": "8be7f14540b293dfaa0219308aeed86156acb56f7ffc0a69c4704221c47d88d6",
    "equity:us:aapl": "03fe368605e5320b322e02af21d54083fe5289ef2b6ea8de1422d715716b9d67",
    "equity:us:amd": "0e59b35db7c915849305b6fd67b968a51221e1a32405fbd3735d8909faf2ee02",
    "equity:us:amzn": "adfbe6ae57fa631db91572f6bd60d0b997c333b4d8116c285b5375495abb636c",
    "equity:us:b": "69c5426f4c00af4d167254a196e32506fa472dfa0a0fd781fc64e61bdfac4fb4",
    "equity:us:bvn": "f7953b12f78daf588386e7e808887da7bca36a8c9fb22a68b461b02389663cd5",
    "equity:us:cat": "5f2d4a86e8ef6e171a359d296a24215b605d52a270b4a005c2f5331676402aab",
    "equity:us:cde": "d0f313c1156bb96a72f57b7c21827cd3d2a33ee09fba65f99ee4866ccf6e1f94",
    "equity:us:cvx": "ee843c6ba9feeb67d5987685c6854969d7d7f27af589996a25fed816d4f56615",
    "equity:us:hymc": "75b7c86ecb83bb1d50e4af44ff7582340ba556f18dc7d869d38ad01b78d046b4",
    "equity:us:intc": "90fe524826eddca2ba3937cafd467ea4f5dfeb48bc6621a6985971da76bddf71",
    "equity:us:jnj": "e67d0c0c8817bda03779ea1772000ebb523b7503a6b7e85c94fdb399614f18b5",
    "equity:us:msft": "388df9c76d9c83228201f19d5ae4ef511b22273348da7accb0a2e6e0f2d67e74",
    "equity:us:mstr": "880252715c54a5760e7023a6023f426b890d307aeeec8630992de669c681f3d1",
    "equity:us:mu": "f8dd6c6a39708bff472888ba5e113056eef675997a613a570a7860b7b7ed0eb8",
    "equity:us:mux": "1333627a66bdd45d83578e2a8401dee41f995833ab0745371e180a01694e748b",
    "equity:us:nem": "8c3814075fd9bb24d4119a20d1324a50fb5248beb9fed72762f9de940df7167a",
    "equity:us:nvda": "0f498da4958c7515967bec89fe84ad6338dc6e0644209d50544975a6b78a1ced",
    "equity:us:pltr": "eb0de4ccddc0249c887af57db8e7bea19cb178388be9c492b7f505399aafdbc9",
    "equity:us:scco": "781a4cbe33e79a2a8932c6f7f80e63d2758dce9c86bd8fc51e6054182a0aed01",
    "equity:us:tsm": "4f2e0f0a2ec95609b35d98be5732757ab143e4caba1907bac290846bc6db8aaf",
    "etf:us:gbtc": "4be94ef354eaeb5f12c07c2f898d87d5d79b66cdd9e84ac0e3d10fcb9868f777",
    "etf:us:gld": "7f8115a16aac7e8460ef176d73beb293ca900d78921e4cb976759e45ac201659",
    "etf:us:ibit": "bbee0e840e2c794975a2fcef267a1f72c28933b1a0ff2362b4dcf82eb9b8bd3c",
    "etf:us:qqq": "746719e7d45f0f503ff676ae3dd0b2f7b96767bf74b49e0311a1f4b6bbb466ca",
    "etf:us:spy": "d8bded754b16c74c44492f23fc21a39a4c75dad13dca4274f8c576a8d8fadf19",
    "etf:us:tlt": "85912ec0bfca631a9d4609e2efff60e7007dda3fc4d8073c3daf05495de9be1e",
}

_JOB_SUFFIXES = (
    "market-daily",
    "fundamentals-quarterly",
    "primary-documents",
    "declared-activity",
)


class _UnusedController:
    """Job construction must never need a provider callback."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(name)


def _catalog() -> AssetCatalogService:
    return AssetCatalogService.load_default()


def _universe():
    return InvestmentAnalystApplication.create_default().list_market_assets()


def _config(*asset_ids: str, institutional: bool = False) -> LocalWatchlistScheduleConfig:
    return LocalWatchlistScheduleConfig(
        timezone="America/Lima",
        run_at=time(hour=7),
        market_start=date(2025, 1, 1),
        selected_asset_ids=tuple(sorted(asset_ids)),
        sec_cusip_asset_ids=tuple(sorted(asset_ids)) if institutional else (),
    )


def _canonical_digest(entry: dict[str, object]) -> str:
    encoded = json.dumps(entry, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("asset_id", tuple(_NEW_ASSETS))
def test_new_identity_resolves_catalog_providers_sources_basis_and_cusip(asset_id: str) -> None:
    expected = _NEW_ASSETS[asset_id]
    catalog = _catalog()
    resolver = ProviderAssetContextResolver(catalog)
    asset = catalog.get(asset_id)

    assert asset.asset_class is AssetClass.EQUITY
    assert asset.is_active is True
    assert asset.symbol == expected["symbol"]
    assert asset.name == expected["name"]
    assert asset.exchange == expected["exchange"]
    assert asset.quote_currency == "USD"
    assert asset.security_unit_basis == "reported_common_share"
    assert asset.security_unit_basis_version == "security-unit-basis-v1"
    assert asset.security_unit_factor == Decimal("1")
    assert asset.security_unit_market_adjustment == "all"
    assert asset.security_unit_basis_revisions == ()

    alpaca_context = resolver.resolve(
        asset_id,
        provider="alpaca",
        required_namespaces=("symbol",),
        required_capabilities=("market.daily_bars",),
    )
    assert alpaca_context.require_identifier("symbol") == expected["symbol"]
    sec_context = resolver.resolve(
        asset_id,
        provider="sec",
        required_namespaces=("cik", "cusip", "taxonomy", "ticker"),
        required_capabilities=(
            "fundamentals.company_facts",
            "fundamentals.submissions",
        ),
    )
    assert sec_context.require_identifier("cik") == expected["cik"]
    assert sec_context.require_identifier("cusip") == expected["cusip"]
    assert sec_context.require_identifier("taxonomy") == "us-gaap"
    assert sec_context.require_identifier("ticker") == expected["sec_ticker"]

    alpaca = resolve_alpaca_configuration(resolver, asset_id=asset_id)
    assert alpaca.symbol == expected["symbol"]
    assert alpaca.feed == "iex"
    assert alpaca.adjustment == "all"
    assert alpaca.source_id == expected["alpaca_source_id"]
    sec = resolve_sec_configuration(resolver, asset_id=asset_id)
    assert sec.cik == expected["cik"]
    assert sec.ticker == expected["sec_ticker"]
    assert sec.submissions_source_id == expected["sec_submissions_source_id"]
    assert sec.companyfacts_source_id == expected["sec_companyfacts_source_id"]
    assert sec.accounting_standard is SecAccountingStandard.US_GAAP
    assert resolve_sec_cusip_binding(resolver, asset_id=asset_id) == expected["cusip"]

    descriptor = next(item for item in _universe().assets if item.asset_id == asset_id)
    assert descriptor.provider == "alpaca"
    assert descriptor.provider_identifier == expected["symbol"]
    assert descriptor.source_id == expected["alpaca_source_id"]
    assert descriptor.has_fundamentals is True
    assert descriptor.has_corporate_valuation is True
    assert descriptor.fundamental_frequencies == (
        DataFrequency.ANNUAL,
        DataFrequency.QUARTERLY,
    )
    assert descriptor.fundamental_source_ids == tuple(
        sorted(
            (
                expected["sec_submissions_source_id"],
                expected["sec_companyfacts_source_id"],
            )
        )
    )
    assert descriptor.default_market_start == date(2016, 1, 1)

    mappings = resolve_catalog_sec_cusip_mappings(ApplicationRuntime.create_default().catalog)
    assert mappings[expected["cusip"]] == asset_id


def test_new_identities_do_not_collapse_share_classes_or_cross_assets() -> None:
    catalog = _catalog()
    for asset_id, expected in _NEW_ASSETS.items():
        assert catalog.resolve_alias(expected["symbol"]).asset_id == asset_id
        assert (
            catalog.resolve_external(
                provider="sec",
                namespace="ticker",
                identifier=expected["sec_ticker"],
            ).asset_id
            == asset_id
        )
        assert (
            catalog.resolve_external(
                provider="sec",
                namespace="cusip",
                identifier=expected["cusip"],
            ).asset_id
            == asset_id
        )

    assert catalog.resolve_alias("Alphabet").asset_id == "equity:us:googl"
    assert catalog.resolve_alias("Moog Inc. Class A").asset_id == "equity:us:mog.a"
    assert catalog.resolve_alias("NIKE, Inc. Class B").asset_id == "equity:us:nke"

    for absent in ("GOOG", "MOG", "MOG.B", "NKE.B", "GOOGL.B"):
        with pytest.raises(AssetAliasNotFoundError):
            catalog.resolve_alias(absent)
    for identifier in ("GOOG", "MOG", "MOG.B", "02079K107"):
        with pytest.raises(ProviderBindingNotFoundError):
            catalog.resolve_external(provider="sec", namespace="ticker", identifier=identifier)

    sources: dict[str, str] = {}
    cusips: dict[str, str] = {}
    for asset_id, expected in _NEW_ASSETS.items():
        for source_id in (
            expected["alpaca_source_id"],
            expected["sec_submissions_source_id"],
            expected["sec_companyfacts_source_id"],
        ):
            assert source_id not in sources
            sources[source_id] = asset_id
        assert expected["cusip"] not in cusips
        cusips[expected["cusip"]] = asset_id
    assert len(sources) == 9
    assert len(cusips) == 3


def test_prior_catalog_entries_are_preserved_exactly() -> None:
    resource = resources.files("investment_analyst.catalog").joinpath("default_assets.v1.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    entries = {entry["asset_id"]: entry for entry in payload["assets"]}

    assert len(entries) == 40
    assert set(entries) - set(_PRIOR_ASSET_SHA256) == set(_NEW_ASSETS)
    for asset_id, digest in _PRIOR_ASSET_SHA256.items():
        assert _canonical_digest(entries[asset_id]) == digest, asset_id
    assert _catalog().catalog_version == 1


def test_new_equities_create_four_jobs_each_without_global_duplicates() -> None:
    jobs = build_local_watchlist_jobs(
        _UnusedController(),
        _universe(),
        _config(*_NEW_ASSETS, institutional=True),
    )
    by_id = {}
    for job in jobs:
        assert job.definition.job_id not in by_id
        by_id[job.definition.job_id] = job

    for asset_id in _NEW_ASSETS:
        scoped = tuple(job for job in jobs if job.definition.asset_id == asset_id)
        assert {job.definition.job_id for job in scoped} == {
            f"alpaca:{asset_id}:{_JOB_SUFFIXES[0]}",
            f"sec:{asset_id}:{_JOB_SUFFIXES[1]}",
            f"sec:{asset_id}:{_JOB_SUFFIXES[2]}",
            f"sec:{asset_id}:{_JOB_SUFFIXES[3]}",
        }
        assert len(scoped) == 4
        domains = [job.definition.domain for job in scoped]
        assert domains.count(ScheduledJobDomain.MARKET_DAILY) == 1
        assert domains.count(ScheduledJobDomain.FUNDAMENTALS) == 1
        assert domains.count(ScheduledJobDomain.EVENTS) == 2
        fundamental = next(
            job for job in scoped if job.definition.domain is ScheduledJobDomain.FUNDAMENTALS
        )
        assert fundamental.definition.data_frequency == "quarterly"

    institutional = [job_id for job_id in by_id if job_id.startswith("sec:institutional:")]
    assert institutional == [
        "sec:institutional:13f-cycle",
        "sec:institutional:13f-history",
    ]


def test_adoption_merges_new_assets_without_overwriting_existing_preferences(
    tmp_path: Path,
) -> None:
    universe = _universe()
    seed = cli_seed_asset_preferences(universe, ("crypto:btc-usd", "equity:us:aapl"))
    service = AssetPreferencesService(
        AssetPreferencesStore(tmp_path / "asset_preferences.json"),
        universe,
        seed,
        scheduler=None,
        job_factory=None,
    )
    current = service.effective()
    adopted = {
        entry.asset_id: entry.model_copy(
            update={"watchlist": True, "scheduled_refresh": True, "favorite": False}
        )
        for entry in current.entries
        if entry.asset_id in _NEW_ASSETS
    }
    assert set(adopted) == set(_NEW_ASSETS)
    merged = tuple(
        sorted(
            (adopted.get(entry.asset_id, entry) for entry in current.entries),
            key=lambda entry: entry.asset_id,
        )
    )

    service.update(
        AssetPreferencesUpdate(
            expected_revision_id=current.revision_id,
            expected_fingerprint=current.fingerprint,
            entries=merged,
        )
    )

    after = {entry.asset_id: entry for entry in service.effective().entries}
    for original in current.entries:
        if original.asset_id in _NEW_ASSETS:
            assert after[original.asset_id] == adopted[original.asset_id]
        else:
            assert after[original.asset_id] == original
