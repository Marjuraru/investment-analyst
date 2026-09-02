"""Pure monetary-policy total for one effective as-filed close."""

from decimal import Decimal

from investment_analyst.core.models.enums import DataQuality
from investment_analyst.evidence.sec_institutional_observations.definitions import monetary_value
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
)


def effective_close_total(item: InstitutionalHoldingsSemantics) -> tuple[Decimal, DataQuality]:
    total = sum((row.value_as_reported for row in item.rows), Decimal("0"))
    value, quality = monetary_value(
        total, accepted_at=item.cover_revision.document.filing.accepted_at
    )
    return value, DataQuality(quality)
