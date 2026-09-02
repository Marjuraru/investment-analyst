from datetime import UTC, date, datetime

from investment_analyst.analytics.cazatiburones.institutional_metric_engine import calculate
from investment_analyst.analytics.cazatiburones.institutional_metric_models import (
    InstitutionalMetricClose,
)


def test_unresolved_close_is_never_bridged() -> None:
    closes = (
        InstitutionalMetricClose(
            report_period=date(2024, 3, 31), artifact_id=None, status="insufficient"
        ),
        InstitutionalMetricClose(
            report_period=date(2024, 6, 30), artifact_id=None, status="ambiguous"
        ),
    )
    result = calculate(
        asset_id="equity:us:aapl",
        manager_cik="1067983",
        known_at=datetime(2025, 1, 1, tzinfo=UTC),
        closes=closes,
    )
    assert {item.reason for item in result.skipped} == {"unresolved_close"}
