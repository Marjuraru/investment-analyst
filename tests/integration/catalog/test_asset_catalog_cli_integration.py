"""Subprocess coverage for the catalog listing and resolution commands."""

import json
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parents[3]


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    source = str(_PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = source if not existing else f"{source}{os.pathsep}{existing}"
    environment["CATALOG_TEST_SECRET"] = "must-not-be-printed"
    return environment


def _run(script: str, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_PROJECT_ROOT / "scripts" / script), *arguments],
        cwd=cwd,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_list_assets_filters_and_is_cwd_independent(tmp_path) -> None:
    all_assets = _run("list_assets.py", cwd=tmp_path)
    equities = _run("list_assets.py", "--asset-type", "equity", cwd=tmp_path)
    market = _run(
        "list_assets.py",
        "--capability",
        "market.daily_bars",
        cwd=tmp_path,
    )

    assert all_assets.returncode == 0, all_assets.stderr
    assert equities.returncode == 0, equities.stderr
    assert market.returncode == 0, market.stderr
    all_payload = json.loads(all_assets.stdout)
    assert all_payload["catalog_version"] == 1
    assert [item["asset_id"] for item in all_payload["assets"]] == [
        "crypto:btc-usd",
        "equity:us:aapl",
    ]
    assert json.loads(equities.stdout)["count"] == 1
    assert json.loads(market.stdout)["count"] == 2


def test_resolve_by_id_alias_and_binding(tmp_path) -> None:
    by_id = _run("resolve_asset.py", "--asset-id", "crypto:btc-usd", cwd=tmp_path)
    by_alias = _run("resolve_asset.py", "--alias", "aapl", cwd=tmp_path)
    binding = _run(
        "resolve_asset.py",
        "--alias",
        "AAPL",
        "--provider",
        "sec",
        "--namespace",
        "cik",
        cwd=tmp_path,
    )

    assert by_id.returncode == 0, by_id.stderr
    assert by_alias.returncode == 0, by_alias.stderr
    assert binding.returncode == 0, binding.stderr
    assert json.loads(by_id.stdout)["asset"]["asset_id"] == "crypto:btc-usd"
    assert json.loads(by_alias.stdout)["asset"]["asset_id"] == "equity:us:aapl"
    assert json.loads(binding.stdout)["binding"]["identifier"] == "0000320193"


def test_cli_errors_are_typed_compact_and_secret_free(tmp_path) -> None:
    missing_asset = _run(
        "resolve_asset.py",
        "--asset-id",
        "equity:us:missing",
        cwd=tmp_path,
    )
    missing_alias = _run("resolve_asset.py", "--alias", "missing", cwd=tmp_path)
    provider_only = _run(
        "resolve_asset.py",
        "--alias",
        "AAPL",
        "--provider",
        "sec",
        cwd=tmp_path,
    )
    namespace_only = _run(
        "resolve_asset.py",
        "--alias",
        "AAPL",
        "--namespace",
        "cik",
        cwd=tmp_path,
    )

    for result in (missing_asset, missing_alias, provider_only, namespace_only):
        assert result.returncode != 0
        assert "Traceback" not in result.stderr
        assert "must-not-be-printed" not in result.stdout + result.stderr
        assert "order" not in result.stdout.casefold()
        assert "trading" not in result.stdout.casefold()
