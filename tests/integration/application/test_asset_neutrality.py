"""Catalog and preference neutrality across the supported asset universe."""

from __future__ import annotations

from pathlib import Path

import pytest

from investment_analyst.application.asset_preferences import (
    AssetPreferencesService,
    AssetPreferencesStore,
    cli_seed_asset_preferences,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.market_universe import MarketAssetUniverse
from investment_analyst.application.runtime import ApplicationRuntime

_AAPL = "equity:us:aapl"


def _universe() -> MarketAssetUniverse:
    return InvestmentAnalystApplication(ApplicationRuntime.create_default()).list_market_assets()


def _without(universe: MarketAssetUniverse, asset_id: str) -> MarketAssetUniverse:
    return MarketAssetUniverse(
        catalog_version=universe.catalog_version,
        assets=tuple(asset for asset in universe.assets if asset.asset_id != asset_id),
    )


def _view(tmp_path: Path, universe: MarketAssetUniverse, selected: tuple[str, ...]):
    seed = cli_seed_asset_preferences(universe, selected)
    service = AssetPreferencesService(
        AssetPreferencesStore(tmp_path / "asset_preferences.json"),
        universe,
        seed,
        scheduler=None,
        job_factory=None,
    )
    return service.view()


def test_catalog_order_does_not_privilege_aapl() -> None:
    universe = _universe()

    assert universe.assets[0].asset_id != _AAPL
    assert _AAPL in {asset.asset_id for asset in universe.assets}
    assert tuple(asset.asset_id for asset in universe.assets) == tuple(
        sorted(asset.asset_id for asset in universe.assets)
    )


def test_default_seed_treats_every_catalog_asset_equally() -> None:
    universe = _universe()

    seed = cli_seed_asset_preferences(universe, ())

    assert {entry.asset_id for entry in seed.entries} == {
        asset.asset_id for asset in universe.assets
    }
    assert all(entry.watchlist and entry.scheduled_refresh for entry in seed.entries)
    assert not any(entry.favorite for entry in seed.entries)


def test_explicit_aapl_choice_is_preserved_without_selecting_others() -> None:
    universe = _universe()

    seed = cli_seed_asset_preferences(universe, (_AAPL,))
    selected = {entry.asset_id for entry in seed.entries if entry.scheduled_refresh}

    assert selected == {_AAPL}
    assert all(not entry.favorite for entry in seed.entries)


def test_catalog_without_aapl_never_fabricates_an_aapl_fallback(tmp_path: Path) -> None:
    universe = _without(_universe(), _AAPL)

    view = _view(tmp_path, universe, ())

    assert all(asset.asset_id != _AAPL for asset in view.assets)
    assert sum(asset.scheduled_refresh for asset in view.assets) == len(universe.assets)


def test_explicit_aapl_is_rejected_when_absent_from_the_catalog(tmp_path: Path) -> None:
    universe = _without(_universe(), _AAPL)

    with pytest.raises(ValueError, match="not supported"):
        _view(tmp_path, universe, (_AAPL,))


def test_explicit_selection_of_another_asset_does_not_add_aapl(tmp_path: Path) -> None:
    universe = _universe()

    view = _view(tmp_path, universe, ("equity:us:tsm",))

    scheduled = {asset.asset_id for asset in view.assets if asset.scheduled_refresh}
    assert scheduled == {"equity:us:tsm"}
