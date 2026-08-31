"""Guard the smoke command's read-only coverage contract."""

from pathlib import Path


def test_smoke_separates_real_refresh_from_the_offline_coverage_query() -> None:
    source = (Path(__file__).parents[2] / "scripts/smoke_universe_coverage.py").read_text()
    assert "UniverseCoverageApplication" in source
    assert "InvestmentAnalystApplication" in source
    assert "refresh_listed_market" in source
    assert "refresh_sec_fundamentals" in source
    assert "refresh_crypto_spot_daily" in source
    assert "load_dotenv" not in source
    assert "--env-file" not in source
    assert "os.environ.pop" in source
