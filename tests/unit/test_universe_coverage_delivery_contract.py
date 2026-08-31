"""Keep the user-facing coverage entry points present in the delivery."""

from pathlib import Path


def test_coverage_documentation_and_scripts_are_declared() -> None:
    root = Path(__file__).parents[2]
    assert (root / "docs/universe_coverage.md").is_file()
    assert (root / "scripts/query_universe_coverage.py").is_file()
    assert (root / "scripts/smoke_universe_coverage.py").is_file()
