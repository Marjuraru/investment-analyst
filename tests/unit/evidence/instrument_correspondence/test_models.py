from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from investment_analyst.evidence.instrument_correspondence.models import (
    INSTRUMENT_CORRESPONDENCE_SCHEMA_VERSION,
    InstrumentCorrespondence,
)


def _declared(*, effective_to: date | None = None, available_at: datetime | None = None):
    available = available_at or datetime(2025, 2, 14, 18, tzinfo=UTC)
    return InstrumentCorrespondence.declare(
        asset_id="equity:us:aapl",
        cusip="037833100",
        title_of_class="COM",
        effective_from=date(2020, 1, 1),
        effective_to=effective_to,
        available_at=available,
        recorded_at=datetime(2025, 2, 15, tzinfo=UTC),
    )


def test_declaration_identity_uses_only_declared_identity_axes() -> None:
    declared = _declared()

    assert declared.correspondence_id == InstrumentCorrespondence.expected_id(
        "037833100",
        "COM",
        "equity:us:aapl",
        date(2020, 1, 1),
        None,
        datetime(2025, 2, 14, 18, tzinfo=UTC),
    )
    assert declared.schema_version == INSTRUMENT_CORRESPONDENCE_SCHEMA_VERSION
    assert declared.correspondence_id != _declared(effective_to=date(2025, 1, 1)).correspondence_id
    assert (
        declared.correspondence_id
        != _declared(available_at=datetime(2025, 2, 14, 19, tzinfo=UTC)).correspondence_id
    )


def test_declaration_rejects_invalid_period_and_unavailable_recording() -> None:
    with pytest.raises(ValidationError, match="effective_to"):
        _declared(effective_to=date(2020, 1, 1))

    with pytest.raises(ValidationError, match="available_at"):
        InstrumentCorrespondence.declare(
            asset_id="equity:us:aapl",
            cusip="037833100",
            title_of_class="COM",
            effective_from=date(2020, 1, 1),
            effective_to=None,
            available_at=datetime(2025, 2, 16, tzinfo=UTC),
            recorded_at=datetime(2025, 2, 15, tzinfo=UTC),
        )
