"""End-to-end checks for bounded persistence across v1 and isolated v2."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import pytest

from investment_analyst.analytics.analysis_snapshot import build_analysis_snapshot
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
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
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_normalizer import SOURCE_ID as COINBASE_SOURCE_ID
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseHistoricalPipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_pipeline import (
    AlpacaHistoricalPipeline,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials, AlpacaStockClient
from investment_analyst.storage import (
    LocalStorage,
    RecordConflictError,
    StoragePaths,
)
from investment_analyst.storage.analysis_snapshot_v2 import AnalysisSnapshotV2Error
from investment_analyst.storage.analytical_v2_validation import (
    AnalyticalV2ValidationContext,
    AnalyticalV2ValidationError,
)
from investment_analyst.storage.bounded_insert import MAX_BOUNDED_INSERT_ROWS
from investment_analyst.storage.metric_v2 import MetricV2Error
from investment_analyst.storage.raw_v2 import RawV2Staging, RawV2StagingError
from investment_analyst.workspace.raw_v2_backup import RawV2StagingBackupService

_BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_ALPACA_FIXTURE = Path("tests/fixtures/alpaca/aapl_daily.json")
_COINBASE_FIXTURE = Path("tests/fixtures/coinbase/btc_usd_daily.json")


class _FixtureTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        return HttpResponse(status_code=200, body=self.body, headers={}, url=url)


class _BoundedWriteRecorder:
    """Observe only bounded INSERT batches while delegating to DuckDB."""

    _INSERT_TABLE = re.compile(r"INSERT INTO ([a-z0-9_]+)", re.IGNORECASE)

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self.connection = connection
        self.batches: list[tuple[str, int, int]] = []

    def execute(self, query: str, parameters: object = None):
        if "WITH source_rows" in query:
            match = self._INSERT_TABLE.search(query)
            assert match is not None
            assert isinstance(parameters, list) and len(parameters) == 1
            decoded = json.loads(str(parameters[0]))
            self.batches.append((match.group(1), len(decoded), len(parameters)))
        if parameters is None:
            return self.connection.execute(query)
        return self.connection.execute(query, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


class _QueryRecorder:
    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self.connection = connection
        self.queries: list[str] = []

    def execute(self, query: str, parameters: object = None):
        self.queries.append(query)
        if parameters is None:
            return self.connection.execute(query)
        return self.connection.execute(query, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


def _staging(root: Path, name: str) -> RawV2Staging:
    destination = (root / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _raw_record(
    *,
    asset_id: str = "equity:us:aapl",
    source_id: str = "test:bounded-market",
    moment: datetime = _BASE,
    record_key: str = "bounded-input",
    value: str = "210.5000",
) -> RawRecord:
    source = SourceReference(
        source_id=source_id,
        record_key=record_key,
        retrieved_at=moment,
        raw_uri="scratch://bounded-input",
        checksum_sha256="a" * 64,
    )
    return RawRecord(
        record_id=uuid4(),
        asset_id=asset_id,
        source=source,
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload={"value": value, "message": 'línea "exacta"'},
        schema_version="bounded-persistence-test-v1",
    )


def _observation(
    record: RawRecord,
    *,
    field_name: str = "close",
    value: Decimal = Decimal("210.5000"),
    available_at: datetime | None = None,
    normalized_at: datetime | None = None,
) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=record.record_id,
        asset_id=record.asset_id,
        field_name=field_name,
        value=value,
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=record.event_time,
        available_at=available_at or record.available_at,
        normalized_at=normalized_at or record.received_at,
        source=record.source,
        quality=DataQuality.VALID,
        transformation_version="bounded-persistence-v1",
    )


def _metric(
    observation_ids: Sequence[UUID],
    index: int,
    moment: datetime,
    *,
    asset_id: str = "equity:us:aapl",
    dependencies: Sequence[UUID] = (),
) -> MetricResult:
    candidate = MetricResult(
        result_id=uuid4(),
        asset_id=asset_id,
        metric_key=f"market.close.bounded.{index}",
        value=Decimal(index).quantize(Decimal("0.01")),
        unit="USD",
        as_of=moment,
        available_at=moment,
        computed_at=moment + timedelta(hours=1),
        parameters={"ordinal": index},
        input_observation_ids=list(observation_ids),
        input_metric_result_ids=list(dependencies),
        algorithm_version="bounded-persistence-test-v1",
        quality=DataQuality.VALID,
    )
    return candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(candidate)})


def _diagnostic(metric: MetricResult, index: int, moment: datetime) -> DiagnosticResult:
    return DiagnosticResult(
        diagnostic_id=uuid4(),
        asset_id=metric.asset_id,
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("50.00"),
        confidence=Decimal("0.80"),
        as_of=moment,
        available_at=moment,
        computed_at=moment + timedelta(hours=2),
        components=[
            DiagnosticComponent(
                component_key="bounded-close",
                score=Decimal("50.00"),
                weight=Decimal("1.00"),
                weighted_contribution=Decimal("50.00"),
                metric_result_ids=[metric.result_id],
                explanation=f"métrica {index}",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("50.00"),
                reason=f"evidencia {index}",
            )
        ],
        algorithm_version="bounded-persistence-test-v1",
        summary=f"Diagnóstico {index}",
        quality=DataQuality.VALID,
    )


def _snapshot(metric: MetricResult, diagnostic: DiagnosticResult, moment: datetime):
    return build_analysis_snapshot(
        asset_id=metric.asset_id,
        domain="market",
        known_at=moment,
        policy_version="bounded-persistence-test-v1",
        metric_ids=[metric.result_id],
        diagnostic_ids=[diagnostic.diagnostic_id],
        evidence_set_hashes=[],
        created_at=moment + timedelta(hours=3),
    )


def _v2_bundle(
    staging: RawV2Staging,
    *,
    count: int,
    dependency_chain: bool = False,
    persist_metrics: bool = True,
) -> tuple[list[MetricResult], list[DiagnosticResult], list[object]]:
    record = _raw_record()
    staging.save(record)
    observation = _observation(record)
    staging.save_observations([observation])
    metrics: list[MetricResult] = []
    for index in range(count):
        moment = _BASE + timedelta(minutes=index)
        dependencies = [metrics[-1].result_id] if dependency_chain and metrics else []
        metrics.append(
            _metric([observation.observation_id], index, moment, dependencies=dependencies)
        )
    if persist_metrics:
        receipt = staging.save_metrics(metrics)
        assert receipt.created_count == count
    diagnostics = [
        _diagnostic(metric, index, _BASE + timedelta(minutes=index))
        for index, metric in enumerate(metrics)
    ]
    snapshots = [
        _snapshot(metric, diagnostic, _BASE + timedelta(minutes=index))
        for index, (metric, diagnostic) in enumerate(zip(metrics, diagnostics, strict=True))
    ]
    return metrics, diagnostics, snapshots


def test_market_ingestion_matches_reference_and_preserves_progress(tmp_path: Path) -> None:
    start = datetime(2026, 7, 7, tzinfo=UTC)
    end = datetime(2026, 7, 12, tzinfo=UTC)
    first_retrieved_at = datetime(2026, 7, 12, 12, tzinfo=UTC)
    later_retrieved_at = first_retrieved_at + timedelta(days=1)
    normalized_at = first_retrieved_at + timedelta(minutes=1)

    with LocalStorage(StoragePaths.from_root(tmp_path / "v1-market")) as storage:
        alpaca_body = _ALPACA_FIXTURE.read_bytes()
        first_alpaca = AlpacaHistoricalPipeline(
            storage,
            AlpacaStockClient(
                _FixtureTransport(alpaca_body),
                AlpacaCredentials(api_key="offline-key", secret_key="offline-secret"),
                clock=lambda: first_retrieved_at,
            ),
            clock=lambda: normalized_at,
        ).run(start, end)
        first_alpaca_observations = storage.observations.list(asset_id="equity:us:aapl")
        first_alpaca_records = [
            row
            for row in storage.raw_records.list(
                source_id="alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
            )
            if row.schema_version != "alpaca-market-fetch-receipt-v1"
        ]

        second_alpaca = AlpacaHistoricalPipeline(
            storage,
            AlpacaStockClient(
                _FixtureTransport(alpaca_body),
                AlpacaCredentials(api_key="offline-key", secret_key="offline-secret"),
                clock=lambda: later_retrieved_at,
            ),
            clock=lambda: later_retrieved_at + timedelta(minutes=1),
        ).run(start, end)
        second_alpaca_observations = storage.observations.list(asset_id="equity:us:aapl")
        assert second_alpaca.raw_records_created == 0
        assert second_alpaca.raw_records_reused == first_alpaca.bars_received
        assert second_alpaca.observations_created == 0
        assert second_alpaca.observations_reused == len(first_alpaca_observations)
        assert second_alpaca_observations == first_alpaca_observations
        assert all(
            item.source.retrieved_at == first_retrieved_at for item in second_alpaca_observations
        )
        assert all(item.source.retrieved_at == first_retrieved_at for item in first_alpaca_records)

        coinbase_body = _COINBASE_FIXTURE.read_bytes()
        first_coinbase = CoinbaseHistoricalPipeline(
            storage,
            CoinbaseExchangeClient(
                _FixtureTransport(coinbase_body),
                sleep=lambda _: None,
                clock=lambda: first_retrieved_at,
            ),
            clock=lambda: normalized_at,
        ).run(start, end)
        first_coinbase_observations = storage.observations.list(
            asset_id="crypto:btc-usd", source_id=COINBASE_SOURCE_ID
        )
        first_coinbase_records = storage.raw_records.list(source_id=COINBASE_SOURCE_ID)

        second_coinbase = CoinbaseHistoricalPipeline(
            storage,
            CoinbaseExchangeClient(
                _FixtureTransport(coinbase_body),
                sleep=lambda _: None,
                clock=lambda: later_retrieved_at,
            ),
            clock=lambda: later_retrieved_at + timedelta(minutes=1),
        ).run(start, end)
        second_coinbase_observations = storage.observations.list(
            asset_id="crypto:btc-usd", source_id=COINBASE_SOURCE_ID
        )

        assert first_alpaca.raw_records_created == first_alpaca.bars_received
        assert first_alpaca.observations_created == 7 * first_alpaca.bars_received
        assert first_coinbase.raw_records_created == first_coinbase.candles_received
        assert first_coinbase.observations_created == 5 * first_coinbase.candles_received
        assert second_coinbase.raw_records_created == 0
        assert second_coinbase.raw_records_reused == first_coinbase.candles_received
        assert second_coinbase.observations_created == 0
        assert second_coinbase.observations_reused == len(first_coinbase_observations)
        assert second_coinbase_observations == first_coinbase_observations
        assert all(
            item.source.retrieved_at == first_retrieved_at for item in second_coinbase_observations
        )
        assert all(
            item.source.retrieved_at == first_retrieved_at for item in first_coinbase_records
        )

        # Compare the first persisted rows and their retry to the same canonical
        # model documents; both paths retain the original provider retrieval time.
        assert {item.observation_id: item for item in second_alpaca_observations} == {
            item.observation_id: item for item in first_alpaca_observations
        }
        assert {item.observation_id: item for item in second_coinbase_observations} == {
            item.observation_id: item for item in first_coinbase_observations
        }


def test_v2_batched_writes_match_reference_at_chunk_boundaries(tmp_path: Path) -> None:
    staging = _staging(tmp_path, "v2-batched-writes")
    with staging:
        metrics, diagnostics, snapshots = _v2_bundle(staging, count=513)
        staging.save_diagnostics(diagnostics)
        staging.save_analysis_snapshots(snapshots)
        recorder = _BoundedWriteRecorder(staging._connection)
        staging._connection = recorder  # type: ignore[assignment]

        empty_metrics = staging.save_metrics([])
        empty_diagnostics = staging.save_diagnostics([])
        empty_snapshots = staging.save_analysis_snapshots([])
        assert empty_metrics.total_count == 0
        assert empty_diagnostics.total_count == 0
        assert empty_snapshots.total_count == 0
        assert recorder.batches == []

        metric_receipt = staging.save_metrics(metrics)
        diagnostic_receipt = staging.save_diagnostics(diagnostics)
        snapshot_receipt = staging.save_analysis_snapshots(snapshots)
        assert metric_receipt.created_count == 0
        assert diagnostic_receipt.created_count == 0
        assert snapshot_receipt.created_count == 0

        # A fresh second corpus is required for insert-side measurements; this
        # staging already contains the reference data and proves idempotent reads.
        assert staging.get_metrics([item.result_id for item in metrics]) == {
            item.result_id: item for item in metrics
        }
        assert staging.get_diagnostics([item.diagnostic_id for item in diagnostics]) == {
            item.diagnostic_id: item for item in diagnostics
        }
        assert staging.get_analysis_snapshots([item.snapshot_id for item in snapshots]) == {
            item.snapshot_id: item for item in snapshots
        }

    fresh = _staging(tmp_path, "v2-batched-write-cost")
    with fresh:
        metrics, diagnostics, snapshots = _v2_bundle(fresh, count=513, persist_metrics=False)
        recorder = _BoundedWriteRecorder(fresh._connection)
        fresh._connection = recorder  # type: ignore[assignment]
        assert fresh.save_metrics(metrics).created_count == 513
        assert fresh.save_diagnostics(diagnostics).created_count == 513
        assert fresh.save_analysis_snapshots(snapshots).created_count == 513

        counts = Counter(table for table, _, _ in recorder.batches)
        rows_by_table: Counter[str] = Counter()
        for table, row_count, parameter_count in recorder.batches:
            assert 0 < row_count <= MAX_BOUNDED_INSERT_ROWS
            assert parameter_count == 1
            rows_by_table[table] += row_count
        assert counts == Counter(
            {
                "metric_results_v2": 3,
                "metric_v2_observation_links": 3,
                "diagnostic_results_v2": 3,
                "diagnostic_v2_components": 3,
                "diagnostic_v2_component_metric_links": 3,
                "diagnostic_v2_evidence": 3,
                "analysis_snapshots_v2": 3,
                "analysis_snapshot_v2_metric_links": 3,
                "analysis_snapshot_v2_diagnostic_links": 3,
            }
        )
        assert rows_by_table == Counter(
            {
                "metric_results_v2": 513,
                "metric_v2_observation_links": 513,
                "diagnostic_results_v2": 513,
                "diagnostic_v2_components": 513,
                "diagnostic_v2_component_metric_links": 513,
                "diagnostic_v2_evidence": 513,
                "analysis_snapshots_v2": 513,
                "analysis_snapshot_v2_metric_links": 513,
                "analysis_snapshot_v2_diagnostic_links": 513,
            }
        )
        assert fresh.get_metrics([item.result_id for item in metrics]) == {
            item.result_id: item for item in metrics
        }
        assert fresh.get_diagnostics([item.diagnostic_id for item in diagnostics]) == {
            item.diagnostic_id: item for item in diagnostics
        }
        assert fresh.get_analysis_snapshots([item.snapshot_id for item in snapshots]) == {
            item.snapshot_id: item for item in snapshots
        }


def test_validation_work_scales_with_reachable_graph_and_expires_per_operation(
    tmp_path: Path,
) -> None:
    staging = _staging(tmp_path, "v2-validation-context")
    with staging:
        metrics, _, _ = _v2_bundle(staging, count=1024, dependency_chain=True)
        recorder = _QueryRecorder(staging._connection)
        staging._connection = recorder  # type: ignore[assignment]
        context = AnalyticalV2ValidationContext()

        resolved: dict[UUID, MetricResult] = {}
        for start in range(0, len(metrics), 256):
            page = metrics[start : start + 256]
            resolved.update(
                context.resolve_metrics(staging._connection, [m.result_id for m in page])
            )

        assert len(resolved) == 1024
        assert len(context.metrics_by_id) == 1024
        assert len(context.observations_by_id) == 1
        assert len([query for query in recorder.queries if "metric_results_v2" in query]) <= 20
        query_count_before_reuse = len(recorder.queries)
        assert context.resolve_metrics(
            staging._connection, [item.result_id for item in metrics]
        ) == {item.result_id: item for item in metrics}
        assert len(recorder.queries) == query_count_before_reuse

        # A new operation must re-read and detect a mutation that the completed
        # operation's context correctly no longer owns.
        staging._connection.execute(
            "UPDATE metric_results_v2 SET algorithm_version = ? WHERE result_id = ?",
            ["tampered-algorithm", str(metrics[0].result_id)],
        )
        fresh_context = AnalyticalV2ValidationContext()
        with pytest.raises(MetricV2Error, match="identity"):
            fresh_context.resolve_metrics(staging._connection, [metrics[-1].result_id])


def test_conflict_corruption_future_and_foreign_references_fail_closed(
    tmp_path: Path,
) -> None:
    staging = _staging(tmp_path, "v2-negative-cases")
    with staging:
        record = _raw_record()
        staging.save(record)
        observation = _observation(record)
        staging.save_observations([observation])
        metric = _metric([observation.observation_id], 0, _BASE)
        staging.save_metrics([metric])

        with pytest.raises(RecordConflictError):
            staging.save(record.model_copy(update={"payload": {"value": "different"}}))
        with pytest.raises(RecordConflictError):
            staging.save_observations(
                [observation, observation.model_copy(update={"value": Decimal("999.00")})]
            )
        with pytest.raises(RecordConflictError, match="different content"):
            staging.save_metrics([metric.model_copy(update={"value": Decimal("999.00")})])

        future_moment = _BASE + timedelta(days=2)
        future_record = _raw_record(moment=future_moment, record_key="future")
        staging.save(future_record)
        future_observation = _observation(future_record, available_at=future_moment)
        staging.save_observations([future_observation])
        future_metric = _metric(
            [future_observation.observation_id],
            1,
            _BASE + timedelta(days=1),
        )
        with pytest.raises(MetricV2Error, match="future observation"):
            staging.save_metrics([future_metric])

        foreign_record = _raw_record(
            asset_id="crypto:btc-usd",
            source_id="test:foreign",
            record_key="foreign",
        )
        staging.save(foreign_record)
        foreign_observation = _observation(foreign_record)
        staging.save_observations([foreign_observation])
        foreign_metric = _metric(
            [foreign_observation.observation_id],
            2,
            _BASE,
            asset_id="equity:us:aapl",
        )
        with pytest.raises(MetricV2Error, match="foreign observation"):
            staging.save_metrics([foreign_metric])

        missing_metric = _metric([uuid4()], 3, _BASE)
        with pytest.raises(MetricV2Error, match="missing observation"):
            staging.save_metrics([missing_metric])

        diagnostic = _diagnostic(metric, 0, _BASE)
        staging.save_diagnostics([diagnostic])
        snapshot = _snapshot(metric, diagnostic, _BASE)
        staging.save_analysis_snapshots([snapshot])
        staging._connection.execute(
            "UPDATE metric_v2_observation_links SET position = 2 WHERE result_id = ?",
            [str(metric.result_id)],
        )
        with pytest.raises(AnalyticalV2ValidationError, match="positions"):
            staging.get_metrics([metric.result_id])
        staging._connection.execute(
            "UPDATE metric_v2_observation_links SET position = 0 WHERE result_id = ?",
            [str(metric.result_id)],
        )

        staging._connection.execute(
            "UPDATE analysis_snapshots_v2 SET evidence_set_digest = ? WHERE snapshot_id = ?",
            ["0" * 64, str(snapshot.snapshot_id)],
        )
        with pytest.raises(AnalysisSnapshotV2Error):
            staging.get_analysis_snapshot(snapshot.snapshot_id)

        relative_path, checksum = staging._connection.execute(
            "SELECT relative_path, checksum_sha256 FROM raw_v2_index WHERE record_id = ?",
            [str(record.record_id)],
        ).fetchone()
        blob_path = staging._raw_root / str(relative_path)
        blob_path.write_bytes(b"corrupt payload")
        with pytest.raises(RawV2StagingError, match="checksum"):
            staging.get(record.record_id)
        assert len(str(checksum)) == 64


def test_v1_v2_reopen_and_restore_preserve_identity_and_pit(tmp_path: Path) -> None:
    v1_root = tmp_path / "v1-reopen"
    raw_v1 = _raw_record()
    observation_v1 = _observation(raw_v1)
    with LocalStorage(StoragePaths.from_root(v1_root)) as storage:
        raw_receipt = storage.raw_records.save_many([raw_v1])
        observation_receipt = storage.observations.save_many([observation_v1])
        assert raw_receipt.created_count == 1
        assert observation_receipt.created_count == 1
    with LocalStorage(StoragePaths.from_root(v1_root)) as reopened:
        assert reopened.raw_records.get(raw_v1.record_id) == raw_v1
        assert reopened.observations.get(observation_v1.observation_id) == observation_v1

    staging = _staging(tmp_path, "v2-backup-source")
    service = RawV2StagingBackupService()
    backup_path = tmp_path / "v2-backup"
    with staging:
        raw_v2 = _raw_record(record_key="v2-backup")
        staging.save(raw_v2)
        observation_v2 = _observation(raw_v2)
        staging.save_observations([observation_v2])
        metric = _metric([observation_v2.observation_id], 4, _BASE)
        diagnostic = _diagnostic(metric, 4, _BASE)
        snapshot = _snapshot(metric, diagnostic, _BASE)
        staging.save_metrics([metric])
        staging.save_diagnostics([diagnostic])
        staging.save_analysis_snapshots([snapshot])
        manifest = service.create(staging, staging._connection, backup_path)
        assert manifest.counts.records == 1

    restored_manifest = service.restore(backup_path, tmp_path / "v2-backup-restored")
    assert restored_manifest.backup_id == manifest.backup_id
    restored_path = tmp_path / "v2-backup-restored"
    restored_connection = duckdb.connect(str(restored_path / "raw-v2-index.duckdb"))
    restored = RawV2Staging(restored_path, restored_connection)
    with restored:
        assert restored.get(raw_v2.record_id) == raw_v2
        assert restored.get_observations([observation_v2.observation_id]) == {
            observation_v2.observation_id: observation_v2
        }
        assert restored.get_metrics([metric.result_id]) == {metric.result_id: metric}
        assert restored.get_diagnostics([diagnostic.diagnostic_id]) == {
            diagnostic.diagnostic_id: diagnostic
        }
        assert restored.get_analysis_snapshot(snapshot.snapshot_id) == snapshot
        assert metric.computed_at > snapshot.known_at
        assert restored.list_metrics(available_to=snapshot.known_at) == [metric]
    restored_connection.close()
