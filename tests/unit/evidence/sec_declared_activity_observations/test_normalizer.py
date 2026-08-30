import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.evidence import sec_declared_activity_observations as package
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_declared_activity_observations import (
    definitions,
    models,
    normalizer,
    service,
)
from investment_analyst.evidence.sec_declared_activity_observations.normalizer import (
    DeclaredActivityNormalizationError,
    normalize_beneficial_ownership_statement,
    normalize_ownership_statement,
)
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_ownership.models import (
    OwnershipEntry,
    OwnershipStatement,
    ReportingOwner,
)

_ASSET_ID = "equity:us:aapl"
_ISSUER_CIK = "0000320193"
_OWNER_CIK = "0001214156"


def _filing(accession: str, accepted_at: datetime, form: str) -> SecFiling:
    return SecFiling(
        filing_id=SecFiling.expected_id(_ISSUER_CIK, accession),
        filer_cik=_ISSUER_CIK,
        accession=accession,
        form=form,
        filing_date=accepted_at.date(),
        report_date=accepted_at.date() - timedelta(days=1),
        accepted_at=accepted_at,
        is_amendment=False,
    )


def _revision(filing: SecFiling, name: str) -> SecDocumentRevision:
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, name),
        filing=filing,
        name=name,
    )
    checksum = uuid4().hex + uuid4().hex[:32]
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, checksum, REVISION_SCHEMA_VERSION_V2
    )
    return SecDocumentRevision(
        revision_id=revision_id,
        asset_id=_ASSET_ID,
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=uuid4(),
        content_sha256=checksum,
        content_size_bytes=1,
        available_at=filing.accepted_at,
        retrieved_at=filing.accepted_at + timedelta(hours=1),
        source_url=f"https://www.sec.gov/Archives/{name}",
        revision_schema_version=REVISION_SCHEMA_VERSION_V2,
    )


def _ownership_statement(
    *,
    accession: str = "0000320193-25-000001",
    accepted_at: datetime,
    shares: Decimal | None = Decimal("1000"),
    price_per_share: Decimal | None = Decimal("225.50"),
    shares_owned_following: Decimal | None = Decimal("500000"),
    transaction_date: object = "unset",
) -> OwnershipStatement:
    filing = _filing(accession, accepted_at, "4")
    revision = _revision(filing, "form4.xml")
    statement_id = OwnershipStatement.expected_id(
        revision.revision_id, "sec-ownership-statement-v2"
    )
    owner = ReportingOwner(
        reporting_owner_id=ReportingOwner.expected_id(_OWNER_CIK),
        cik=_OWNER_CIK,
        name="COOK TIMOTHY D",
        is_officer=True,
        officer_title="Chief Executive Officer",
    )
    entry_id = OwnershipEntry.expected_id(statement_id, "non_derivative", "transaction", 0)
    resolved_transaction_date = (
        accepted_at.date() if transaction_date == "unset" else transaction_date
    )
    entry = OwnershipEntry(
        entry_id=entry_id,
        table="non_derivative",
        kind="transaction",
        ordinal=0,
        owner_cik=_OWNER_CIK,
        security_title="Common Stock",
        transaction_date=resolved_transaction_date,
        transaction_code="S",
        acquired_disposed="D",
        shares=shares,
        price_per_share=price_per_share,
        shares_owned_following=shares_owned_following,
        ownership_nature="D",
    )
    return OwnershipStatement(
        statement_id=statement_id,
        raw_record_id=OwnershipStatement.expected_raw_record_id(statement_id),
        asset_id=_ASSET_ID,
        document_revision=revision,
        form="4",
        period_of_report=filing.report_date,
        issuer_cik=_ISSUER_CIK,
        issuer_name="Apple Inc.",
        reporting_owners=(owner,),
        entries=(entry,),
        available_at=accepted_at,
        parsed_at=accepted_at + timedelta(hours=1),
        schema_version="sec-ownership-statement-v2",
    )


def _beneficial_statement(
    *,
    accession: str = "0000320193-25-000002",
    accepted_at: datetime,
    event_date: object = "unset",
    shares_beneficially_owned: Decimal | None = Decimal("1300000000"),
    percent_of_class: Decimal | None = Decimal("8.1"),
) -> BeneficialOwnershipStatement:
    filing = _filing(accession, accepted_at, "SC 13G")
    revision = _revision(filing, "sc13g.xml")
    statement_id = BeneficialOwnershipStatement.expected_id(revision.revision_id)
    resolved_event_date = accepted_at.date() if event_date == "unset" else event_date
    return BeneficialOwnershipStatement(
        statement_id=statement_id,
        raw_record_id=BeneficialOwnershipStatement.expected_raw_record_id(statement_id),
        asset_id=_ASSET_ID,
        document_revision=revision,
        form="SC 13G",
        subject_cik=_ISSUER_CIK,
        subject_name="Apple Inc.",
        reporting_person_cik="0000102909",
        reporting_person_name="Vanguard Group Inc",
        event_date=resolved_event_date,
        shares_beneficially_owned=shares_beneficially_owned,
        percent_of_class=percent_of_class,
        available_at=accepted_at,
        parsed_at=accepted_at + timedelta(hours=1),
    )


_NOW = datetime(2026, 1, 15, tzinfo=UTC)


def test_ownership_statement_produces_observations_with_full_traceability() -> None:
    statement = _ownership_statement(accepted_at=_NOW)
    result = normalize_ownership_statement(statement, normalized_at=_NOW + timedelta(hours=2))
    assert len(result.observations) == 3
    assert result.skipped == ()
    by_field = {obs.field_name: obs for obs in result.observations}
    shares_obs = by_field["insider_transaction_shares"]
    assert shares_obs.raw_record_id == statement.raw_record_id
    assert shares_obs.asset_id == statement.asset_id
    assert shares_obs.available_at == statement.available_at
    assert shares_obs.value == Decimal("1000")
    assert isinstance(shares_obs.value, Decimal)
    assert shares_obs.unit == "shares"
    assert shares_obs.transformation_version == definitions.TRANSFORMATION_VERSION
    assert shares_obs.observed_at == datetime.combine(
        statement.entries[0].transaction_date, datetime.min.time(), tzinfo=UTC
    )
    record_key = json.loads(shares_obs.source.record_key)
    assert record_key["statement_id"] == str(statement.statement_id)
    assert record_key["entry_id"] == str(statement.entries[0].entry_id)
    assert record_key["field_name"] == "insider_transaction_shares"
    assert record_key["date_attribute"] == "entry.transaction_date"


def test_beneficial_statement_produces_observations_with_source_id() -> None:
    statement = _beneficial_statement(accepted_at=_NOW)
    result = normalize_beneficial_ownership_statement(
        statement, normalized_at=_NOW + timedelta(hours=2)
    )
    assert len(result.observations) == 2
    assert result.skipped == ()
    by_field = {obs.field_name: obs for obs in result.observations}
    percent_obs = by_field["beneficial_percent_of_class"]
    assert percent_obs.source.source_id == "sec-edgar:beneficial-ownership-13d-13g"
    assert percent_obs.value == Decimal("8.1")
    record_key = json.loads(percent_obs.source.record_key)
    assert record_key["entry_id"] is None
    assert record_key["date_attribute"] == "statement.event_date"


def test_missing_declared_value_is_skipped_with_reason_and_never_becomes_zero() -> None:
    statement = _ownership_statement(accepted_at=_NOW, price_per_share=None)
    result = normalize_ownership_statement(statement, normalized_at=_NOW + timedelta(hours=1))
    field_names = {obs.field_name for obs in result.observations}
    assert "insider_transaction_price_per_share" not in field_names
    assert Decimal("0") not in {obs.value for obs in result.observations}
    skipped_fields = {(s.field_name, s.reason) for s in result.skipped}
    assert ("insider_transaction_price_per_share", "missing_value") in skipped_fields


def test_missing_declared_date_is_skipped_with_own_reason() -> None:
    statement = _beneficial_statement(accepted_at=_NOW, event_date=None)
    result = normalize_beneficial_ownership_statement(
        statement, normalized_at=_NOW + timedelta(hours=1)
    )
    assert result.observations == ()
    assert {s.reason for s in result.skipped} == {"missing_date"}
    assert {s.field_name for s in result.skipped} == {
        "beneficial_shares_owned",
        "beneficial_percent_of_class",
    }


def test_insider_entry_falls_back_to_period_of_report_when_transaction_date_absent() -> None:
    statement = _ownership_statement(accepted_at=_NOW, transaction_date=None)
    result = normalize_ownership_statement(statement, normalized_at=_NOW + timedelta(hours=1))
    assert len(result.observations) == 3
    for observation in result.observations:
        assert observation.observed_at is None
        assert observation.period_end == datetime.combine(
            statement.period_of_report, datetime.min.time(), tzinfo=UTC
        )


def test_amendment_produces_distinct_identity_from_original() -> None:
    original = _ownership_statement(accepted_at=_NOW, accession="0000320193-25-000001")
    amendment = _ownership_statement(
        accepted_at=_NOW + timedelta(days=1), accession="0000320193-25-000003"
    )
    original_ids = {
        obs.observation_id
        for obs in normalize_ownership_statement(original, normalized_at=_NOW).observations
    }
    amendment_ids = {
        obs.observation_id
        for obs in normalize_ownership_statement(
            amendment, normalized_at=_NOW + timedelta(days=1)
        ).observations
    }
    assert original_ids.isdisjoint(amendment_ids)


def test_rerun_with_different_clock_reuses_identical_identity() -> None:
    statement = _ownership_statement(accepted_at=_NOW)
    first = normalize_ownership_statement(statement, normalized_at=_NOW + timedelta(hours=1))
    second = normalize_ownership_statement(statement, normalized_at=_NOW + timedelta(days=3))
    first_ids = {obs.observation_id for obs in first.observations}
    second_ids = {obs.observation_id for obs in second.observations}
    assert first_ids == second_ids


def test_normalized_at_before_available_at_is_rejected_explicitly() -> None:
    statement = _ownership_statement(accepted_at=_NOW)
    with pytest.raises(DeclaredActivityNormalizationError, match="available_at"):
        normalize_ownership_statement(statement, normalized_at=_NOW - timedelta(days=1))


def test_non_decimal_declared_value_is_rejected_explicitly() -> None:
    statement = _ownership_statement(accepted_at=_NOW)
    tampered_entry = statement.entries[0].model_copy(update={"shares": 1000.0})
    tampered = statement.model_copy(update={"entries": (tampered_entry,)})
    with pytest.raises(DeclaredActivityNormalizationError, match="must be Decimal"):
        normalize_ownership_statement(tampered, normalized_at=_NOW + timedelta(hours=1))


def test_non_finite_declared_value_is_rejected_explicitly() -> None:
    statement = _ownership_statement(accepted_at=_NOW)
    tampered_entry = statement.entries[0].model_copy(update={"shares": Decimal("NaN")})
    tampered = statement.model_copy(update={"entries": (tampered_entry,)})
    with pytest.raises(DeclaredActivityNormalizationError, match="finite"):
        normalize_ownership_statement(tampered, normalized_at=_NOW + timedelta(hours=1))


def test_module_never_imports_forbidden_domains() -> None:
    forbidden = (
        "investment_analyst.alerts",
        "investment_analyst.analytics.cazatiburones",
        "investment_analyst.analytics.market",
        "investment_analyst.analytics.fundamentals",
        "investment_analyst.analytics.valuation",
        "investment_analyst.evidence.sec_institutional_holdings",
        "investment_analyst.evidence.instrument_correspondence",
    )
    for module in (definitions, models, normalizer, service, package):
        source = inspect.getsource(module)
        for banned in forbidden:
            assert banned not in source, f"{module.__name__} must not reference {banned}"


def test_run_summary_contract_excludes_scoring_and_event_vocabulary() -> None:
    forbidden_terms = {
        "score",
        "verdict",
        "confidence",
        "ranking",
        "event",
        "candidate",
        "dedup",
        "cooldown",
    }
    field_names = set(models.DeclaredActivityObservationRunSummary.model_fields)
    assert not (field_names & forbidden_terms)
