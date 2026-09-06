"""Unit tests for institutional composition v2 models."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.institutional_composition_v2_models import (
    InstitutionalCompositionV2Candidate,
    InstitutionalCompositionV2Result,
)


def test_candidate_v2_model_is_strict_and_forbids_extra() -> None:
    now = datetime(2025, 2, 14, tzinfo=UTC)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        InstitutionalCompositionV2Candidate(
            artifact_id=uuid4(),
            accession="0000950123-25-000001",
            manager_cik="0001067983",
            report_period=date(2024, 12, 31),
            available_at=now,
            is_amendment=False,
            extra_field="invalid",  # type: ignore[call-arg]
        )


def test_result_v2_requires_source_literal_and_operation_consistency() -> None:
    now = datetime(2025, 2, 14, tzinfo=UTC)
    # Source literal provided without operation -> error
    with pytest.raises(ValidationError, match="source literal requires an operation"):
        InstitutionalCompositionV2Result(
            manager_cik="0001067983",
            report_period=date(2024, 12, 31),
            known_at=now,
            status="amended",
            reason="declared_amendment_restatement",
            effective_artifact_id=uuid4(),
            effective_accession="0000950123-25-000002",
            source_literal="RESTATEMENT",
            operation=None,
            declared_entry_total=1,
            observed_entry_total=1,
            declared_value_total=Decimal("10.00"),
            observed_value_total=Decimal("10.00"),
            entry_total_matches=True,
            value_total_matches=True,
        )

    # Operation provided without source literal -> error
    with pytest.raises(ValidationError, match="operation requires a source literal"):
        InstitutionalCompositionV2Result(
            manager_cik="0001067983",
            report_period=date(2024, 12, 31),
            known_at=now,
            status="amended",
            reason="declared_amendment_restatement",
            effective_artifact_id=uuid4(),
            effective_accession="0000950123-25-000002",
            source_literal=None,
            operation="replacement",
            declared_entry_total=1,
            observed_entry_total=1,
            declared_value_total=Decimal("10.00"),
            observed_value_total=Decimal("10.00"),
            entry_total_matches=True,
            value_total_matches=True,
        )
