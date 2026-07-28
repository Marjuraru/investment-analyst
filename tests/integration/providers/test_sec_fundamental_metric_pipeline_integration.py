"""Integration tests for persisted SEC issuer fundamental metrics."""

import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid5

from investment_analyst.core.models import (
    AssetClass,
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    sec_transformation_version,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    SEC_FACT_DEFINITIONS,
)
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricRequest,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecAaplFundamentalMetricPipeline,
    SecIssuerFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
    SecIssuerFundamentalPointInTimeService,
)
from investment_analyst.providers.fundamentals.sec_raw_records import (
    aapl_sec_configuration,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_OBSERVATION_NAMESPACE = UUID("2d55a2cc-cae2-4a55-b2ba-c2422d142884")
_TAGS = {item.field_name: item.tag for item in SEC_FACT_DEFINITIONS}


def _save_annual_period(
    storage: LocalStorage,
    *,
    fiscal_year: int,
    period_end: datetime,
    values: dict[str, str],
    acceptance_at: datetime,
    configuration: SecAssetConfiguration | None = None,
) -> None:
    resolved = configuration or aapl_sec_configuration()
    identity_prefix = (
        "" if resolved.asset_id == aapl_sec_configuration().asset_id else (f"{resolved.asset_id}:")
    )
    for field_name, text_value in values.items():
        raw_record_id = uuid5(
            _OBSERVATION_NAMESPACE,
            f"{identity_prefix}raw:{fiscal_year}:{field_name}:{text_value}",
        )
        observation_id = uuid5(
            _OBSERVATION_NAMESPACE,
            f"{identity_prefix}observation:{fiscal_year}:{field_name}:{text_value}",
        )
        accession = f"{resolved.cik}-{str(fiscal_year)[-2:]}-000001"
        record_key = json.dumps(
            {
                "accession_number": accession,
                "taxonomy": "us-gaap",
                "tag": _TAGS[field_name],
                "unit": "USD",
                "period": period_end.date().isoformat(),
                "form": "10-K",
                "fiscal_year": str(fiscal_year),
                "fiscal_period": "FY",
                "companyfacts_record_id": str(raw_record_id),
                "submissions_record_id": str(
                    uuid5(
                        _OBSERVATION_NAMESPACE,
                        f"{identity_prefix}submissions:{fiscal_year}",
                    )
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        duration = field_name in {
            "fundamental.revenue",
            "fundamental.net_income",
        }
        storage.observations.save(
            NormalizedObservation(
                observation_id=observation_id,
                raw_record_id=raw_record_id,
                asset_id=resolved.asset_id,
                field_name=field_name,
                value=Decimal(text_value),
                unit="USD",
                frequency=DataFrequency.ANNUAL,
                observed_at=period_end,
                period_start=(period_end.replace(year=period_end.year - 1) if duration else None),
                period_end=period_end,
                available_at=acceptance_at,
                normalized_at=datetime(2027, 1, 1, tzinfo=UTC),
                source=SourceReference(
                    source_id=resolved.companyfacts_source_id,
                    record_key=record_key,
                    retrieved_at=datetime(2026, 12, 1, tzinfo=UTC),
                    raw_uri="https://data.sec.gov/test",
                    checksum_sha256="a" * 64,
                ),
                quality=DataQuality.VALID,
                transformation_version=sec_transformation_version(resolved),
            )
        )


def _seed_history(
    storage: LocalStorage,
    configuration: SecAssetConfiguration | None = None,
) -> None:
    _save_annual_period(
        storage,
        fiscal_year=2024,
        period_end=datetime(2024, 9, 28, tzinfo=UTC),
        acceptance_at=datetime(2024, 11, 1, tzinfo=UTC),
        values={
            "fundamental.revenue": "100",
            "fundamental.net_income": "20",
            "fundamental.assets": "200",
            "fundamental.liabilities": "80",
            "fundamental.stockholders_equity": "120",
        },
        configuration=configuration,
    )
    _save_annual_period(
        storage,
        fiscal_year=2025,
        period_end=datetime(2025, 9, 27, tzinfo=UTC),
        acceptance_at=datetime(2025, 10, 31, tzinfo=UTC),
        values={
            "fundamental.revenue": "125",
            "fundamental.net_income": "30",
            "fundamental.assets": "250",
            "fundamental.liabilities": "100",
            "fundamental.stockholders_equity": "150",
        },
        configuration=configuration,
    )


class _CountingService:
    def __init__(self, service: SecAaplFundamentalPointInTimeService) -> None:
        self._service = service
        self.calls = 0

    def query(self, request):
        self.calls += 1
        return self._service.query(request)


def test_pipeline_persists_and_reuses_metrics_without_source_side_effects(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_history(storage)
        service = _CountingService(SecAaplFundamentalPointInTimeService(storage))
        pipeline = SecAaplFundamentalMetricPipeline(
            storage,
            service,
            SecFundamentalMetricEngine(),
            clock=lambda: datetime(2027, 1, 2, tzinfo=UTC),
        )
        request = SecFundamentalMetricRequest(
            known_at=datetime(2026, 12, 31, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
        )
        observation_count = len(storage.observations.list())
        raw_count = storage.store.connection.execute(
            "SELECT COUNT(*) FROM raw_record_index"
        ).fetchone()[0]

        first = pipeline.run(request)
        first_results = storage.metric_results.list(asset_id="equity:us:aapl")
        second = pipeline.run(request)
        second_results = storage.metric_results.list(asset_id="equity:us:aapl")

        assert service.calls == 2
        assert first.metrics_generated == 8
        assert first.metrics_created == 8
        assert first.metrics_reused == 0
        assert second.metrics_created == 0
        assert second.metrics_reused == 8
        assert {item.result_id for item in first_results} == {
            item.result_id for item in second_results
        }
        assert len(storage.observations.list()) == observation_count
        assert (
            storage.store.connection.execute("SELECT COUNT(*) FROM raw_record_index").fetchone()[0]
            == raw_count
        )
        assert storage.diagnostics.list() == []
        assert first.to_json_dict()["traceability_verified"] is True


def test_other_known_at_with_same_inputs_reuses_metric_identity(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_history(storage)
        service = SecAaplFundamentalPointInTimeService(storage)
        pipeline = SecAaplFundamentalMetricPipeline(
            storage,
            service,
            SecFundamentalMetricEngine(),
            clock=lambda: datetime(2027, 2, 1, tzinfo=UTC),
        )
        first = pipeline.run(
            SecFundamentalMetricRequest(
                known_at=datetime(2026, 12, 31, tzinfo=UTC),
                frequency=DataFrequency.ANNUAL,
            )
        )
        second = pipeline.run(
            SecFundamentalMetricRequest(
                known_at=datetime(2027, 1, 15, tzinfo=UTC),
                frequency=DataFrequency.ANNUAL,
            )
        )

        assert first.metrics_created == 8
        assert second.metrics_created == 0
        assert second.metrics_reused == 8


def test_revised_input_creates_only_affected_metric_versions(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_history(storage)
        pipeline = SecAaplFundamentalMetricPipeline(
            storage,
            SecAaplFundamentalPointInTimeService(storage),
            SecFundamentalMetricEngine(),
            clock=lambda: datetime(2027, 2, 1, tzinfo=UTC),
        )
        request = SecFundamentalMetricRequest(
            known_at=datetime(2026, 12, 31, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
        )
        first = pipeline.run(request)
        _save_annual_period(
            storage,
            fiscal_year=2025,
            period_end=datetime(2025, 9, 27, tzinfo=UTC),
            acceptance_at=datetime(2026, 1, 15, tzinfo=UTC),
            values={"fundamental.revenue": "130"},
        )

        second = pipeline.run(request)

        assert first.metrics_created == 8
        assert second.metrics_created == 2
        assert second.metrics_reused == 6
        names = Counter(
            item.metric_key for item in storage.metric_results.list(asset_id="equity:us:aapl")
        )
        assert names["fundamental.net_margin"] == 3
        assert names["fundamental.revenue_yoy_growth"] == 2


def test_two_sec_issuers_persist_metrics_without_cross_asset_reuse(tmp_path) -> None:
    amd = SecAssetConfiguration(
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
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_history(storage)
        _seed_history(storage, amd)
        apple_pipeline = SecAaplFundamentalMetricPipeline(
            storage,
            SecAaplFundamentalPointInTimeService(storage),
            SecFundamentalMetricEngine(),
            clock=lambda: datetime(2027, 1, 2, tzinfo=UTC),
        )
        amd_pipeline = SecIssuerFundamentalMetricPipeline(
            storage,
            SecIssuerFundamentalPointInTimeService(storage, amd),
            SecFundamentalMetricEngine(amd),
            configuration=amd,
            clock=lambda: datetime(2027, 1, 2, tzinfo=UTC),
        )

        apple_summary = apple_pipeline.run(
            SecFundamentalMetricRequest(
                known_at=datetime(2026, 12, 31, tzinfo=UTC),
                frequency=DataFrequency.ANNUAL,
            )
        )
        amd_request = SecFundamentalMetricRequest(
            asset_id=amd.asset_id,
            known_at=datetime(2026, 12, 31, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
        )
        amd_first = amd_pipeline.run(amd_request)
        amd_second = amd_pipeline.run(amd_request)

        apple_results = storage.metric_results.list(asset_id="equity:us:aapl")
        amd_results = storage.metric_results.list(asset_id=amd.asset_id)
        assert apple_summary.metrics_created == 8
        assert amd_first.metrics_created == 8
        assert amd_second.metrics_created == 0
        assert amd_second.metrics_reused == 8
        assert len(apple_results) == len(amd_results) == 8
        assert {result.result_id for result in apple_results}.isdisjoint(
            {result.result_id for result in amd_results}
        )
        assert {result.parameters["source_id"] for result in amd_results} == {
            amd.companyfacts_source_id
        }
