"""Keep the user-facing coverage entry points present in the delivery."""

from pathlib import Path


def test_coverage_documentation_and_scripts_are_declared() -> None:
    root = Path(__file__).parents[2]
    assert (root / "docs/universe_coverage.md").is_file()
    assert (root / "scripts/query_universe_coverage.py").is_file()
    assert (root / "scripts/smoke_universe_coverage.py").is_file()


def test_coverage_frontend_contract_declares_ten_year_fundamental_window() -> None:
    root = Path(__file__).parents[2]
    app_mesa = (root / "src/investment_analyst/frontend/static/app-mesa.js").read_text(
        encoding="utf-8"
    )
    assert "const MESA_COVERAGE_WINDOW_DAYS = 365;" in app_mesa
    assert "const MESA_FUNDAMENTAL_COVERAGE_WINDOW_DAYS = 3660;" in app_mesa
    assert "fundamental_start: coverageWindow.fundamentalStart" in app_mesa
