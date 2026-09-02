from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from investment_analyst.analytics.cazatiburones.institutional_close_totals import (
    effective_close_total,
)
from investment_analyst.core.models.enums import DataQuality


def test_effective_close_total_applies_the_effective_filing_monetary_policy() -> None:
    item = SimpleNamespace(
        rows=(SimpleNamespace(value_as_reported=Decimal("2")),),
        cover_revision=SimpleNamespace(
            document=SimpleNamespace(
                filing=SimpleNamespace(accepted_at=datetime(2022, 1, 1, tzinfo=UTC))
            )
        ),
    )
    assert effective_close_total(item) == (Decimal("2000"), DataQuality.PARTIAL)


def test_pre_and_post_policy_equivalent_weights_keep_the_same_ratio() -> None:
    pre = SimpleNamespace(
        rows=(SimpleNamespace(value_as_reported=Decimal("2")),),
        cover_revision=SimpleNamespace(
            document=SimpleNamespace(
                filing=SimpleNamespace(accepted_at=datetime(2022, 1, 1, tzinfo=UTC))
            )
        ),
    )
    post = SimpleNamespace(
        rows=(SimpleNamespace(value_as_reported=Decimal("2")),),
        cover_revision=SimpleNamespace(
            document=SimpleNamespace(
                filing=SimpleNamespace(accepted_at=datetime(2024, 1, 1, tzinfo=UTC))
            )
        ),
    )
    assert Decimal("1000") / effective_close_total(pre)[0] == (
        Decimal("1") / effective_close_total(post)[0]
    )
