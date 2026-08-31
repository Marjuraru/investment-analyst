"""Guard the smoke command's read-only coverage contract."""

from pathlib import Path


def test_smoke_uses_the_coverage_application_without_environment_loading() -> None:
    source = (Path(__file__).parents[2] / "scripts/smoke_universe_coverage.py").read_text()
    assert "UniverseCoverageApplication" in source
    assert ".env" not in source
    assert "refresh" not in source.casefold()
