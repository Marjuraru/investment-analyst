from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.institutional_composition_models import (
    InstitutionalCompositionCandidate,
    InstitutionalCompositionResult,
)


def test_models_keep_decimal_totals_and_typed_reasons() -> None:
    candidate = InstitutionalCompositionCandidate(
        artifact_id=uuid4(),
        accession="0000950123-25-000001",
        manager_cik="0001067983",
        report_period=date(2024, 12, 31),
        available_at=datetime(2025, 2, 14, tzinfo=UTC),
        is_amendment=False,
        declared_entry_total=1,
        declared_value_total=Decimal("0.10"),
        observed_entry_total=1,
        observed_value_total=Decimal("0.10"),
    )

    assert candidate.declared_value_total == Decimal("0.10")
    with pytest.raises(ValidationError):
        InstitutionalCompositionCandidate(
            artifact_id=uuid4(),
            accession="0000950123-25-000002",
            manager_cik="0001067983",
            report_period=date(2024, 12, 31),
            available_at=datetime(2025, 2, 14, tzinfo=UTC),
            is_amendment=False,
            observed_entry_total=1,
            observed_value_total=0.1,
        )


def test_unresolved_result_cannot_expose_an_effective_close() -> None:
    with pytest.raises(ValidationError, match="unresolved result"):
        InstitutionalCompositionResult(
            manager_cik="0001067983",
            report_period=date(2024, 12, 31),
            known_at=datetime(2025, 2, 14, tzinfo=UTC),
            status="ambiguous",
            reason="available_at_tie",
            effective_artifact_id=uuid4(),
            effective_accession="0000950123-25-000001",
        )
