"""Tests for explicit catalog-backed provider resolution identity."""

import ast
import inspect
import json
import pathlib
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_coinbase_configuration,
    resolve_coinbase_intraday_configuration,
    resolve_deribit_configuration,
    resolve_sec_configuration,
    resolve_sec_cusip_binding,
    resolve_smv_bvl_configuration,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CATALOG_PATH = _REPO_ROOT / "src/investment_analyst/catalog/default_assets.v1.json"
_RESOLVER_NAMES = {
    "resolve_alpaca_configuration",
    "resolve_coinbase_configuration",
    "resolve_coinbase_intraday_configuration",
    "resolve_sec_configuration",
    "resolve_sec_cusip_binding",
    "resolve_deribit_configuration",
    "resolve_smv_bvl_configuration",
}
_FIVE_EXPLICIT_RESOLVERS: tuple[Callable[..., Any], ...] = (
    resolve_alpaca_configuration,
    resolve_coinbase_configuration,
    resolve_coinbase_intraday_configuration,
    resolve_sec_configuration,
    resolve_sec_cusip_binding,
)


def _resolver_calls(*roots: str) -> list[tuple[str, str, bool]]:
    calls: list[tuple[str, str, bool]] = []
    for root in roots:
        for path in sorted((_REPO_ROOT / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                name = (
                    function.attr
                    if isinstance(function, ast.Attribute)
                    else function.id
                    if isinstance(function, ast.Name)
                    else None
                )
                if name in _RESOLVER_NAMES:
                    keywords = {keyword.arg for keyword in node.keywords}
                    calls.append((str(path.relative_to(_REPO_ROOT)), name, "asset_id" in keywords))
    return calls


def _assertion_count(path: str) -> int:
    text = (_REPO_ROOT / path).read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if line.strip().startswith("assert "))


def test_all_five_catalog_resolvers_require_an_explicit_asset_id() -> None:
    for resolver in _FIVE_EXPLICIT_RESOLVERS:
        parameter = inspect.signature(resolver).parameters["asset_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    for resolver in (resolve_deribit_configuration, resolve_smv_bvl_configuration):
        parameter = inspect.signature(resolver).parameters["asset_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


def test_a_resolver_call_without_asset_id_is_a_type_error_and_never_silently_resolves_apple() -> (
    None
):
    resolver = ApplicationRuntime.create_default().provider_resolver

    for function in _FIVE_EXPLICIT_RESOLVERS:
        with pytest.raises(TypeError):
            function(resolver)  # type: ignore[call-arg]


def test_the_eight_previously_implicit_resolver_calls_pass_their_contract_identity_explicitly_without_behavior_change() -> (  # noqa: E501
    None
):
    calls = _resolver_calls("src", "scripts")

    assert len(calls) >= 48
    assert [call for call in calls if not call[2]] == []

    scripts = {call[0] for call in calls if call[0].startswith("scripts/") and call[2]}
    assert {
        "scripts/fetch_alpaca_history.py",
        "scripts/fetch_coinbase_history.py",
        "scripts/fetch_coinbase_intraday.py",
        "scripts/fetch_sec_aapl_fundamentals.py",
        "scripts/run_aapl_complete_snapshot.py",
    } <= scripts

    facade = (_REPO_ROOT / "src/investment_analyst/application/facade.py").read_text(
        encoding="utf-8"
    )
    assert facade.count("asset_id=request.asset_id") >= 2
    assert facade.count("asset_id=APPLE_ASSET_ID") >= 2

    for script, identity in (
        ("scripts/fetch_coinbase_history.py", 'asset_id="crypto:btc-usd"'),
        ("scripts/fetch_coinbase_intraday.py", 'asset_id="crypto:btc-usd"'),
        ("scripts/fetch_sec_aapl_fundamentals.py", 'asset_id="equity:us:aapl"'),
        ("scripts/run_aapl_complete_snapshot.py", 'asset_id="equity:us:aapl"'),
    ):
        assert identity in (_REPO_ROOT / script).read_text(encoding="utf-8")


def test_minute_bars_capability_still_appears_exactly_once_and_no_asset_gains_capability() -> None:
    raw = _CATALOG_PATH.read_text(encoding="utf-8")
    assert raw.count('"market.minute_bars"') == 1

    catalog = json.loads(raw)
    capabilities: dict[str, frozenset[str]] = {}
    for asset in catalog["assets"]:
        declared: set[str] = set()
        for binding in asset.get("provider_bindings", []):
            declared.update(binding.get("capabilities", []))
        capabilities[asset["asset_id"]] = frozenset(declared)

    assert len(capabilities) == 37
    assert sum("market.daily_bars" in item for item in capabilities.values()) == 31
    assert sum("market.minute_bars" in item for item in capabilities.values()) == 1
    assert sum("fundamentals.company_facts" in item for item in capabilities.values()) == 20
    assert sum("derivatives.perpetual.snapshot" in item for item in capabilities.values()) == 2
    assert capabilities["crypto:btc-usd"] == frozenset(
        {
            "market.daily_bars",
            "market.minute_bars",
            "derivatives.funding.hourly",
            "derivatives.perpetual.snapshot",
            "derivatives.volatility_index.daily",
        }
    )
    assert "market.minute_bars" not in capabilities["crypto:eth-usd"]
    assert capabilities["crypto:sol-usd"] == frozenset({"market.daily_bars"})


_BASE_TEST_ASSERTION_COUNTS = {
    "tests/unit/providers/test_provider_asset_configuration.py": 36,
    "tests/integration/providers/test_catalog_provider_migration_compatibility.py": 20,
    "tests/integration/application/test_aapl_workspace_bootstrap_integration.py": 113,
    "tests/integration/providers/test_instrument_correspondence_resolution.py": 1,
}


def test_the_fifteen_implicit_resolver_calls_in_tests_pass_their_contract_identity_explicitly() -> (
    None
):
    calls = _resolver_calls("tests")

    assert [call for call in calls if not call[2]] == []
    per_file = Counter(call[0] for call in calls)
    for path, minimum in (
        ("tests/unit/providers/test_provider_asset_configuration.py", 21),
        ("tests/integration/providers/test_catalog_provider_migration_compatibility.py", 5),
        ("tests/integration/application/test_aapl_workspace_bootstrap_integration.py", 2),
        ("tests/integration/providers/test_instrument_correspondence_resolution.py", 1),
    ):
        assert per_file[path] >= minimum, path


def test_no_assertion_in_the_four_resolver_test_files_is_removed_weakened_or_rewritten() -> None:
    for path, base_count in _BASE_TEST_ASSERTION_COUNTS.items():
        assert _assertion_count(path) >= base_count, path
