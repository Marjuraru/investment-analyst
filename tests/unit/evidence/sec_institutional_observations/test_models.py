from datetime import UTC, datetime

import pytest

from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationSummary,
)


def _summary(**changes) -> InstitutionalObservationSummary:
    values = {
        "asset_id": "equity:us:aapl",
        "known_at": datetime(2026, 1, 1, tzinfo=UTC),
        "normalized_at": datetime(2026, 1, 2, tzinfo=UTC),
        "reports_examined": 2,
        "reports_missing": 1,
        "reports_not_enriched": 0,
        "rows_examined": 3,
        "rows_linked": 2,
        "rows_unlinked": 1,
        "values_examined": 2,
        "observations_generated": 3,
        "observations_created": 2,
        "observations_reused": 1,
        "skipped_by_reason": {"missing_report": 1, "class_mismatch": 1},
    }
    values.update(changes)
    return InstitutionalObservationSummary(**values)


def test_summary_enforces_complete_contractual_accounting() -> None:
    assert _summary().rows_linked == 2

    with pytest.raises(ValueError, match="report counts"):
        _summary(reports_missing=2, reports_not_enriched=1)
    with pytest.raises(ValueError, match="row counts"):
        _summary(rows_unlinked=0)
    with pytest.raises(ValueError, match="values examined"):
        _summary(values_examined=1)
    with pytest.raises(ValueError, match="observation counts"):
        _summary(observations_reused=2)
