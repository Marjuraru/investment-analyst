from datetime import UTC, datetime
from typing import get_args
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationArtifactReference,
    InstitutionalObservationSummary,
    InstitutionalObservationSummaryItem,
    InstitutionalObservationSummaryQueryResult,
    InstitutionalObservationSummaryReport,
    InstitutionalObservationSummaryRow,
    InstitutionalObservationView,
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
    with pytest.raises(ValueError):
        _summary(skipped_by_reason={"missing_report": -1})
    assert InstitutionalObservationSummary.model_fields["skipped_by_reason"].default_factory() == {}


def test_observation_view_accepts_both_admissible_correspondence_proofs() -> None:
    """The read model keeps the manual declaration and the row-scoped claim as a typed union."""
    annotation = InstitutionalObservationView.model_fields["correspondence"].annotation
    assert set(get_args(annotation)) == {
        InstrumentCorrespondence,
        SecInstitutionalRowCorrespondence,
    }
    assert set(InstitutionalObservationView.model_fields) == {
        "observation",
        "report",
        "artifact",
        "row",
        "correspondence",
        "compatibility_notes",
    }
    notes = InstitutionalObservationView.model_fields["compatibility_notes"]
    assert notes.default == ()
    assert set(get_args(get_args(notes.annotation)[0])) == {
        "replayed_retrieval_time",
        "legacy_acceptance_timezone",
    }


def test_summary_contract_is_isolated_strict_and_compact() -> None:
    assert set(InstitutionalObservationSummaryQueryResult.model_fields) == {
        "schema_version",
        "observations",
        "total_matching",
        "truncated",
    }
    assert set(InstitutionalObservationSummaryItem.model_fields) == {
        "observation",
        "report",
        "row",
        "artifact_reference",
        "correspondence_id",
        "compatibility_notes",
    }
    assert InstitutionalObservationSummaryQueryResult(
        observations=(), total_matching=0, truncated=False
    ).schema_version == ("institutional-observation-summary-v1")
    with pytest.raises(ValidationError):
        InstitutionalObservationSummaryQueryResult(
            observations=(), total_matching=-1, truncated=False
        )
    with pytest.raises(ValidationError):
        InstitutionalObservationSummaryQueryResult(
            observations=(), total_matching=0, truncated=False, extra=1
        )
    assert (
        InstitutionalObservationSummaryReport(
            report_id=uuid4(),
            manager_cik="1067983",
            report_period=None,
        ).manager_cik
        == "0001067983"
    )
    assert (
        InstitutionalObservationSummaryRow(
            row_id=uuid4(), cusip="037833100", title_of_class="COM"
        ).cusip
        == "037833100"
    )


def test_summary_artifact_reference_requires_exact_sha256() -> None:
    identifier = uuid4()
    valid = InstitutionalObservationArtifactReference(
        artifact_id=identifier,
        raw_record_id=uuid4(),
        cover_revision_id=uuid4(),
        cover_content_sha256="a" * 64,
        information_table_revision_id=uuid4(),
        information_table_content_sha256="b" * 64,
    )
    assert isinstance(valid.artifact_id, UUID)
    with pytest.raises(ValidationError):
        InstitutionalObservationArtifactReference(
            artifact_id=identifier,
            raw_record_id=uuid4(),
            cover_revision_id=uuid4(),
            cover_content_sha256="A" * 64,
            information_table_revision_id=uuid4(),
            information_table_content_sha256="b" * 64,
        )


def test_summary_notes_use_the_canonical_order_and_unique_values() -> None:
    notes = InstitutionalObservationSummaryItem.model_fields["compatibility_notes"]
    assert notes.default == ()
    assert set(get_args(get_args(notes.annotation)[0])) == {
        "replayed_retrieval_time",
        "legacy_acceptance_timezone",
    }
