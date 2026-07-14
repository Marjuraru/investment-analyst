"""Integration tests for persisted Apple SEC fundamental metrics."""

import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid5

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    SEC_FACT_DEFINITIONS,
    TRANSFORMATION_VERSION,
)
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricRequest,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecAaplFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
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
) -> None:
    for field_name, text_value in values.items():
        raw_record_id = uuid5(
            _OBSERVATION_NAMESPACE,
            f"raw:{fiscal_year}:{field_name}:{text_value}",
        )
        observation_id = uuid5(
            _OBSERVATION_NAMESPACE,
            f"observation:{fiscal_year}:{field_name}:{text_value}",
        )
        accession = f"0000320193-{str(fiscal_year)[-2:]}-000001"
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
                    uuid5(_OBSERVATION_NAMESPACE, f"submissions:{fiscal_year}")
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
                asset_id="equity:us:aapl",
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
                    source_id="sec-edgar:aapl:companyfacts",
                    record_key=record_key,
                    retrieved_at=datetime(2026, 12, 1, tzinfo=UTC),
                    raw_uri="https://data.sec.gov/test",
                    checksum_sha256="a" * 64,
                ),
                quality=DataQuality.VALID,
                transformation_version=TRANSFORMATION_VERSION,
            )
        )


def _seed_history(storage: LocalStorage) -> None:
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
