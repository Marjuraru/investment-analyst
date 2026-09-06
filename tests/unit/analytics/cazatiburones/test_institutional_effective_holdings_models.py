from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_models import (
    InstitutionalEffectiveHoldingsQuery,
)


def test_query_rejects_non_normalized_manager_cik() -> None:
    with pytest.raises(ValidationError, match="normalized"):
        InstitutionalEffectiveHoldingsQuery(
            manager_cik="1067983",
            report_period=date(2024, 12, 31),
            known_at=datetime(2025, 2, 14, tzinfo=UTC),
        )
