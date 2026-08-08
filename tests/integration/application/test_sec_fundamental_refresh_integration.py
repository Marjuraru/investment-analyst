"""Offline integration tests for one catalog-backed SEC issuer refresh."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from investment_analyst.analytics.valuation import (
    CorporateValuationRequest,
    ValuationPersistenceSummary,
)
from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalKnownAtTooEarlyError,
    SecIssuerFundamentalRefreshPipeline,
    SecIssuerFundamentalRefreshStageError,
)
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshStage,
)
from investment_analyst.core.models import AssetClass, DataFrequency
from investment_analyst.providers.asset_config import (
    SecAccountingStandard,
    SecAssetConfiguration,
)
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_pipeline import (
    SecIssuerFundamentalDiagnosticPipeline,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecIssuerFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecIssuerObservationPipeline,
)
from investment_analyst.providers.fundamentals.sec_pipeline import (
    SecIssuerFundamentalsPipeline,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecIssuerFundamentalPointInTimeService,
)
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

_CONFIGURATION = SecAssetConfiguration(
    asset_id="equity:us:amd",
    cik="0000002488",
    ticker="AMD",
    submissions_source_id="sec-edgar:amd:submissions",
    companyfacts_source_id="sec-edgar:amd:companyfacts",
    name="Advanced Micro Devices, Inc.",
    asset_class=AssetClass.EQUITY,
    quote_currency="USD",
    exchange="NASDAQ",
)
_FETCHED_AT = datetime(2026, 7, 28, 15, tzinfo=UTC)
_NORMALIZED_AT = datetime(2026, 7, 28, 15, 1, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
_COMPUTED_AT = datetime(2027, 1, 1, tzinfo=UTC)
_ANNUAL_ACCN = "0000002488-26-000001"
_QUARTERLY_ACCN = "0000002488-26-000002"


class _FixtureTransport:
    """Return the two prepared issuer documents without network access."""

    def __init__(self, submissions: bytes, companyfacts: bytes) -> None:
        self._bodies = [submissions, companyfacts]
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append(url)
        return HttpResponse(200, self._bodies.pop(0), {}, url)


class _FailingDiagnosticPipeline:
    def run(self, request):
        raise RuntimeError("synthetic diagnostic failure")


class _ValuationService:
    def __init__(self) -> None:
        self.requests: list[CorporateValuationRequest] = []

    def query(self, request: CorporateValuationRequest) -> CorporateValuationRequest:
        self.requests.append(request)
        return request


class _ValuationPipeline:
    def persist(self, snapshot: CorporateValuationRequest) -> ValuationPersistenceSummary:
        return ValuationPersistenceSummary(
            definitions_created=11,
            definitions_reused=0,
            metric_results_created=6,
            metric_results_reused=0,
            metrics_not_evaluable=5,
            metrics_not_applicable=0,
        )


def _documents() -> tuple[bytes, bytes]:
    submissions = {
        "cik": _CONFIGURATION.cik,
        "name": _CONFIGURATION.name,
        "tickers": [_CONFIGURATION.ticker],
        "exchanges": [_CONFIGURATION.exchange],
        "filings": {
            "recent": {
                "accessionNumber": [_ANNUAL_ACCN, _QUARTERLY_ACCN],
                "filingDate": ["2026-02-04", "2026-04-29"],
                "reportDate": ["2025-12-27", "2026-03-28"],
                "acceptanceDateTime": [
                    "2026-02-04T21:00:00Z",
                    "2026-04-29T21:00:00Z",
                ],
                "form": ["10-K", "10-Q"],
                "primaryDocument": ["amd-20251227.htm", "amd-20260328.htm"],
            },
            "files": [],
        },
    }

    def duration(
        *,
        annual: bool,
        value: str,
    ) -> dict[str, str]:
        return {
            "start": "2024-12-29" if annual else "2025-12-28",
            "end": "2025-12-27" if annual else "2026-03-28",
            "val": value,
            "accn": _ANNUAL_ACCN if annual else _QUARTERLY_ACCN,
            "fy": "2025" if annual else "2026",
            "fp": "FY" if annual else "Q1",
            "form": "10-K" if annual else "10-Q",
            "filed": "2026-02-04" if annual else "2026-04-29",
        }

    def instant(
        *,
        annual: bool,
        value: str,
    ) -> dict[str, str]:
        result = duration(annual=annual, value=value)
        del result["start"]
        return result

    facts = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {
                "USD": [
                    duration(annual=True, value="25000"),
                    duration(annual=False, value="7000"),
                ]
            }
        },
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    duration(annual=True, value="4000"),
                    duration(annual=False, value="1200"),
                ]
            }
        },
        "Assets": {
            "units": {
                "USD": [
                    instant(annual=True, value="70000"),
                    instant(annual=False, value="72000"),
                ]
            }
        },
        "Liabilities": {
            "units": {
                "USD": [
                    instant(annual=True, value="25000"),
                    instant(annual=False, value="26000"),
                ]
            }
        },
        "StockholdersEquity": {
            "units": {
                "USD": [
                    instant(annual=True, value="45000"),
                    instant(annual=False, value="46000"),
                ]
            }
        },
    }
    companyfacts = {
        "cik": _CONFIGURATION.cik,
        "entityName": _CONFIGURATION.name,
        "facts": {"us-gaap": facts},
    }
    return (
        json.dumps(submissions, separators=(",", ":"), sort_keys=True).encode(),
        json.dumps(companyfacts, separators=(",", ":"), sort_keys=True).encode(),
    )


def _pipeline(
    storage: LocalStorage,
    *,
    fail_diagnostic: bool = False,
    configuration: SecAssetConfiguration = _CONFIGURATION,
    valuation_service: _ValuationService | None = None,
    valuation_pipeline: _ValuationPipeline | None = None,
) -> tuple[SecIssuerFundamentalRefreshPipeline, _FixtureTransport]:
    submissions, companyfacts = _documents()
    transport = _FixtureTransport(submissions, companyfacts)
    client = SecEdgarClient(
        transport,
        SecEdgarIdentity("Investment Analyst integration@example.com"),
        cik=_CONFIGURATION.cik,
        ticker=_CONFIGURATION.ticker,
        sleep=lambda _: None,
        clock=lambda: _FETCHED_AT,
    )
    point_in_time = SecIssuerFundamentalPointInTimeService(
        storage,
        _CONFIGURATION,
    )
    diagnostic = (
        _FailingDiagnosticPipeline()
        if fail_diagnostic
        else SecIssuerFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage, _CONFIGURATION),
            SecFundamentalDiagnosticEngine(_CONFIGURATION),
            configuration=_CONFIGURATION,
            clock=lambda: _COMPUTED_AT,
        )
    )
    return (
        SecIssuerFundamentalRefreshPipeline(
            storage,
            configuration=configuration,
            fetch_pipeline=SecIssuerFundamentalsPipeline(
                storage,
                client,
                configuration=_CONFIGURATION,
            ),
            observation_pipeline=SecIssuerObservationPipeline(
                storage,
                SecCompanyFactsNormalizer(_CONFIGURATION),
                configuration=_CONFIGURATION,
                clock=lambda: _NORMALIZED_AT,
            ),
            metric_pipeline=SecIssuerFundamentalMetricPipeline(
                storage,
                point_in_time,
                SecFundamentalMetricEngine(_CONFIGURATION),
                configuration=_CONFIGURATION,
                clock=lambda: _COMPUTED_AT,
            ),
            diagnostic_pipeline=diagnostic,
            valuation_service=valuation_service,
            valuation_pipeline=valuation_pipeline,
            clock=lambda: _KNOWN_AT,
        ),
        transport,
    )


def _request(
    *,
    known_at: datetime | None = _KNOWN_AT,
) -> SecIssuerFundamentalRefreshRequest:
    return SecIssuerFundamentalRefreshRequest(
        asset_id=_CONFIGURATION.asset_id,
        frequency=DataFrequency.ANNUAL,
        requested_known_at=known_at,
    )


def test_refresh_isolated_idempotent_and_fully_traceable(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first_pipeline, first_transport = _pipeline(storage)
        first = first_pipeline.run(_request())
        second_pipeline, second_transport = _pipeline(storage)
        second = second_pipeline.run(_request())

        assert first.schema_version == "sec-issuer-fundamental-refresh-v1"
        assert first.asset_id == _CONFIGURATION.asset_id
        assert first.source_id == _CONFIGURATION.companyfacts_source_id
        assert first.documents_received == 2
        assert first.raw_records_created == 2
        assert first.observations_created == first.observations_generated == 10
        assert first.annual_observations == first.quarterly_observations == 5
        assert first.metric_results_created == sum(first.metric_counts.values()) == 3
        assert first.diagnostics_created == 1
        assert first.diagnostic_target_period_end == datetime(
            2025,
            12,
            27,
            tzinfo=UTC,
        )
        assert first.traceability_verified

        assert second.raw_records_created == 0
        assert second.raw_records_reused == 2
        assert second.observations_created == 0
        assert second.observations_reused == 10
        assert second.metric_results_created == 0
        assert second.metric_results_reused == 3
        assert second.diagnostics_created == 0
        assert second.diagnostics_reused == 1
        assert len(first_transport.calls) == len(second_transport.calls) == 2
        assert {
            item.asset_id for item in storage.observations.list(asset_id=_CONFIGURATION.asset_id)
        } == {_CONFIGURATION.asset_id}
        assert {
            item.asset_id for item in storage.metric_results.list(asset_id=_CONFIGURATION.asset_id)
        } == {_CONFIGURATION.asset_id}
        assert {
            item.asset_id for item in storage.diagnostics.list(asset_id=_CONFIGURATION.asset_id)
        } == {_CONFIGURATION.asset_id}


def test_late_stage_failure_preserves_fetch_normalization_and_metrics(
    tmp_path: Path,
) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline, _ = _pipeline(storage, fail_diagnostic=True)

        with pytest.raises(SecIssuerFundamentalRefreshStageError) as captured:
            pipeline.run(_request())

        assert captured.value.stage is (SecIssuerFundamentalRefreshStage.FUNDAMENTAL_DIAGNOSTIC)
        assert len(storage.raw_records.list()) == 2
        assert len(storage.observations.list(asset_id=_CONFIGURATION.asset_id)) == 10
        assert len(storage.metric_results.list(asset_id=_CONFIGURATION.asset_id)) == 3
        assert storage.diagnostics.list(asset_id=_CONFIGURATION.asset_id) == []


def test_historical_cut_too_early_preserves_ingested_evidence_only(
    tmp_path: Path,
) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline, _ = _pipeline(storage)

        with pytest.raises(SecIssuerFundamentalKnownAtTooEarlyError) as captured:
            pipeline.run(_request(known_at=datetime(2025, 1, 1, tzinfo=UTC)))

        assert captured.value.minimum_known_at == datetime(
            2026,
            2,
            4,
            21,
            tzinfo=UTC,
        )
        assert len(storage.raw_records.list()) == 2
        assert len(storage.observations.list(asset_id=_CONFIGURATION.asset_id)) == 10
        assert storage.metric_results.list(asset_id=_CONFIGURATION.asset_id) == []
        assert storage.diagnostics.list(asset_id=_CONFIGURATION.asset_id) == []


def test_unsupported_ifrs_quarterly_refresh_fails_before_provider_or_storage(
    tmp_path: Path,
) -> None:
    ifrs_configuration = _CONFIGURATION.model_copy(
        update={"accounting_standard": SecAccountingStandard.IFRS}
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline, transport = _pipeline(storage, configuration=ifrs_configuration)
        request = SecIssuerFundamentalRefreshRequest(
            asset_id=ifrs_configuration.asset_id,
            frequency=DataFrequency.QUARTERLY,
            requested_known_at=_KNOWN_AT,
        )

        with pytest.raises(
            RuntimeError,
            match="AMD SEC fundamentals support only: annual",
        ):
            pipeline.run(request)

        assert transport.calls == []
        assert storage.raw_records.list() == []
        assert storage.observations.list(asset_id=ifrs_configuration.asset_id) == []


def test_sec_refresh_materializes_local_valuation_after_the_same_provider_fetch(
    tmp_path: Path,
) -> None:
    valuation_service = _ValuationService()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline, transport = _pipeline(
            storage,
            valuation_service=valuation_service,
            valuation_pipeline=_ValuationPipeline(),
        )

        summary = pipeline.run(_request())

    assert len(transport.calls) == 2
    assert len(valuation_service.requests) == 1
    assert valuation_service.requests[0].known_at == _KNOWN_AT
    assert summary.valuation_metric_results_created == 6
    assert summary.valuation_metrics_not_evaluable == 5
    assert summary.metric_results_created == sum(summary.metric_counts.values()) + 6
