"""Integration tests for the read-only Apple daily report."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.analytics.aapl_daily_report_formatter import (
    format_aapl_daily_report,
)
from investment_analyst.analytics.aapl_daily_report_models import (
    AAPL_DAILY_REPORT_SCHEMA_VERSION,
)
from investment_analyst.analytics.aapl_daily_report_service import (
    AaplDailyReportService,
    AaplDailyReportTraceabilityError,
)
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticStatus,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
)
from investment_analyst.analytics.market.diagnostic_rules import (
    ALGORITHM_VERSION as MARKET_DIAGNOSTIC_ALGORITHM_VERSION,
)
from investment_analyst.analytics.market.statistics_definitions import SIMPLE_RETURN_KEY
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
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
)
from investment_analyst.storage import LocalStorage, StoragePaths

KNOWN_AT = datetime(2026, 7, 14, tzinfo=UTC)


def _metric(
    key: str,
    as_of: datetime,
    *,
    available_at: datetime = datetime(2026, 7, 1, tzinfo=UTC),
    frequency: DataFrequency | None = None,
) -> MetricResult:
    definition = next(
        (item for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS if item.metric_name == key),
        None,
    )
    parameters: dict[str, object] = {}
    algorithm = "market-simple-return-1d-v1-decimal34"
    if definition is not None:
        if frequency is None:
            raise ValueError("fundamental test metric requires a frequency")
        parameters = {
            "frequency": frequency.value,
            "period_end": as_of.isoformat(),
            "formula": definition.formula,
            "input_roles": [],
        }
        algorithm = definition.algorithm_version
    return MetricResult(
        asset_id="equity:us:aapl",
        metric_key=key,
        value=Decimal("0.125"),
        unit="ratio",
        as_of=as_of,
        available_at=available_at,
        computed_at=datetime(2026, 7, 16, tzinfo=UTC),
        parameters=parameters,
        input_observation_ids=[uuid4()],
        algorithm_version=algorithm,
        quality=DataQuality.VALID,
    )


def _diagnostic(
    mode: DiagnosticMode,
    metric: MetricResult,
    *,
    available_at: datetime | None = None,
) -> DiagnosticResult:
    score = Decimal("68") if mode is DiagnosticMode.MARKET else Decimal("72")
    algorithm = (
        MARKET_DIAGNOSTIC_ALGORITHM_VERSION
        if mode is DiagnosticMode.MARKET
        else FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION
    )
    return DiagnosticResult(
        asset_id="equity:us:aapl",
        mode=mode,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=score,
        confidence=Decimal("0.7"),
        as_of=metric.as_of,
        available_at=available_at or metric.available_at,
        computed_at=datetime(2026, 7, 20, tzinfo=UTC),
        components=[
            DiagnosticComponent(
                component_key=f"{mode.value}_test",
                score=score,
                weight=Decimal("1"),
                weighted_contribution=score,
                metric_result_ids=[metric.result_id],
                explanation="Independent descriptive component.",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("0.3"),
                reason="Independent descriptive evidence.",
            )
        ],
        algorithm_version=algorithm,
        summary=f"Independent {mode.value} diagnostic.",
        quality=DataQuality.VALID,
    )


def _request() -> ConsolidatedDiagnosticRequest:
    return ConsolidatedDiagnosticRequest(
        known_at=KNOWN_AT,
        fundamental_frequency=DataFrequency.QUARTERLY,
    )


def _counts(storage: LocalStorage) -> tuple[int, int, int, int]:
    connection = storage.store.connection
    return tuple(
        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "raw_record_index",
            "normalized_observations",
            "metric_results",
            "diagnostic_results",
        )
    )


def test_complete_report_resolves_evidence_and_formats_without_writes(tmp_path) -> None:
    market_metric = _metric(SIMPLE_RETURN_KEY, datetime(2026, 7, 10, tzinfo=UTC))
    fundamental_metric = _metric(
        "fundamental.net_margin",
        datetime(2026, 6, 30, tzinfo=UTC),
        frequency=DataFrequency.QUARTERLY,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.metric_results.save(market_metric)
        storage.metric_results.save(fundamental_metric)
        storage.diagnostics.save(_diagnostic(DiagnosticMode.MARKET, market_metric))
        storage.diagnostics.save(_diagnostic(DiagnosticMode.FUNDAMENTAL, fundamental_metric))
        before = _counts(storage)

        report = AaplDailyReportService(storage).query(_request())

        after = _counts(storage)

    assert before == after
    assert report.schema_version == AAPL_DAILY_REPORT_SCHEMA_VERSION
    assert report.view.status is ConsolidatedDiagnosticStatus.COMPLETE
    assert report.market.reference_age_days == 4
    assert report.market.availability_age_days == 13
    assert report.fundamental.reference_age_days == 14
    assert report.market.metrics[0].display_name == "Daily Simple Return"
    assert report.market.metrics[0].value == Decimal("0.125")
    assert report.fundamental.metrics[0].display_name == "Net Margin"

    payload = report.to_json_dict()
    assert payload["schema_version"] == AAPL_DAILY_REPORT_SCHEMA_VERSION
    assert payload["traceability"]["verified"] is True
    assert "combined_score" not in payload
    assert "combined_verdict" not in payload
    market_payload = payload["market"]
    assert market_payload["metrics"][0]["value"] == "0.125"
    assert market_payload["freshness"] == {
        "reference_age_days": 4,
        "availability_age_days": 13,
    }

    rendered = format_aapl_daily_report(report)
    assert "MARKET DIAGNOSTIC" in rendered
    assert "FUNDAMENTAL DIAGNOSTIC" in rendered
    assert "Daily Simple Return [market.history.simple_return_1d]" in rendered
    assert "Net Margin [fundamental.net_margin]" in rendered
    assert "Formula:" in rendered
    assert "Parameters:" in rendered
    assert "weighted contribution" in rendered
    assert "IEX is single-exchange coverage" in rendered
    assert str(market_metric.result_id) in rendered
    assert "1 input observation(s)" in rendered


def test_partial_and_unavailable_reports_are_valid_and_read_only(tmp_path) -> None:
    partial_root = tmp_path / "partial"
    market_metric = _metric(SIMPLE_RETURN_KEY, datetime(2026, 7, 10, tzinfo=UTC))
    with LocalStorage(StoragePaths.from_root(partial_root)) as storage:
        storage.metric_results.save(market_metric)
        storage.diagnostics.save(_diagnostic(DiagnosticMode.MARKET, market_metric))
        before = _counts(storage)
        partial = AaplDailyReportService(storage).query(_request())
        after = _counts(storage)

    assert before == after
    assert partial.view.status is ConsolidatedDiagnosticStatus.PARTIAL
    assert partial.market.metrics[0].result_id == market_metric.result_id
    assert partial.fundamental.metrics == ()
    assert partial.fundamental.reference_age_days is None
    assert partial.to_json_dict()["fundamental"]["freshness"] == {
        "reference_age_days": None,
        "availability_age_days": None,
    }

    unavailable_root = tmp_path / "unavailable"
    with LocalStorage(StoragePaths.from_root(unavailable_root)) as storage:
        before = _counts(storage)
        unavailable = AaplDailyReportService(storage).query(_request())
        after = _counts(storage)

    assert before == after
    assert unavailable.view.status is ConsolidatedDiagnosticStatus.UNAVAILABLE
    assert unavailable.market.metrics == ()
    assert unavailable.fundamental.metrics == ()
    assert "Reason: no eligible current-version market diagnostic" in format_aapl_daily_report(
        unavailable
    )


def test_report_rejects_selected_metric_unavailable_at_known_at(tmp_path) -> None:
    metric = _metric(
        SIMPLE_RETURN_KEY,
        datetime(2026, 7, 10, tzinfo=UTC),
        available_at=datetime(2026, 7, 15, tzinfo=UTC),
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.metric_results.save(metric)
        storage.diagnostics.save(
            _diagnostic(
                DiagnosticMode.MARKET,
                metric,
                available_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )

        with pytest.raises(AaplDailyReportTraceabilityError, match="unavailable at known_at"):
            AaplDailyReportService(storage).query(_request())
