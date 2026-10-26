"""Behavioral tests for the catalog-driven initial market asset selection."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib.resources import files

import pytest

APP_CORE = (
    files("investment_analyst.frontend").joinpath("static/app-core.js").read_text(encoding="utf-8")
)
APP_SHELL = (
    files("investment_analyst.frontend").joinpath("static/app-shell.js").read_text(encoding="utf-8")
)
APP_MESA = (
    files("investment_analyst.frontend").joinpath("static/app-mesa.js").read_text(encoding="utf-8")
)


def test_deferred_board_failure_releases_the_loaded_marker_for_retry() -> None:
    loader = re.search(
        r"function loadDeferredBoardData\(boardId\) \{.*?\n\}",
        APP_SHELL,
        flags=re.DOTALL,
    )
    assert loader, "loadDeferredBoardData must exist in app-shell.js"
    body = loader.group(0)
    assert "Promise.allSettled" in body
    assert "loadedBoardIds.delete(boardId)" in body
    assert "throw error;" in APP_MESA


def test_default_known_at_cut_is_a_stable_utc_day() -> None:
    assert "function defaultKnownAtCut()" in APP_CORE
    assert "cut.setUTCHours(0, 0, 0, 0);" in APP_CORE
    assert "knownAtByAsset.get(assetId) || defaultKnownAtCut()" in APP_CORE
    assert "knownAtByAsset.get(assetId) || new Date().toISOString()" not in APP_CORE


def _resolver_source() -> str:
    match = re.search(
        r"function resolveMarketAssetSelection\(assets, selectedAssetId\) \{.*?\n\}",
        APP_CORE,
        flags=re.DOTALL,
    )
    assert match, "resolveMarketAssetSelection must exist in app-core.js"
    return match.group(0)


def _assets(*asset_ids: str) -> list[dict[str, str]]:
    return [{"asset_id": asset_id} for asset_id in asset_ids]


def test_initial_selection_is_not_a_hardcoded_ticker() -> None:
    assert "let selectedMarketAsset = null;" in APP_CORE
    assert "equity:us:aapl" not in APP_CORE


def test_catalog_load_uses_the_resolver_instead_of_a_ticker_fallback() -> None:
    assert (
        "selectedMarketAsset = resolveMarketAssetSelection(payload.assets, selectedMarketAsset);"
        in APP_CORE
    )
    assert "if (!marketAssets[selectedMarketAsset])" not in APP_CORE


def test_first_contractual_catalog_asset_wins_without_an_explicit_choice() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the real JavaScript selection smoke")
    cases = [
        {
            "name": "no-selection-first-is-not-aapl",
            "assets": _assets("crypto:btc-usd", "equity:us:aapl"),
            "selected": None,
            "expected": "crypto:btc-usd",
        },
        {
            "name": "catalog-without-aapl",
            "assets": _assets("crypto:eth-usd", "equity:us:tsm"),
            "selected": None,
            "expected": "crypto:eth-usd",
        },
        {
            "name": "contractual-order-is-not-reordered",
            "assets": _assets("equity:us:aapl", "crypto:btc-usd"),
            "selected": None,
            "expected": "equity:us:aapl",
        },
        {
            "name": "explicit-selection-is-preserved",
            "assets": _assets("crypto:btc-usd", "equity:us:aapl", "equity:us:tsm"),
            "selected": "equity:us:tsm",
            "expected": "equity:us:tsm",
        },
        {
            "name": "invalid-selection-uses-catalog-order",
            "assets": _assets("crypto:btc-usd", "equity:us:aapl"),
            "selected": "equity:us:retired",
            "expected": "crypto:btc-usd",
        },
    ]
    script = (
        f"{_resolver_source()}\n"
        f"const cases = {json.dumps(cases)};\n"
        "for (const item of cases) {\n"
        "  const actual = resolveMarketAssetSelection(item.assets, item.selected);\n"
        "  if (actual !== item.expected) {\n"
        "    console.error(JSON.stringify({case: item.name, actual, expected: item.expected}));\n"
        "    process.exit(1);\n"
        "  }\n"
        "}\n"
        "console.log(JSON.stringify({passed: cases.length}));\n"
    )
    result = subprocess.run(
        [node, "-"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": len(cases)}
