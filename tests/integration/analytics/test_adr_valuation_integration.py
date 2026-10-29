"""End-to-end ADR security-unit basis valuation over a real scratch v2 workspace."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.valuation.models import (
    CorporateValuationRequest,
    CorporateValuationSnapshot,
    ValuationReasonCode,
    ValuationSnapshotStatus,
    ValuationStatus,
)
from investment_analyst.analytics.valuation.pipeline import (
    CorporateValuationPersistencePipeline,
)
from investment_analyst.analytics.valuation.service import CorporateValuationService
from investment_analyst.application.analysis_capabilities import analysis_capabilities_for
from investment_analyst.catalog.models import (
    SecurityUnitBasisAmbiguityError,
    SecurityUnitBasisRevision,
    select_security_unit_basis_revision,
)
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
)
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    IFRS_TRANSFORMATION_VERSION,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    DEI_SHARES_FIELD_NAME,
    DEI_SHARES_TRANSFORMATION_VERSION,
    SEC_DEI_SHARES_FACT_DEFINITION,
    get_sec_fact_definition,
)
from investment_analyst.providers.market.alpaca_normalizer import (
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_SOURCE_SHA_TSM = "9b4a4e6cd176e5f55976dbe067d006b395aab2570115d943e6c089560805a141"
_SOURCE_SHA_BVN = "ec2067c4ec169bbd068101dab16ad88159fac9be5825af76c7f851d71e83e8dd"
_TSM_AVAILABLE = datetime(2026, 4, 17, tzinfo=UTC)
_BVN_AVAILABLE = datetime(2026, 5, 1, tzinfo=UTC)
_FACTS = (
    ("fundamental.revenue", "100000", "USD"),
    ("fundamental.net_income", "20000", "USD"),
    ("fundamental.stockholders_equity", "80000", "USD"),
    ("fundamental.shares_outstanding", "1000", "shares"),
    ("fundamental.commercial_paper", "5000", "USD"),
    ("fundamental.long_term_debt_current", "2000", "USD"),
    ("fundamental.long_term_debt_noncurrent", "18000", "USD"),
    ("fundamental.cash_and_cash_equivalents", "20000", "USD"),
    ("fundamental.operating_income", "25000", "USD"),
    ("fundamental.operating_cash_flow", "30000", "USD"),
    ("fundamental.capital_expenditures", "10000", "USD"),
    ("fundamental.depreciation_and_amortization", "5000", "USD"),
)


def _revision(
    *,
    factor: str,
    valid_from: date,
    available_at: datetime,
    valid_to: date | None = None,
    accession_number: str = "0001628280-26-025362",
    cik: str = "0001046179",
    source_sha256: str = _SOURCE_SHA_TSM,
) -> SecurityUnitBasisRevision:
    return SecurityUnitBasisRevision(
        basis="depositary_receipt",
        factor=factor,
        adjustment="all",
        valid_from=valid_from,
        valid_to=valid_to,
        available_at=available_at,
        reported_share_unit="common_share",
        cik=cik,
        accession_number=accession_number,
        source_url="https://www.sec.gov/Archives/edgar/data/1046179/example.htm",
        source_sha256=source_sha256,
    )


def _with_change(
    revision: SecurityUnitBasisRevision, **changes: object
) -> SecurityUnitBasisRevision:
    return SecurityUnitBasisRevision.model_validate(
        {**revision.model_dump(exclude={"revision"}), **changes}
    )


def _v2_storage(root: Path) -> LocalStorage:
    initialization = WorkspaceService().initialize(root, format_version=2)
    paths = StoragePaths.from_workspace_root(
        initialization.paths.root,
        format_version=initialization.manifest.format_version,
        workspace_id=initialization.manifest.workspace_id,
    )
    return LocalStorage(paths)


def _seed_sec_facts(
    storage: LocalStorage,
    *,
    asset_id: str,
    source_id: str,
    available_at: datetime,
    accession_number: str,
    period_end: date = date(2025, 12, 31),
) -> None:
    period_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)
    observations: list[NormalizedObservation] = []
    raw_records: list[RawRecord] = []
    for field_name, value, unit in _FACTS:
        definition = get_sec_fact_definition(field_name)
        raw_id = uuid4()
        key = {
            "accession_number": accession_number,
            "taxonomy": definition.taxonomy,
            "tag": definition.tag,
            "unit": unit,
            "period": period_end.isoformat(),
            "companyfacts_record_id": str(raw_id),
            "submissions_record_id": str(uuid4()),
            "form": "20-F",
            "fiscal_year": "2025",
            "fiscal_period": "FY",
        }
        observations.append(
            NormalizedObservation(
                observation_id=uuid4(),
                raw_record_id=raw_id,
                asset_id=asset_id,
                field_name=field_name,
                value=Decimal(value),
                unit=unit,
                frequency=DataFrequency.ANNUAL,
                observed_at=period_at,
                period_end=period_at,
                available_at=available_at,
                normalized_at=available_at,
                source={
                    "source_id": source_id,
                    "record_key": json.dumps(key, separators=(",", ":"), sort_keys=True),
                    "retrieved_at": available_at,
                },
                quality=DataQuality.VALID,
                transformation_version="sec-companyfacts-normalizer-v2",
            )
        )
        raw_records.append(
            RawRecord(
                record_id=raw_id,
                asset_id=asset_id,
                source={
                    "source_id": source_id,
                    "record_key": json.dumps(
                        {"companyfacts_record_id": str(raw_id)},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "retrieved_at": available_at,
                },
                event_time=period_at,
                available_at=available_at,
                received_at=available_at,
                payload={"fixture": field_name},
                schema_version="sec-companyfacts-snapshot-v1",
            )
        )
    storage.raw_records.save_many(raw_records)
    storage.observations.save_many(observations)


def _seed_dei_ifrs_facts(
    storage: LocalStorage,
    *,
    asset_id: str,
    source_id: str,
    available_at: datetime,
    accession_number: str,
    period_end: date = date(2025, 12, 31),
    dei_shares: str = "1000",
) -> None:
    period_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)
    values = (
        ("fundamental.revenue", "100000"),
        ("fundamental.net_income", "20000"),
        ("fundamental.stockholders_equity", "80000"),
        ("fundamental.operating_income", "25000"),
        ("fundamental.depreciation_and_amortization", "5000"),
        ("fundamental.operating_cash_flow", "30000"),
        ("fundamental.capital_expenditures", "10000"),
        ("fundamental.cash_and_cash_equivalents", "20000"),
    )
    observations: list[NormalizedObservation] = []
    raw_records: list[RawRecord] = []
    definitions = [
        (field_name, value, get_sec_fact_definition(field_name, taxonomy="ifrs-full"))
        for field_name, value in values
    ]
    definitions.append((DEI_SHARES_FIELD_NAME, dei_shares, SEC_DEI_SHARES_FACT_DEFINITION))
    for field_name, value, definition in definitions:
        raw_id = uuid4()
        key = {
            "accession_number": accession_number,
            "taxonomy": definition.taxonomy,
            "tag": definition.tag,
            "unit": definition.unit,
            "period": period_end.isoformat(),
            "companyfacts_record_id": str(raw_id),
            "submissions_record_id": str(uuid4()),
            "form": "20-F",
            "fiscal_year": "2025",
            "fiscal_period": "FY",
        }
        observations.append(
            NormalizedObservation(
                observation_id=uuid4(),
                raw_record_id=raw_id,
                asset_id=asset_id,
                field_name=field_name,
                value=Decimal(value),
                unit=definition.unit,
                frequency=DataFrequency.ANNUAL,
                observed_at=period_at,
                period_end=period_at,
                available_at=available_at,
                normalized_at=available_at,
                source={
                    "source_id": source_id,
                    "record_key": json.dumps(key, separators=(",", ":"), sort_keys=True),
                    "retrieved_at": available_at,
                },
                quality=DataQuality.VALID,
                transformation_version=(
                    DEI_SHARES_TRANSFORMATION_VERSION
                    if definition.taxonomy == "dei"
                    else IFRS_TRANSFORMATION_VERSION
                ),
            )
        )
        raw_records.append(
            RawRecord(
                record_id=raw_id,
                asset_id=asset_id,
                source={
                    "source_id": source_id,
                    "record_key": json.dumps(
                        {"companyfacts_record_id": str(raw_id)},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "retrieved_at": available_at,
                },
                event_time=period_at,
                available_at=available_at,
                received_at=available_at,
                payload={"fixture": field_name},
                schema_version="sec-companyfacts-snapshot-v1",
            )
        )
    storage.raw_records.save_many(raw_records)
    storage.observations.save_many(observations)


def _seed_bar(
    storage: LocalStorage,
    *,
    configuration: AlpacaAssetConfiguration,
    timestamp: datetime,
    close: str,
) -> None:
    close_value = Decimal(close)
    bar = AlpacaStockBar(
        symbol=configuration.symbol,
        timestamp=timestamp,
        open=close_value - Decimal("1"),
        high=close_value + Decimal("2"),
        low=close_value - Decimal("2"),
        close=close_value,
        volume=Decimal("1000"),
        trade_count=Decimal("10"),
        vwap=close_value,
        raw_values={
            "t": timestamp.isoformat().replace("+00:00", "Z"),
            "o": str(close_value - Decimal("1")),
            "h": str(close_value + Decimal("2")),
            "l": str(close_value - Decimal("2")),
            "c": str(close_value),
            "v": "1000",
            "n": "10",
            "vw": str(close_value),
        },
    )
    retrieved_at = timestamp + timedelta(hours=2)
    raw = bar_to_raw_record(
        bar,
        retrieved_at=retrieved_at,
        request_url="https://data.alpaca.markets/adr-fixture",
        configuration=configuration,
    )
    storage.raw_records.save(raw)
    storage.observations.save_many(
        bar_to_observations(
            bar,
            raw,
            normalized_at=retrieved_at + timedelta(minutes=1),
            configuration=configuration,
        )
    )


def _configuration(catalog: AssetCatalogService, asset_id: str) -> AlpacaAssetConfiguration:
    asset = catalog.get(asset_id)
    configuration = resolve_alpaca_configuration(
        ProviderAssetContextResolver(catalog), asset_id=asset_id
    )
    return AlpacaAssetConfiguration(
        asset_id=asset.asset_id,
        symbol=asset.symbol,
        feed="iex",
        adjustment="all",
        source_id=configuration.source_id,
        name=asset.name,
        asset_class=asset.asset_class,
        quote_currency=asset.quote_currency,
        exchange=asset.exchange or "NYSE",
    )


def _service(
    storage: LocalStorage,
    asset_id: str,
    *,
    catalog: AssetCatalogService,
    revisions: tuple[SecurityUnitBasisRevision, ...] | None = None,
) -> CorporateValuationService:
    asset = catalog.get(asset_id)
    resolver = ProviderAssetContextResolver(catalog)
    return CorporateValuationService(
        storage,
        capabilities=analysis_capabilities_for(asset),
        market_source_id=resolve_alpaca_configuration(resolver, asset_id=asset_id).source_id,
        fundamental_source_id=resolve_sec_configuration(
            resolver, asset_id=asset_id
        ).companyfacts_source_id,
        price_currency=asset.quote_currency,
        security_unit_factor=asset.security_unit_factor,
        security_unit_basis=asset.security_unit_basis,
        security_unit_basis_version=asset.security_unit_basis_version,
        security_unit_market_adjustment=asset.security_unit_market_adjustment,
        security_unit_basis_revisions=(
            asset.security_unit_basis_revisions if revisions is None else revisions
        ),
    )


def _market_cap(snapshot: CorporateValuationSnapshot) -> Decimal:
    return next(
        metric.value
        for metric in snapshot.metrics
        if metric.metric_key == "valuation.corporate.market_cap" and metric.value is not None
    )


def _seed_adr(
    storage: LocalStorage,
    *,
    catalog: AssetCatalogService,
    asset_id: str,
    available_at: datetime,
    accession_number: str,
    bar_date: date,
    close: str = "100",
    known_at: datetime | None = None,
) -> None:
    configuration = _configuration(catalog, asset_id)
    _seed_sec_facts(
        storage,
        asset_id=asset_id,
        source_id=resolve_sec_configuration(
            ProviderAssetContextResolver(catalog), asset_id=asset_id
        ).companyfacts_source_id,
        available_at=available_at,
        accession_number=accession_number,
    )
    timestamp = datetime.combine(bar_date, datetime.min.time(), tzinfo=UTC) + timedelta(hours=16)
    _seed_bar(storage, configuration=configuration, timestamp=timestamp, close=close)


def test_tsm_revision_evaluates_persists_and_replays_identically(tmp_path: Path) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    request = CorporateValuationRequest(
        asset_id="equity:us:tsm",
        known_at=datetime(2026, 4, 17, 20, tzinfo=UTC),
        valuation_date=date(2026, 4, 17),
    )
    computed_at = datetime(2026, 4, 17, 21, tzinfo=UTC)

    with storage:
        _seed_adr(
            storage,
            catalog=catalog,
            asset_id="equity:us:tsm",
            available_at=_TSM_AVAILABLE,
            accession_number="0001628280-26-025362",
            bar_date=date(2026, 4, 17),
        )
        first = _service(storage, "equity:us:tsm", catalog=catalog).query(
            request, computed_at=computed_at
        )
        assert first.status is ValuationSnapshotStatus.EVALUATED
        assert first.security_basis is not None
        assert first.security_basis.basis == "depositary_receipt"
        assert first.security_basis.market_units_per_reported_share == Decimal("0.2")
        assert first.security_basis.basis_evidence is not None
        assert first.security_basis.contract_version == (
            f"security-unit-basis-v2:{first.security_basis.basis_evidence.revision}"
        )
        assert _market_cap(first) == Decimal("20000")
        assert "share_basis_unavailable" not in {
            metric.reason_code.value for metric in first.metrics if metric.reason_code is not None
        }

        persisted = CorporateValuationPersistencePipeline(storage).persist(first)
        assert persisted.metric_results_created == 11

        replay = _service(storage, "equity:us:tsm", catalog=catalog).query(
            request, computed_at=computed_at + timedelta(minutes=5)
        )
        assert tuple(metric.result_id for metric in replay.metrics) == tuple(
            metric.result_id for metric in first.metrics
        )
        assert tuple(metric.value for metric in replay.metrics) == tuple(
            metric.value for metric in first.metrics
        )
        assert (
            CorporateValuationPersistencePipeline(storage).persist(replay).metric_results_created
            == 0
        )

    reopened_storage = LocalStorage(storage.paths)
    with reopened_storage:
        reopened = _service(reopened_storage, "equity:us:tsm", catalog=catalog).query(
            request, computed_at=computed_at + timedelta(minutes=10)
        )
        assert tuple(metric.result_id for metric in reopened.metrics) == tuple(
            metric.result_id for metric in first.metrics
        )
        assert _market_cap(reopened) == Decimal("20000")
        assert (
            CorporateValuationPersistencePipeline(reopened_storage)
            .persist(reopened)
            .metric_results_created
            == 0
        )


def test_ratio_change_creates_new_identity_and_preserves_history(tmp_path: Path) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    request = CorporateValuationRequest(
        asset_id="equity:us:tsm",
        known_at=datetime(2026, 4, 17, 20, tzinfo=UTC),
        valuation_date=date(2026, 4, 17),
    )
    baseline_revisions = catalog.get("equity:us:tsm").security_unit_basis_revisions
    changed = tuple(_with_change(item, factor=Decimal("5")) for item in baseline_revisions)

    with storage:
        _seed_adr(
            storage,
            catalog=catalog,
            asset_id="equity:us:tsm",
            available_at=_TSM_AVAILABLE,
            accession_number="0001628280-26-025362",
            bar_date=date(2026, 4, 17),
        )
        baseline = _service(storage, "equity:us:tsm", catalog=catalog).query(
            request, computed_at=datetime(2026, 4, 17, 21, tzinfo=UTC)
        )
        CorporateValuationPersistencePipeline(storage).persist(baseline)

        changed_snapshot = _service(
            storage, "equity:us:tsm", catalog=catalog, revisions=changed
        ).query(request, computed_at=datetime(2026, 4, 17, 21, tzinfo=UTC))
        assert _market_cap(changed_snapshot) == Decimal("500000")
        assert set(metric.result_id for metric in changed_snapshot.metrics).isdisjoint(
            metric.result_id for metric in baseline.metrics
        )
        summary = CorporateValuationPersistencePipeline(storage).persist(changed_snapshot)
        assert summary.metric_results_created == 11
        assert summary.metric_results_reused == 0
        assert storage.metric_results.get_existing(
            {metric.result_id for metric in baseline.metrics if metric.result_id is not None}
        )


def test_bvn_factor_one_and_pre_span_price_mismatch(tmp_path: Path) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    known_at = datetime(2026, 5, 1, 20, tzinfo=UTC)

    with storage:
        configuration = _configuration(catalog, "equity:us:bvn")
        _seed_sec_facts(
            storage,
            asset_id="equity:us:bvn",
            source_id=resolve_sec_configuration(
                ProviderAssetContextResolver(catalog), asset_id="equity:us:bvn"
            ).companyfacts_source_id,
            available_at=_BVN_AVAILABLE,
            accession_number="0001104659-26-053101",
        )
        _seed_bar(
            storage,
            configuration=configuration,
            timestamp=datetime(2026, 4, 30, 16, tzinfo=UTC),
            close="100",
        )
        previous_price = _service(storage, "equity:us:bvn", catalog=catalog).query(
            CorporateValuationRequest(
                asset_id="equity:us:bvn",
                known_at=known_at,
                valuation_date=date(2026, 5, 1),
            ),
            computed_at=known_at,
        )
        assert previous_price.security_basis is not None
        assert previous_price.security_basis.market_units_per_reported_share == Decimal("1")
        assert previous_price.status is ValuationSnapshotStatus.NOT_EVALUABLE
        assert {metric.reason_code for metric in previous_price.metrics} == {
            ValuationReasonCode.SECURITY_UNIT_MISMATCH
        }

        _seed_bar(
            storage,
            configuration=configuration,
            timestamp=datetime(2026, 5, 1, 16, tzinfo=UTC),
            close="100",
        )
        evaluated = _service(storage, "equity:us:bvn", catalog=catalog).query(
            CorporateValuationRequest(
                asset_id="equity:us:bvn",
                known_at=known_at,
                valuation_date=date(2026, 5, 1),
            ),
            computed_at=known_at,
        )
        assert evaluated.status is ValuationSnapshotStatus.EVALUATED
        assert _market_cap(evaluated) == Decimal("100000")


def test_cut_before_availability_and_before_span_remain_unavailable(
    tmp_path: Path,
) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")

    with storage:
        _seed_adr(
            storage,
            catalog=catalog,
            asset_id="equity:us:tsm",
            available_at=_TSM_AVAILABLE,
            accession_number="0001628280-26-025362",
            bar_date=date(2026, 4, 17),
        )
        service = _service(storage, "equity:us:tsm", catalog=catalog)
        early_cut = service.query(
            CorporateValuationRequest(
                asset_id="equity:us:tsm",
                known_at=datetime(2026, 4, 16, 20, tzinfo=UTC),
                valuation_date=date(2026, 4, 17),
            ),
            computed_at=datetime(2026, 4, 17, 21, tzinfo=UTC),
        )
        assert early_cut.security_basis is None
        assert {metric.reason_code for metric in early_cut.metrics} == {
            ValuationReasonCode.SHARE_BASIS_UNAVAILABLE
        }

        early_date = service.query(
            CorporateValuationRequest(
                asset_id="equity:us:tsm",
                known_at=datetime(2026, 4, 17, 20, tzinfo=UTC),
                valuation_date=date(2026, 4, 16),
            ),
            computed_at=datetime(2026, 4, 17, 21, tzinfo=UTC),
        )
        assert early_date.security_basis is None
        assert {metric.reason_code for metric in early_date.metrics} == {
            ValuationReasonCode.SHARE_BASIS_UNAVAILABLE
        }


def test_corrective_revision_closes_the_interval_without_rewriting_history() -> None:
    open_revision = _revision(
        factor="0.2",
        valid_from=date(2026, 4, 17),
        available_at=datetime(2026, 4, 17, tzinfo=UTC),
    )
    correction = _revision(
        factor="0.25",
        valid_from=date(2026, 4, 17),
        valid_to=date(2026, 6, 1),
        available_at=datetime(2026, 5, 20, tzinfo=UTC),
    )
    revisions = (open_revision, correction)

    before = select_security_unit_basis_revision(
        revisions,
        known_at=datetime(2026, 5, 19, tzinfo=UTC),
        valuation_date=date(2026, 5, 15),
    )
    after = select_security_unit_basis_revision(
        revisions,
        known_at=datetime(2026, 5, 21, tzinfo=UTC),
        valuation_date=date(2026, 5, 15),
    )
    outside = select_security_unit_basis_revision(
        revisions,
        known_at=datetime(2026, 5, 21, tzinfo=UTC),
        valuation_date=date(2026, 6, 1),
    )

    assert before is not None and before.revision == open_revision.revision
    assert after is not None and after.revision == correction.revision
    assert outside is None


def test_ambiguous_overlap_and_equal_availability_fail_closed() -> None:
    first = _revision(
        factor="0.2",
        valid_from=date(2026, 4, 17),
        available_at=datetime(2026, 4, 17, tzinfo=UTC),
    )
    overlapping = _revision(
        factor="0.5",
        valid_from=date(2026, 5, 1),
        available_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    with pytest.raises(SecurityUnitBasisAmbiguityError):
        select_security_unit_basis_revision(
            (first, overlapping),
            known_at=datetime(2026, 6, 1, tzinfo=UTC),
            valuation_date=date(2026, 6, 1),
        )

    conflicting_tie = _revision(
        factor="0.3",
        valid_from=date(2026, 4, 17),
        available_at=datetime(2026, 4, 17, tzinfo=UTC),
    )
    with pytest.raises(SecurityUnitBasisAmbiguityError):
        select_security_unit_basis_revision(
            (first, conflicting_tie),
            known_at=datetime(2026, 6, 1, tzinfo=UTC),
            valuation_date=date(2026, 6, 1),
        )


def test_invalid_revisions_and_contradictory_declarations_are_rejected() -> None:
    with pytest.raises(ValidationError, match="not float"):
        _revision(
            factor=0.2,  # type: ignore[arg-type]
            valid_from=date(2026, 4, 17),
            available_at=_TSM_AVAILABLE,
        )
    with pytest.raises(ValidationError, match="finite positive"):
        _revision(
            factor="0",
            valid_from=date(2026, 4, 17),
            available_at=_TSM_AVAILABLE,
        )
    with pytest.raises(ValidationError, match="source_sha256"):
        _revision(
            factor="0.2",
            valid_from=date(2026, 4, 17),
            available_at=_TSM_AVAILABLE,
            source_sha256="NOT-A-DIGEST",
        )
    with pytest.raises(ValidationError, match="valid_to must be after valid_from"):
        _revision(
            factor="0.2",
            valid_from=date(2026, 4, 17),
            valid_to=date(2026, 4, 17),
            available_at=_TSM_AVAILABLE,
        )

    catalog = AssetCatalogService.load_default()
    conflicting = catalog.get("equity:us:tsm").model_copy(
        update={
            "security_unit_factor": Decimal("0.2"),
            "security_unit_basis": "depositary_receipt",
            "security_unit_basis_version": "security-unit-basis-v1",
            "security_unit_market_adjustment": "all",
        }
    )
    with pytest.raises(ValidationError, match="must not coexist"):
        conflicting.__class__.model_validate(conflicting.model_dump(exclude={"revision"}))


def test_equivalent_declarations_keep_asset_own_identity() -> None:
    catalog = AssetCatalogService.load_default()
    first = catalog.get("equity:us:tsm").security_unit_basis_revisions[0]
    second = catalog.get("equity:us:bvn").security_unit_basis_revisions[0]
    assert (
        first.revision
        == SecurityUnitBasisRevision.model_validate(first.model_dump(exclude={"revision"})).revision
    )
    assert first.revision != second.revision
    equivalent = _with_change(first, source_url=first.source_url)
    assert equivalent.revision == first.revision
    changed_availability = _with_change(first, available_at=first.available_at + timedelta(days=1))
    assert changed_availability.revision != first.revision
    changed_factor = _with_change(first, factor=Decimal("0.25"))
    assert changed_factor.revision != first.revision


def test_catalog_semantic_digest_tracks_declared_revisions() -> None:
    catalog = AssetCatalogService.load_default()
    baseline = catalog.semantic_digest
    changed_asset = catalog.get("equity:us:tsm").model_copy(
        update={
            "security_unit_basis_revisions": (
                _with_change(
                    catalog.get("equity:us:tsm").security_unit_basis_revisions[0],
                    factor=Decimal("0.25"),
                ),
            )
        }
    )
    document = catalog.document.model_copy(
        update={
            "assets": tuple(
                changed_asset if item.asset_id == changed_asset.asset_id else item
                for item in catalog.document.assets
            )
        }
    )
    assert AssetCatalogService(document).semantic_digest != baseline
    assert AssetCatalogService.load_default().semantic_digest == baseline


def test_ordinary_common_share_assets_keep_legacy_basis(tmp_path: Path) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    configuration = _configuration(catalog, "equity:us:aapl")
    with storage:
        _seed_sec_facts(
            storage,
            asset_id="equity:us:aapl",
            source_id=resolve_sec_configuration(
                ProviderAssetContextResolver(catalog), asset_id="equity:us:aapl"
            ).companyfacts_source_id,
            available_at=datetime(2026, 1, 30, tzinfo=UTC),
            accession_number="0000320193-25-000079",
            period_end=date(2025, 9, 27),
        )
        _seed_bar(
            storage,
            configuration=configuration,
            timestamp=datetime(2026, 1, 30, 16, tzinfo=UTC),
            close="100",
        )
        snapshot = _service(storage, "equity:us:aapl", catalog=catalog).query(
            CorporateValuationRequest(
                asset_id="equity:us:aapl",
                known_at=datetime(2026, 2, 1, tzinfo=UTC),
                valuation_date=date(2026, 1, 30),
            ),
            computed_at=datetime(2026, 2, 2, tzinfo=UTC),
        )
        assert snapshot.security_basis is not None
        assert snapshot.security_basis.basis == "reported_common_share"
        assert snapshot.security_basis.basis_evidence is None
        assert snapshot.security_basis.contract_version == "security-unit-basis-v1"
        assert _market_cap(snapshot) == Decimal("100000")
        assert [metric.status for metric in snapshot.metrics] == [ValuationStatus.EVALUATED] * 11


def test_dei_shares_enable_ifrs_adr_market_cap_and_replay(tmp_path: Path) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    request = CorporateValuationRequest(
        asset_id="equity:us:tsm",
        known_at=datetime(2026, 4, 17, 20, tzinfo=UTC),
        valuation_date=date(2026, 4, 17),
    )
    computed_at = datetime(2026, 4, 17, 21, tzinfo=UTC)

    with storage:
        _seed_dei_ifrs_facts(
            storage,
            asset_id="equity:us:tsm",
            source_id=resolve_sec_configuration(
                ProviderAssetContextResolver(catalog), asset_id="equity:us:tsm"
            ).companyfacts_source_id,
            available_at=_TSM_AVAILABLE,
            accession_number="0001193125-25-083423",
        )
        _seed_bar(
            storage,
            configuration=_configuration(catalog, "equity:us:tsm"),
            timestamp=datetime(2026, 4, 17, 16, tzinfo=UTC),
            close="100",
        )
        first = _service(storage, "equity:us:tsm", catalog=catalog).query(
            request, computed_at=computed_at
        )
        by_key = {metric.metric_key: metric for metric in first.metrics}
        shares = next(item for item in first.inputs if item.role == "shares_outstanding")

        assert first.status is ValuationSnapshotStatus.PARTIAL
        assert first.report_currency == "USD"
        assert _market_cap(first) == Decimal("20000")
        assert by_key["valuation.corporate.price_to_earnings_latest_annual"].value == Decimal("1")
        assert by_key["valuation.corporate.price_to_sales_latest_annual"].value == Decimal("0.2")
        assert shares.taxonomy == "dei"
        assert shares.tag == "EntityCommonStockSharesOutstanding"
        assert shares.value == Decimal("1000")
        assert first.coverage.evaluated == 6

        persisted = CorporateValuationPersistencePipeline(storage).persist(first)
        assert persisted.metric_results_created == 6

        replay = _service(storage, "equity:us:tsm", catalog=catalog).query(
            request, computed_at=computed_at + timedelta(minutes=5)
        )
        assert tuple(metric.result_id for metric in replay.metrics) == tuple(
            metric.result_id for metric in first.metrics
        )
        assert (
            CorporateValuationPersistencePipeline(storage).persist(replay).metric_results_created
            == 0
        )

    reopened_storage = LocalStorage(storage.paths)
    with reopened_storage:
        reopened = _service(reopened_storage, "equity:us:tsm", catalog=catalog).query(
            request, computed_at=computed_at + timedelta(minutes=10)
        )
        assert _market_cap(reopened) == Decimal("20000")
        assert (
            CorporateValuationPersistencePipeline(reopened_storage)
            .persist(reopened)
            .metric_results_created
            == 0
        )
