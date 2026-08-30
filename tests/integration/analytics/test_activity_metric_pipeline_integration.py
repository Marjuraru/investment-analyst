from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones.activity_metric_pipeline import (
    ActivityMetricIdentityConflictError,
    ActivityMetricPipeline,
    ActivityMetricPipelineError,
)
from investment_analyst.core.models.enums import MetricCategory
from investment_analyst.evidence.sec_beneficial_ownership.models import BeneficialOwnershipStatement
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    statement_to_raw_record as beneficial_statement_to_raw_record,
)
from investment_analyst.evidence.sec_declared_activity_observations.service import (
    DeclaredActivityObservationService,
)
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import revision_to_raw_record
from investment_analyst.evidence.sec_ownership.models import (
    OwnershipEntry,
    OwnershipStatement,
    ReportingOwner,
)
from investment_analyst.evidence.sec_ownership.repository import (
    statement_to_raw_record as ownership_statement_to_raw_record,
)
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

_ASSET_ID = "equity:us:aapl"
_ISSUER_CIK = "0000320193"
_OWNER_CIK = "0001214156"
_NOW = datetime(2026, 1, 15, tzinfo=UTC)


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
        document_id=SecLogicalDocument.expected_id(filing.filing_id, name), filing=filing, name=name
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
    *, accession: str, accepted_at: datetime, following: Decimal
) -> OwnershipStatement:
    filing = _filing(accession, accepted_at, "4")
    revision = _revision(filing, f"{accession}-form4.xml")
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
    entry = OwnershipEntry(
        entry_id=entry_id,
        table="non_derivative",
        kind="transaction",
        ordinal=0,
        owner_cik=_OWNER_CIK,
        security_title="Common Stock",
        transaction_date=accepted_at.date(),
        transaction_code="S",
        acquired_disposed="D",
        shares=Decimal("1000"),
        price_per_share=Decimal("225.50"),
        shares_owned_following=following,
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
    *, accession: str, accepted_at: datetime, percent: Decimal, shares: Decimal
) -> BeneficialOwnershipStatement:
    filing = _filing(accession, accepted_at, "SC 13G")
    revision = _revision(filing, f"{accession}-sc13g.xml")
    statement_id = BeneficialOwnershipStatement.expected_id(revision.revision_id)
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
        event_date=accepted_at.date(),
        shares_beneficially_owned=shares,
        percent_of_class=percent,
        available_at=accepted_at,
        parsed_at=accepted_at + timedelta(hours=1),
    )


def _seed_and_normalize(
    storage: LocalStorage,
    *,
    insider: tuple[OwnershipStatement, ...] = (),
    beneficial: tuple[BeneficialOwnershipStatement, ...] = (),
    known_at: datetime,
) -> None:
    for statement in insider:
        storage.raw_records.save(revision_to_raw_record(statement.document_revision))
        storage.raw_records.save(ownership_statement_to_raw_record(statement))
    for statement in beneficial:
        storage.raw_records.save(revision_to_raw_record(statement.document_revision))
        storage.raw_records.save(beneficial_statement_to_raw_record(statement))
    DeclaredActivityObservationService(storage, clock=lambda: known_at).normalize(
        asset_id=_ASSET_ID, known_at=known_at
    )


def test_pipeline_requires_writable_storage(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)):
        pass
    with (
        LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage,
        pytest.raises(StorageError, match="writable storage"),
    ):
        ActivityMetricPipeline(storage).compute(asset_id=_ASSET_ID, known_at=_NOW)


def test_pipeline_rejects_a_naive_clock(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = ActivityMetricPipeline(storage, clock=lambda: datetime(2026, 1, 15))  # noqa: DTZ001
        with pytest.raises(ActivityMetricPipelineError, match="timezone"):
            pipeline.compute(asset_id=_ASSET_ID, known_at=_NOW)


def test_pipeline_upserts_catalog_definitions(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        ActivityMetricPipeline(storage, clock=lambda: _NOW).compute(
            asset_id=_ASSET_ID, known_at=_NOW
        )
        definitions = storage.metric_definitions.list_all()
        assert len(definitions) == 3
        assert all(item.category == MetricCategory.CAZATIBURONES for item in definitions)


def test_first_run_creates_second_run_reuses_all(tmp_path: Path) -> None:
    first = _ownership_statement(
        accession="0000320193-25-000001", accepted_at=_NOW, following=Decimal("100")
    )
    second = _ownership_statement(
        accession="0000320193-25-000002",
        accepted_at=_NOW + timedelta(days=5),
        following=Decimal("125"),
    )
    bfirst = _beneficial_statement(
        accession="0000320193-25-000003",
        accepted_at=_NOW,
        percent=Decimal("4"),
        shares=Decimal("100"),
    )
    bsecond = _beneficial_statement(
        accession="0000320193-25-000004",
        accepted_at=_NOW + timedelta(days=5),
        percent=Decimal("5"),
        shares=Decimal("150"),
    )
    known_at = _NOW + timedelta(days=10)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_and_normalize(
            storage, insider=(first, second), beneficial=(bfirst, bsecond), known_at=known_at
        )

        pipeline = ActivityMetricPipeline(storage, clock=lambda: known_at + timedelta(hours=1))
        run1 = pipeline.compute(asset_id=_ASSET_ID, known_at=known_at)
        assert run1.metrics_generated == 3
        assert run1.metrics_created == 3
        assert run1.metrics_reused == 0
        assert storage.metric_results.count() == 3

        results = {r.metric_key: r for r in storage.metric_results.list(asset_id=_ASSET_ID)}
        assert results["cazatiburones.insider.holding_delta_ratio"].value == Decimal("0.25")
        assert results["cazatiburones.beneficial.delta_percent_of_class"].value == Decimal("1")
        assert results["cazatiburones.beneficial.delta_shares_beneficially_owned"].value == Decimal(
            "50"
        )

        pipeline_again = ActivityMetricPipeline(storage, clock=lambda: known_at + timedelta(days=3))
        run2 = pipeline_again.compute(asset_id=_ASSET_ID, known_at=known_at)
        assert run2.metrics_created == 0
        assert run2.metrics_reused == 3
        assert storage.metric_results.count() == 3


def test_known_at_cutoff_excludes_not_yet_available_declaration(tmp_path: Path) -> None:
    first = _ownership_statement(
        accession="0000320193-25-000001", accepted_at=_NOW, following=Decimal("100")
    )
    late = _ownership_statement(
        accession="0000320193-25-000002",
        accepted_at=_NOW + timedelta(days=10),
        following=Decimal("140"),
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        early_known_at = _NOW + timedelta(days=1)
        _seed_and_normalize(storage, insider=(first, late), known_at=_NOW + timedelta(days=20))

        early_run = ActivityMetricPipeline(storage, clock=lambda: early_known_at).compute(
            asset_id=_ASSET_ID, known_at=early_known_at
        )
        assert early_run.metrics_generated == 0
        assert early_run.skipped_by_reason == {"not_evaluable_no_precedent": 1}

        late_known_at = _NOW + timedelta(days=20)
        late_run = ActivityMetricPipeline(storage, clock=lambda: late_known_at).compute(
            asset_id=_ASSET_ID, known_at=late_known_at
        )
        assert late_run.metrics_generated == 1
        assert late_run.metrics_created == 1


def test_later_known_at_revealing_late_filing_keeps_prior_result_intact(tmp_path: Path) -> None:
    first = _ownership_statement(
        accession="0000320193-25-000001", accepted_at=_NOW, following=Decimal("100")
    )
    second = _ownership_statement(
        accession="0000320193-25-000002",
        accepted_at=_NOW + timedelta(days=5),
        following=Decimal("125"),
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        cut_a = _NOW + timedelta(days=6)
        cut_b = _NOW + timedelta(days=20)
        _seed_and_normalize(storage, insider=(first, second), known_at=cut_b)

        run_a = ActivityMetricPipeline(storage, clock=lambda: cut_a + timedelta(hours=1)).compute(
            asset_id=_ASSET_ID, known_at=cut_a
        )
        assert run_a.metrics_created == 1
        first_result_id = storage.metric_results.list(asset_id=_ASSET_ID)[0].result_id

        run_b = ActivityMetricPipeline(storage, clock=lambda: cut_b + timedelta(hours=1)).compute(
            asset_id=_ASSET_ID, known_at=cut_b
        )
        assert run_b.metrics_created == 1
        assert storage.metric_results.count() == 2
        preserved = storage.metric_results.get(first_result_id)
        assert preserved.value == Decimal("0.25")


def test_zero_prior_holding_is_not_evaluable(tmp_path: Path) -> None:
    first = _ownership_statement(
        accession="0000320193-25-000001", accepted_at=_NOW, following=Decimal("0")
    )
    second = _ownership_statement(
        accession="0000320193-25-000002",
        accepted_at=_NOW + timedelta(days=5),
        following=Decimal("5"),
    )
    known_at = _NOW + timedelta(days=10)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_and_normalize(storage, insider=(first, second), known_at=known_at)
        summary = ActivityMetricPipeline(storage, clock=lambda: known_at).compute(
            asset_id=_ASSET_ID, known_at=known_at
        )
        assert summary.metrics_generated == 0
        assert summary.skipped_by_reason == {
            "not_evaluable_no_precedent": 1,
            "not_evaluable_zero_prior": 1,
        }
        assert storage.metric_results.count() == 0


def test_missing_declared_value_skips_without_persisting(tmp_path: Path) -> None:
    first = _ownership_statement(
        accession="0000320193-25-000001", accepted_at=_NOW, following=Decimal("100")
    )
    second = _ownership_statement(
        accession="0000320193-25-000002",
        accepted_at=_NOW + timedelta(days=5),
        following=Decimal("125"),
    )
    second_without_holding = second.model_copy(
        update={"entries": (second.entries[0].model_copy(update={"shares_owned_following": None}),)}
    )
    known_at = _NOW + timedelta(days=10)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_and_normalize(storage, insider=(first, second_without_holding), known_at=known_at)
        summary = ActivityMetricPipeline(storage, clock=lambda: known_at).compute(
            asset_id=_ASSET_ID, known_at=known_at
        )
        assert summary.metrics_generated == 0
        assert summary.skipped_by_reason == {
            "not_evaluable_no_precedent": 1,
            "missing_input_observation": 1,
        }


def test_identity_conflict_raises_and_preserves_prior_result(tmp_path: Path) -> None:
    from investment_analyst.analytics.cazatiburones.activity_metric_engine import (
        calculate_insider_activity_metrics,
    )
    from investment_analyst.analytics.cazatiburones.activity_metric_identity import (
        expected_activity_metric_result_id,
    )
    from investment_analyst.core.models.metric import MetricResult
    from investment_analyst.evidence.sec_declared_activity_observations.normalizer import (
        expected_observation_id,
    )
    from investment_analyst.evidence.sec_ownership.models import OWNERSHIP_SOURCE_ID

    first = _ownership_statement(
        accession="0000320193-25-000001", accepted_at=_NOW, following=Decimal("100")
    )
    second = _ownership_statement(
        accession="0000320193-25-000002",
        accepted_at=_NOW + timedelta(days=5),
        following=Decimal("125"),
    )
    known_at = _NOW + timedelta(days=10)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_and_normalize(storage, insider=(first, second), known_at=known_at)

        # Precompute the exact candidate and identity the pipeline itself will derive, so a
        # forged MetricResult can be seeded at that identity before the pipeline ever runs.
        observation_index = {}
        for statement in (first, second):
            for entry in statement.entries:
                observation_id = expected_observation_id(
                    source_id=OWNERSHIP_SOURCE_ID,
                    statement_id=statement.statement_id,
                    entry_id=entry.entry_id,
                    field_name="insider_shares_owned_following",
                )
                observation_index[observation_id] = storage.observations.get(observation_id)
        candidate = calculate_insider_activity_metrics(
            (first, second), observations=observation_index, known_at=known_at
        ).candidates[0]
        result_id = expected_activity_metric_result_id(candidate)
        forged = MetricResult(
            result_id=result_id,
            asset_id=candidate.asset_id,
            metric_key=candidate.metric_key,
            value=Decimal("999"),
            unit=candidate.unit,
            as_of=candidate.as_of,
            available_at=candidate.available_at,
            computed_at=candidate.available_at,
            parameters=candidate.parameters,
            input_observation_ids=list(candidate.input_observation_ids),
            algorithm_version=candidate.algorithm_version,
            quality=candidate.quality,
        )
        storage.metric_results.save(forged)

        with pytest.raises(ActivityMetricIdentityConflictError):
            ActivityMetricPipeline(storage, clock=lambda: known_at + timedelta(hours=2)).compute(
                asset_id=_ASSET_ID, known_at=known_at
            )

        unchanged = storage.metric_results.get(result_id)
        assert unchanged.value == Decimal("999")
        assert storage.metric_results.count() == 1


def test_writes_only_metric_definitions_and_metric_results_tables(tmp_path: Path) -> None:
    first = _ownership_statement(
        accession="0000320193-25-000001", accepted_at=_NOW, following=Decimal("100")
    )
    second = _ownership_statement(
        accession="0000320193-25-000002",
        accepted_at=_NOW + timedelta(days=5),
        following=Decimal("125"),
    )
    known_at = _NOW + timedelta(days=10)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_and_normalize(storage, insider=(first, second), known_at=known_at)
        observations_before = storage.observations.count()
        raw_records_before = storage.raw_records.count()
        ActivityMetricPipeline(storage, clock=lambda: known_at).compute(
            asset_id=_ASSET_ID, known_at=known_at
        )
        assert storage.diagnostics.count() == 0
        assert storage.observations.count() == observations_before
        assert storage.raw_records.count() == raw_records_before


def test_result_is_reconstructable_to_its_raw_record(tmp_path: Path) -> None:
    first = _ownership_statement(
        accession="0000320193-25-000001", accepted_at=_NOW, following=Decimal("100")
    )
    second = _ownership_statement(
        accession="0000320193-25-000002",
        accepted_at=_NOW + timedelta(days=5),
        following=Decimal("125"),
    )
    known_at = _NOW + timedelta(days=10)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_and_normalize(storage, insider=(first, second), known_at=known_at)
        ActivityMetricPipeline(storage, clock=lambda: known_at).compute(
            asset_id=_ASSET_ID, known_at=known_at
        )
        result = storage.metric_results.list(asset_id=_ASSET_ID)[0]
        for observation_id in result.input_observation_ids:
            observation = storage.observations.get(observation_id)
            raw = storage.raw_records.get(observation.raw_record_id)
            assert raw.record_id in {first.raw_record_id, second.raw_record_id}
