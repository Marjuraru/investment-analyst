"""Unit coverage for shared storage-location CLI helpers and migrated entry points."""

import argparse
from pathlib import Path

import pytest

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)

_PROJECT_ROOT = Path(__file__).parents[3]
_READ_ONLY_SCRIPTS = (
    "query_aapl_fundamental_research.py",
    "query_aapl_diagnostics.py",
    "query_market_history.py",
    "query_sec_aapl_fundamentals.py",
)
_READ_WRITE_SCRIPTS = (
    "compute_market_diagnostic.py",
    "compute_market_statistics.py",
    "compute_sec_aapl_fundamental_diagnostic.py",
    "compute_sec_aapl_fundamental_metrics.py",
    "fetch_alpaca_history.py",
    "fetch_coinbase_history.py",
    "fetch_sec_aapl_fundamentals.py",
    "normalize_sec_aapl_fundamentals.py",
    "run_aapl_complete_snapshot.py",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    add_storage_location_arguments(parser)
    return parser


def test_storage_arguments_are_optional_and_mutually_exclusive(tmp_path: Path) -> None:
    empty = storage_location_from_namespace(_parser().parse_args([]))
    workspace = storage_location_from_namespace(
        _parser().parse_args(["--workspace", str(tmp_path / "workspace")])
    )
    legacy = storage_location_from_namespace(
        _parser().parse_args(["--root", str(tmp_path / "legacy")])
    )

    assert empty.workspace is None and empty.legacy_root is None
    assert workspace.workspace == (tmp_path / "workspace").resolve()
    assert legacy.legacy_root == (tmp_path / "legacy").resolve()
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "--workspace",
                str(tmp_path / "workspace"),
                "--root",
                str(tmp_path / "legacy"),
            ]
        )


def test_migrated_scripts_use_only_central_storage_composition() -> None:
    for script_name in (*_READ_ONLY_SCRIPTS, *_READ_WRITE_SCRIPTS):
        text = (_PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert "add_storage_location_arguments(parser)" in text
        assert "StoragePaths.from_root" not in text
        assert "LocalStorage(" not in text
        assert "INVESTMENT_ANALYST_WORKSPACE" not in text
        assert "dotenv" not in text


def test_scripts_and_facade_keep_explicit_storage_access_modes() -> None:
    for script_name in _READ_ONLY_SCRIPTS:
        text = (_PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        if script_name == "query_aapl_diagnostics.py":
            assert ".query_aapl_diagnostics(" in text
            assert "WorkspaceAccessMode" not in text
        else:
            assert "WorkspaceAccessMode.READ_ONLY" in text
            assert "WorkspaceAccessMode.READ_WRITE" not in text
    for script_name in _READ_WRITE_SCRIPTS:
        text = (_PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert "WorkspaceAccessMode.READ_WRITE" in text

    facade = (_PROJECT_ROOT / "src" / "investment_analyst" / "application" / "facade.py").read_text(
        encoding="utf-8"
    )
    assert facade.count("access_mode=WorkspaceAccessMode.READ_ONLY") == 7
    assert "def query_aapl_diagnostics(" in facade
    assert "def query_aapl_market_chart(" in facade
    assert "def query_btc_market_chart(" in facade
    assert "def refresh_btc_market(" in facade
    assert "def query_aapl_fundamental_trend(" in facade
    assert "def query_aapl_fundamental_research(" in facade
    assert "def query_aapl_fundamental_research_history(" in facade
    assert "def query_aapl_fundamental_analysis(" in facade
    assert facade.count("access_mode=WorkspaceAccessMode.READ_WRITE") == 2


def test_operational_cli_delegates_without_direct_storage_or_dotenv() -> None:
    text = (_PROJECT_ROOT / "scripts" / "run_aapl_daily.py").read_text(encoding="utf-8")

    assert "AaplDailyRunner.create_default()" in text
    assert "LocalStorage" not in text
    assert "StoragePaths" not in text
    assert "dotenv" not in text


def test_local_interface_reuses_application_boundaries_and_installer_does_not_start_service() -> (
    None
):
    server = (_PROJECT_ROOT / "scripts" / "serve_investment_analyst.py").read_text(encoding="utf-8")
    installer = (_PROJECT_ROOT / "scripts" / "install_local_service.py").read_text(encoding="utf-8")

    assert "AaplLocalController(" in server
    assert "AaplDailyRunner(" in server
    assert "InvestmentAnalystApplication(" in server
    assert "LocalStorage" not in server
    assert "dotenv" not in server
    assert "subprocess" not in installer
    assert "systemctl --user enable --now" in installer
