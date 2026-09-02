from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.institutional_concentration_models import (
    InstitutionalConcentrationInput,
    InstitutionalConcentrationPosition,
    InstitutionalConcentrationResult,
)
from investment_analyst.core.models.enums import DataQuality


def _input(**updates: object) -> InstitutionalConcentrationInput:
    values: dict[str, object] = {
        "manager_cik": "0001067983",
        "report_period": date(2024, 12, 31),
        "known_at": datetime(2025, 2, 14, tzinfo=UTC),
        "close_status": "original_complete",
        "close_reason": "declared_original",
        "effective_artifact_id": uuid4(),
        "effective_accession": "0000950123-25-000001",
        "accepted_at": datetime(2025, 2, 14, tzinfo=UTC),
        "effective_close_total": Decimal("100"),
        "total_quality": DataQuality.VALID,
        "positions": (
            InstitutionalConcentrationPosition(
                cusip="037833100",
                title_of_class="COM",
                value_as_reported=Decimal("100"),
            ),
        ),
    }
    values.update(updates)
    return InstitutionalConcentrationInput(**values)


def test_models_reject_float_and_preserve_declared_position_identity() -> None:
    position = InstitutionalConcentrationPosition(
        cusip="037833100",
        title_of_class="COM",
        put_call=None,
        value_as_reported=Decimal("0"),
    )

    assert position.declared_position_key == ("037833100", "COM", None)
    with pytest.raises(ValidationError):
        InstitutionalConcentrationPosition(
            cusip="037833100",
            title_of_class="COM",
            value_as_reported=0.1,
        )


def test_input_rejects_unselected_rows_or_incomplete_monetary_inputs() -> None:
    with pytest.raises(ValidationError, match="unselected close"):
        _input(effective_artifact_id=None, effective_accession=None)
    with pytest.raises(ValidationError, match="monetary inputs"):
        _input(accepted_at=None)


def test_omitted_result_cannot_expose_a_magnitude_or_quality() -> None:
    with pytest.raises(ValidationError, match="omitted result"):
        InstitutionalConcentrationResult(
            manager_cik="0001067983",
            report_period=date(2024, 12, 31),
            known_at=datetime(2025, 2, 14, tzinfo=UTC),
            status="omitted",
            reason="empty_close",
            close_status="original_complete",
            close_reason="declared_original",
            position_count=0,
        )
