from datetime import UTC, date, datetime

from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_engine import (
    compose,
)
from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_models import (
    InstitutionalEffectiveHoldingsQuery,
)


def test_no_visible_artifact_is_insufficient_without_rows() -> None:
    query = InstitutionalEffectiveHoldingsQuery(
        manager_cik="0001067983",
        report_period=date(2024, 12, 31),
        known_at=datetime(2025, 2, 14, tzinfo=UTC),
    )

    result = compose(query=query, artifacts=())

    assert (result.status, result.reason, result.rows, result.contributors) == (
        "insufficient",
        "composition_insufficient",
        (),
        (),
    )
