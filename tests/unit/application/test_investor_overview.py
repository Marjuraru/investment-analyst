"""Contract and projection tests for the ephemeral investor overview."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.application.investor_overview_models import (
    InvestorOverviewRequest,
    InvestorOverviewResult,
    InvestorReferences,
    InvestorValue,
    InvestorValueState,
)
from investment_analyst.application.investor_overview_service import (
    InvestorOverviewIntegrityError,
    InvestorOverviewService,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_rules import (
    ALGORITHM_VERSION as FUNDAMENTAL_ALGORITHM_VERSION,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
)
from investment_analyst.storage import LocalStorage, StoragePaths

KNOWN_AT = datetime(2026, 8, 29, tzinfo=UTC)
AAPL = "equity:us:aapl"


def _request(*asset_ids: str, known_at: datetime = KNOWN_AT) -> InvestorOverviewRequest:
    return InvestorOverviewRequest(known_at=known_at, asset_ids=tuple(sorted(asset_ids)))


def _service(storage: LocalStorage) -> InvestorOverviewService:
    runtime = ApplicationRuntime.create_default()
    return InvestorOverviewService(storage, runtime.catalog, runtime.provider_resolver)


def test_request_bounds_and_derived_windows() -> None:
    request = _request("equity:us:aapl")
    assert request.market_end == date(2026, 8, 28)
    assert (request.market_end - request.market_start).days == 365
    assert (request.market_end - request.fundamental_start).days == 3660
    for invalid in ((), tuple(f"a{i}" for i in range(11)), ("b", "a"), ("a", "a")):
        with pytest.raises(ValidationError):
            InvestorOverviewRequest(known_at=KNOWN_AT, asset_ids=invalid)
    with pytest.raises(ValidationError):
        InvestorOverviewRequest(known_at=KNOWN_AT, asset_ids=("a",), extra="x")  # type: ignore[call-arg]
    assert len(_request(*(f"a{i:02d}" for i in range(10))).asset_ids) == 10


def test_value_state_is_exact_and_never_a_number_when_absent() -> None:
    available = InvestorValue(
        state=InvestorValueState.AVAILABLE, value=Decimal("0.10"), unit="ratio"
    )
    assert available.model_dump(mode="json")["value"] == "0.10"
    with pytest.raises(ValidationError):
        InvestorValue(state=InvestorValueState.AVAILABLE, value=Decimal("1"))
    with pytest.raises(ValidationError):
        InvestorValue(state=InvestorValueState.MISSING, value=Decimal("0"), unit="ratio")
    with pytest.raises(ValidationError):
        InvestorValue(state=InvestorValueState.AVAILABLE, value=Decimal("NaN"), unit="ratio")
    assert InvestorReferences().observation_ids == ()


def test_result_requires_sorted_unique_assets(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        result = _service(storage).query(_request(AAPL, "etf:us:spy"))
    assert [item.asset_id for item in result.assets] == [AAPL, "etf:us:spy"]
    with pytest.raises(ValidationError):
        InvestorOverviewResult(
            known_at=KNOWN_AT,
            catalog_version=result.catalog_version,
            catalog_sha256=result.catalog_sha256,
            windows=result.windows,
            assets=tuple(reversed(result.assets)),
        )


def test_empty_workspace_declares_absence_by_class_never_zero(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        result = _service(storage).query(
            _request(AAPL, "crypto:btc-usd", "etf:us:spy", "equity:us:tsm")
        )
    by_id = {item.asset_id: item for item in result.assets}
    aapl = by_id[AAPL]
    assert aapl.market.close.state is InvestorValueState.MISSING
    assert aapl.market.close.value is None
    assert aapl.fundamental.score.state is InvestorValueState.MISSING
    assert aapl.valuation.price_to_earnings.state is InvestorValueState.INSUFFICIENT_DATA
    assert aapl.valuation.basis == "latest_annual"
    for asset_id in ("crypto:btc-usd", "etf:us:spy"):
        item = by_id[asset_id]
        assert item.fundamental.score.state is InvestorValueState.NOT_APPLICABLE
        assert item.valuation.price_to_earnings.state is InvestorValueState.NOT_APPLICABLE
    assert result.windows.fundamental_frequency == "annual"
    assert result.model_dump_json().count("NaN") == 0


def test_unknown_asset_is_rejected_by_the_service(tmp_path) -> None:
    with (
        LocalStorage(StoragePaths.from_root(tmp_path)) as storage,
        pytest.raises(ValueError, match="not configured in the catalog"),
    ):
        _service(storage).query(_request("equity:us:nope"))


def _fundamental_metric(key: str, value: str, as_of: datetime) -> MetricResult:
    definition = next(
        item for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS if item.metric_name == key
    )
    inputs = [uuid4(), uuid4()]
    return MetricResult(
        asset_id=AAPL,
        metric_key=key,
        value=Decimal(value),
        unit=definition.unit,
        as_of=as_of,
        available_at=datetime(2026, 7, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 2, tzinfo=UTC),
        parameters={
            "frequency": DataFrequency.ANNUAL.value,
            "period_end": as_of.isoformat(),
            "formula": definition.formula,
            "input_roles": [
                {"role": "a", "observation_id": str(inputs[0])},
                {"role": "b", "observation_id": str(inputs[1])},
            ],
        },
        input_observation_ids=inputs,
        algorithm_version=definition.algorithm_version,
        quality=DataQuality.VALID,
    )


def _seed_annual_diagnostic(
    storage: LocalStorage, *, as_of: datetime, score: str
) -> DiagnosticResult:
    margin = _fundamental_metric("fundamental.net_margin", "0.2400", as_of)
    leverage = _fundamental_metric("fundamental.liabilities_to_assets", "0.6100", as_of)
    storage.metric_results.save(margin)
    storage.metric_results.save(leverage)
    value = Decimal(score)
    half = value / 2
    diagnostic = DiagnosticResult(
        asset_id=AAPL,
        mode=DiagnosticMode.FUNDAMENTAL,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=value,
        confidence=Decimal("0.8"),
        as_of=as_of,
        available_at=datetime(2026, 7, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 3, tzinfo=UTC),
        components=[
            DiagnosticComponent(
                component_key=key,
                score=value,
                weight=Decimal("0.5"),
                weighted_contribution=half,
                metric_result_ids=[metric.result_id],
                explanation="Descriptive component.",
            )
            for key, metric in (("net_margin", margin), ("liabilities", leverage))
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("0.4"),
                reason="Descriptive evidence.",
            )
            for metric in (margin, leverage)
        ],
        algorithm_version=FUNDAMENTAL_ALGORITHM_VERSION,
        summary="Descriptive diagnostic without recommendation.",
        quality=DataQuality.VALID,
    )
    storage.diagnostics.save(diagnostic)
    return diagnostic


def test_fundamental_projection_keeps_exact_decimals_and_real_references(tmp_path) -> None:
    as_of = datetime(2025, 9, 27, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        diagnostic = _seed_annual_diagnostic(storage, as_of=as_of, score="72.5000")
        result = _service(storage).query(_request(AAPL))
    fundamental = result.assets[0].fundamental
    payload = fundamental.model_dump(mode="json")
    assert payload["score"]["value"] == "72.5000"
    assert payload["net_margin"]["value"] == "0.2400"
    assert payload["liabilities_to_assets"]["value"] == "0.6100"
    assert fundamental.score.references.diagnostic_id == diagnostic.diagnostic_id
    assert fundamental.net_margin.references.metric_result_id is not None
    assert len(fundamental.net_margin.references.observation_ids) == 2
    assert fundamental.verdict == "positive"
    assert fundamental.algorithm_version == FUNDAMENTAL_ALGORITHM_VERSION
    assert fundamental.score.unit == "score_0_100"


def test_diagnostic_outside_window_is_not_reported_as_universal_absence(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_annual_diagnostic(storage, as_of=datetime(2012, 9, 30, tzinfo=UTC), score="60")
        result = _service(storage).query(_request(AAPL))
    score = result.assets[0].fundamental.score
    assert score.state is InvestorValueState.MISSING
    assert score.reason_codes == ("annual_diagnostic_outside_window",)
    assert score.reference_at is not None


def test_future_published_diagnostic_is_not_visible_at_the_cut(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_annual_diagnostic(storage, as_of=datetime(2025, 9, 27, tzinfo=UTC), score="60")
        before = _service(storage).query(_request(AAPL, known_at=datetime(2026, 6, 1, tzinfo=UTC)))
    assert before.assets[0].fundamental.score.state is InvestorValueState.MISSING
    assert before.assets[0].fundamental.score.reason_codes == ("no_annual_diagnostic_at_known_at",)


def test_ambiguous_one_day_return_revisions_raise_instead_of_choosing(
    tmp_path, monkeypatch
) -> None:
    from investment_analyst.analytics.market.bar_models import MarketBar
    from investment_analyst.application.universe_coverage_service import UniverseCoverageService

    close_id = uuid4()
    timestamp = datetime(2026, 8, 28, tzinfo=UTC)
    bar = MarketBar(
        asset_id=AAPL,
        source_id="alpaca-market-data:iex:aapl:daily-bars:adjustment-all",
        raw_record_id=uuid4(),
        frequency=DataFrequency.DAY_1,
        timestamp=timestamp,
        available_at=datetime(2026, 8, 28, 21, tzinfo=UTC),
        open=Decimal("10"),
        high=Decimal("12"),
        low=Decimal("9"),
        close=Decimal("11"),
        volume=Decimal("5"),
        quality=DataQuality.VALID,
        observation_ids={
            "open": uuid4(),
            "high": uuid4(),
            "low": uuid4(),
            "close": close_id,
            "volume": uuid4(),
        },
    )

    def result(value: str) -> MetricResult:
        return MetricResult(
            asset_id=AAPL,
            metric_key="market.history.simple_return_1d",
            value=Decimal(value),
            unit="ratio",
            as_of=timestamp,
            available_at=datetime(2026, 8, 28, 22, tzinfo=UTC),
            computed_at=datetime(2026, 8, 28, 23, tzinfo=UTC),
            parameters={"source_id": bar.source_id},
            input_observation_ids=[uuid4(), close_id],
            algorithm_version="market-simple-return-1d-v1-decimal34",
            quality=DataQuality.VALID,
        )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.metric_results.save(result("0.0100"))
        monkeypatch.setattr(
            UniverseCoverageService,
            "selected_market",
            lambda self, descriptor, request, storage: (
                type("M", (), {"capability": "supported", "source_id": bar.source_id})(),
                bar,
            ),
        )
        monkeypatch.setattr(
            "investment_analyst.application.investor_overview_service.CoverageCapability",
            type("C", (), {"SUPPORTED": "supported"}),
        )
        single = _service(storage).query(_request(AAPL))
        assert single.assets[0].market.change_1d.value == Decimal("0.0100")
        assert single.assets[0].market.close.value == Decimal("11")
        assert single.assets[0].market.change_1d.unit == "ratio"
        storage.metric_results.save(result("0.0200"))
        with pytest.raises(InvestorOverviewIntegrityError):
            _service(storage).query(_request(AAPL))
