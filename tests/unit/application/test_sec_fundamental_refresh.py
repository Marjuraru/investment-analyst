"""Unit tests for strict SEC issuer refresh contracts."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshSummary,
)
from investment_analyst.core.models import DataFrequency, DiagnosticVerdict

_KNOWN_AT = datetime(2026, 7, 28, 15, tzinfo=UTC)
_FETCHED_AT = datetime(2026, 7, 28, 14, 55, tzinfo=UTC)
_NORMALIZED_AT = datetime(2026, 7, 28, 14, 56, tzinfo=UTC)
_PERIOD_END = datetime(2025, 12, 27, tzinfo=UTC)


def _request() -> SecIssuerFundamentalRefreshRequest:
    return SecIssuerFundamentalRefreshRequest(
        asset_id="equity:us:amd",
        frequency=DataFrequency.ANNUAL,
        requested_known_at=_KNOWN_AT,
    )


def _summary(**updates: object) -> SecIssuerFundamentalRefreshSummary:
    values: dict[str, object] = {
        "asset_id": "equity:us:amd",
        "source_id": "sec-edgar:amd:companyfacts",
        "request": _request(),
        "effective_known_at": _KNOWN_AT,
        "fetched_at": _FETCHED_AT,
        "normalized_at": _NORMALIZED_AT,
        "documents_received": 2,
        "raw_records_created": 1,
        "raw_records_reused": 1,
        "facts_examined": 5,
        "facts_selected": 2,
        "observations_generated": 2,
        "observations_created": 1,
        "observations_reused": 1,
        "annual_observations": 2,
        "quarterly_observations": 0,
        "observation_field_counts": {
            "fundamental.net_income": 1,
            "fundamental.revenue": 1,
        },
        "observation_skipped_counts": {"missing_tag": 3},
        "target_periods": 1,
        "metric_results_created": 0,
        "metric_results_reused": 1,
        "metric_counts": {"fundamental.net_margin": 1},
        "metric_skipped_counts": {},
        "diagnostic_target_period_end": _PERIOD_END,
        "diagnostic_verdict": DiagnosticVerdict.INSUFFICIENT_DATA,
        "diagnostic_coverage": Decimal("0.3"),
        "diagnostic_missing_requirements": ("fundamental.liabilities_to_assets",),
        "diagnostics_created": 0,
        "diagnostics_reused": 1,
        "traceability_verified": True,
    }
    values.update(updates)
    return SecIssuerFundamentalRefreshSummary(**values)


def test_summary_preserves_exact_decimal_and_explicit_context() -> None:
    summary = _summary()
    payload = summary.to_json_dict()

    assert payload["schema_version"] == "sec-issuer-fundamental-refresh-v1"
    assert payload["asset_id"] == "equity:us:amd"
    assert payload["diagnostic_coverage"] == "0.3"
    assert payload["request"]["requested_known_at"] == "2026-07-28T15:00:00Z"


def test_request_rejects_nonfundamental_frequency() -> None:
    with pytest.raises(ValidationError, match="annual or quarterly"):
        SecIssuerFundamentalRefreshRequest(
            asset_id="equity:us:amd",
            frequency=DataFrequency.DAY_1,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"asset_id": "equity:us:intc"},
        {"raw_records_reused": 0},
        {"observations_reused": 0},
        {"annual_observations": 1},
        {"metric_results_reused": 0},
        {"diagnostics_reused": 0},
        {"traceability_verified": False},
    ],
)
def test_summary_rejects_inconsistent_stage_counts(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _summary(**updates)


def test_summary_rejects_binary_float_coverage() -> None:
    with pytest.raises(ValidationError, match="must not use float"):
        _summary(diagnostic_coverage=0.3)


def test_resolve_known_at_direct_aggregate_and_too_early_error() -> None:
    from investment_analyst.application.sec_fundamental_refresh import (
        SecIssuerFundamentalKnownAtTooEarlyError,
        SecIssuerFundamentalRefreshPipeline,
    )
    from investment_analyst.providers.asset_config import SecAssetConfiguration
    from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
        sec_transformation_version,
    )

    class FakeObservations:
        def __init__(self, min_time: datetime | None) -> None:
            self.min_time = min_time
            self.calls: list[dict[str, object]] = []

        def minimum_available_at(self, **kwargs: object) -> datetime | None:
            self.calls.append(kwargs)
            return self.min_time

    class FakeStorage:
        def __init__(self, min_time: datetime | None) -> None:
            self.observations = FakeObservations(min_time)

        def require_open(self) -> None:
            pass

    config = SecAssetConfiguration(
        asset_id="equity:us:amd",
        cik="0000002488",
        ticker="AMD",
        submissions_source_id="sec-edgar:amd:submissions",
        companyfacts_source_id="sec-edgar:amd:companyfacts",
        name="Advanced Micro Devices, Inc.",
        asset_class="equity",  # type: ignore[arg-type]
        quote_currency="USD",
        exchange="NASDAQ",
    )
    min_time = datetime(2026, 2, 4, 21, tzinfo=UTC)
    storage = FakeStorage(min_time)
    pipeline = SecIssuerFundamentalRefreshPipeline(
        storage,  # type: ignore[arg-type]
        configuration=config,
        fetch_pipeline=None,  # type: ignore[arg-type]
        observation_pipeline=None,  # type: ignore[arg-type]
        metric_pipeline=None,  # type: ignore[arg-type]
        diagnostic_pipeline=None,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 7, 28, 15, tzinfo=UTC),
    )

    req_ok = SecIssuerFundamentalRefreshRequest(
        asset_id="equity:us:amd",
        frequency=DataFrequency.ANNUAL,
        requested_known_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    resolved = pipeline._resolve_known_at(req_ok)
    assert resolved == datetime(2026, 5, 1, tzinfo=UTC)
    assert len(storage.observations.calls) == 1
    assert storage.observations.calls[0]["asset_id"] == "equity:us:amd"
    assert storage.observations.calls[0]["transformation_version"] == sec_transformation_version(
        config
    )

    req_early = SecIssuerFundamentalRefreshRequest(
        asset_id="equity:us:amd",
        frequency=DataFrequency.ANNUAL,
        requested_known_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(SecIssuerFundamentalKnownAtTooEarlyError) as exc_info:
        pipeline._resolve_known_at(req_early)
    assert exc_info.value.minimum_known_at == min_time


def test_sec_fundamental_refresh_subprocess_memory_and_latency_gate(tmp_path) -> None:
    import json
    import subprocess
    import sys

    sub_script = f"""
import json
import resource
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalRefreshPipeline,
)
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
)
from investment_analyst.core.models import AssetClass, DataFrequency
from investment_analyst.providers.asset_config import SecAssetConfiguration
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

class OfflineTransport:
    def __init__(self, sub_b: bytes, facts_b: bytes):
        self._bodies = [sub_b, facts_b]
    def get(self, url, headers=None, timeout_seconds=10.0):
        return HttpResponse(200, self._bodies.pop(0), {{}}, url)

config = SecAssetConfiguration(
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

submissions_data = {{
    "cik": "0000002488",
    "name": "Advanced Micro Devices, Inc.",
    "tickers": ["AMD"],
    "exchanges": ["NASDAQ"],
    "filings": {{
        "recent": {{
            "accessionNumber": ["0000002488-26-000001", "0000002488-26-000002"],
            "filingDate": ["2026-02-04", "2026-04-29"],
            "reportDate": ["2025-12-27", "2026-03-28"],
            "acceptanceDateTime": ["2026-02-04T21:00:00Z", "2026-04-29T21:00:00Z"],
            "form": ["10-K", "10-Q"],
            "primaryDocument": ["amd-20251227.htm", "amd-20260328.htm"],
        }},
        "files": [],
    }},
}}
facts_data = {{
    "cik": "0000002488",
    "entityName": "Advanced Micro Devices, Inc.",
    "facts": {{
        "us-gaap": {{
            "RevenueFromContractWithCustomerExcludingAssessedTax": {{
                "units": {{"USD": [
                    {{
                        "start": "2024-12-29",
                        "end": "2025-12-27",
                        "val": "25000",
                        "accn": "0000002488-26-000001",
                        "fy": "2025",
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2026-02-04",
                    }},
                    {{
                        "start": "2025-12-28",
                        "end": "2026-03-28",
                        "val": "7000",
                        "accn": "0000002488-26-000002",
                        "fy": "2026",
                        "fp": "Q1",
                        "form": "10-Q",
                        "filed": "2026-04-29",
                    }},
                ]}}
            }},
            "NetIncomeLoss": {{
                "units": {{"USD": [
                    {{
                        "start": "2024-12-29",
                        "end": "2025-12-27",
                        "val": "4000",
                        "accn": "0000002488-26-000001",
                        "fy": "2025",
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2026-02-04",
                    }},
                    {{
                        "start": "2025-12-28",
                        "end": "2026-03-28",
                        "val": "1200",
                        "accn": "0000002488-26-000002",
                        "fy": "2026",
                        "fp": "Q1",
                        "form": "10-Q",
                        "filed": "2026-04-29",
                    }},
                ]}}
            }},
            "Assets": {{
                "units": {{"USD": [
                    {{
                        "end": "2025-12-27",
                        "val": "70000",
                        "accn": "0000002488-26-000001",
                        "fy": "2025",
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2026-02-04",
                    }},
                    {{
                        "end": "2026-03-28",
                        "val": "72000",
                        "accn": "0000002488-26-000002",
                        "fy": "2026",
                        "fp": "Q1",
                        "form": "10-Q",
                        "filed": "2026-04-29",
                    }},
                ]}}
            }},
            "Liabilities": {{
                "units": {{"USD": [
                    {{
                        "end": "2025-12-27",
                        "val": "25000",
                        "accn": "0000002488-26-000001",
                        "fy": "2025",
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2026-02-04",
                    }},
                    {{
                        "end": "2026-03-28",
                        "val": "26000",
                        "accn": "0000002488-26-000002",
                        "fy": "2026",
                        "fp": "Q1",
                        "form": "10-Q",
                        "filed": "2026-04-29",
                    }},
                ]}}
            }},
            "StockholdersEquity": {{
                "units": {{"USD": [
                    {{
                        "end": "2025-12-27",
                        "val": "45000",
                        "accn": "0000002488-26-000001",
                        "fy": "2025",
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2026-02-04",
                    }},
                    {{
                        "end": "2026-03-28",
                        "val": "46000",
                        "accn": "0000002488-26-000002",
                        "fy": "2026",
                        "fp": "Q1",
                        "form": "10-Q",
                        "filed": "2026-04-29",
                    }},
                ]}}
            }},
        }}
    }}
}}

sub_bytes = json.dumps(submissions_data).encode()
facts_bytes = json.dumps(facts_data).encode()

work_dir = Path("{tmp_path}") / "sub_ws"
with LocalStorage(StoragePaths.from_root(work_dir)) as storage:
    transport = OfflineTransport(sub_bytes, facts_bytes)
    client = SecEdgarClient(
        transport,
        SecEdgarIdentity("Investment Analyst test@example.com"),
        cik=config.cik,
        ticker=config.ticker,
        sleep=lambda _: None,
        clock=lambda: datetime(2026, 7, 28, 15, tzinfo=UTC),
    )
    pipeline = SecIssuerFundamentalRefreshPipeline(
        storage,
        configuration=config,
        fetch_pipeline=SecIssuerFundamentalsPipeline(storage, client, configuration=config),
        observation_pipeline=SecIssuerObservationPipeline(
            storage,
            SecCompanyFactsNormalizer(config),
            configuration=config,
            clock=lambda: datetime(2026, 7, 28, 15, 1, tzinfo=UTC),
        ),
        metric_pipeline=SecIssuerFundamentalMetricPipeline(
            storage,
            SecIssuerFundamentalPointInTimeService(storage, config),
            SecFundamentalMetricEngine(config),
            configuration=config,
            clock=lambda: datetime(2027, 1, 1, tzinfo=UTC),
        ),
        diagnostic_pipeline=SecIssuerFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage, config),
            SecFundamentalDiagnosticEngine(config),
            configuration=config,
            clock=lambda: datetime(2027, 1, 1, tzinfo=UTC),
        ),
        clock=lambda: datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
    )
    req = SecIssuerFundamentalRefreshRequest(
        asset_id=config.asset_id,
        frequency=DataFrequency.ANNUAL,
        requested_known_at=datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
    )

    rss_before_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    t0 = time.perf_counter()
    summary = pipeline.run(req)
    duration_sec = time.perf_counter() - t0
    current_heap, peak_heap = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

delta_rss_mb = (rss_after_kb - rss_before_kb) / 1024.0
peak_rss_mb = rss_after_kb / 1024.0
peak_heap_mb = peak_heap / (1024.0 * 1024.0)

telemetry = {{
    "delta_rss_mb": delta_rss_mb,
    "peak_rss_mb": peak_rss_mb,
    "peak_heap_mb": peak_heap_mb,
    "duration_seconds": duration_sec,
    "facts_selected": summary.facts_selected,
    "observations_created": summary.observations_created,
    "metric_results_created": summary.metric_results_created,
    "diagnostics_created": summary.diagnostics_created,
}}
print(json.dumps(telemetry))
"""
    res = subprocess.run(
        [sys.executable, "-c", sub_script],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(res.stdout.strip())
    assert data["facts_selected"] == 10
    assert data["observations_created"] == 10
    assert data["metric_results_created"] == 3
    assert data["diagnostics_created"] == 1
    assert data["delta_rss_mb"] <= 100.0
    assert data["duration_seconds"] < 1.0
