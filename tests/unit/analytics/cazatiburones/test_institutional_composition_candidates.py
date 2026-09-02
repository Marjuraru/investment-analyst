from investment_analyst.analytics.cazatiburones.institutional_composition_candidates import (
    candidates_by_period,
)


def test_empty_artifacts_have_no_implicit_zero_close() -> None:
    assert candidates_by_period((), manager_cik="0001067983") == {}
