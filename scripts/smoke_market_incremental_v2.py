"""Run isolated correctness, recovery and scaling probes for daily market v2."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter_ns
from uuid import UUID

import duckdb

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.bar_schemas import ALPACA_SOURCE_ID
from investment_analyst.analytics.market.incremental_service import (
    IncrementalMarketReceipt,
    IncrementalMarketRequest,
    IncrementalMarketService,
)
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.core.models import AssetClass, MetricResult
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.market.alpaca_normalizer import (
    ASSET_ID,
    alpaca_source_id,
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar
from investment_analyst.storage.analytical_v2_validation import (
    AnalyticalV2ValidationContext,
    market_artifact_digests_for_metrics,
)
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.workspace.raw_v2_backup import (
    RAW_V2_BACKUP_MANIFEST_SCHEMA_V5,
    RawV2BackupError,
    RawV2StagingBackupService,
)

_BASE = datetime(2024, 1, 2, 16, tzinfo=UTC)
_KNOWN_AT = datetime(2100, 1, 1, tzinfo=UTC)
_WINDOWS = (12, 20, 26)
_PROFILE_NAMES = (
    "market_incremental_v2_full_delta",
    "market_incremental_v2_restore_resume",
    "market_incremental_v2_scaling",
)
_TABLES = (
    "raw_v2_index",
    "normalized_observations_v2",
    "metric_results_v2",
    "metric_v2_metric_links",
    "metric_v2_observation_links",
    "market_daily_prefixes_v2",
    "market_daily_prefix_observation_links_v2",
    "market_recursive_checkpoints_v2",
    "market_recursive_checkpoint_metric_links_v2",
)


class SmokeAssertionError(RuntimeError):
    """Raised when a smoke acceptance check fails."""


class SimulatedInterruption(RuntimeError):
    """Raised once at a durable page boundary in the restore-resume profile."""


class SqlMeter:
    """Count SQL calls on a DuckDB connection without retaining query text."""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self.raw = connection
        self.reset()
        self.fail_prefix_insert_number: int | None = None
        self.prefix_inserts = 0

    def reset(self) -> None:
        self.statements = 0
        self.select_statements = 0
        self.write_statements = 0
        self.prefix_inserts = 0

    def execute(self, query: str, *args: object, **kwargs: object) -> object:
        normalized = " ".join(query.split())
        upper = normalized.upper()
        first_word = upper.partition(" ")[0]
        self.statements += 1
        if any(
            token in upper
            for token in (
                "INSERT INTO ",
                "UPDATE ",
                "DELETE FROM ",
                "CREATE TABLE ",
                "ALTER TABLE ",
                "DROP TABLE ",
            )
        ):
            self.write_statements += 1
        elif first_word in {"SELECT", "WITH"}:
            self.select_statements += 1
        if "INSERT INTO MARKET_DAILY_PREFIXES_V2" in upper:
            self.prefix_inserts += 1
            if self.prefix_inserts == self.fail_prefix_insert_number:
                self.fail_prefix_insert_number = None
                raise SimulatedInterruption("simulated interruption before the next prefix page")
        return self.raw.execute(query, *args, **kwargs)

    def executemany(self, query: str, *args: object, **kwargs: object) -> object:
        normalized = " ".join(query.split())
        upper = normalized.upper()
        first_word = upper.partition(" ")[0]
        self.statements += 1
        if any(
            token in upper
            for token in (
                "INSERT INTO ",
                "UPDATE ",
                "DELETE FROM ",
                "CREATE TABLE ",
                "ALTER TABLE ",
                "DROP TABLE ",
            )
        ):
            self.write_statements += 1
        elif first_word in {"SELECT", "WITH"}:
            self.select_statements += 1
        return self.raw.executemany(query, *args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self.raw, name)

    def snapshot(self) -> dict[str, int]:
        return {
            "sql_statements": self.statements,
            "select_statements": self.select_statements,
            "write_statements": self.write_statements,
        }


def _open_staging(path: Path, *, meter: bool = True) -> tuple[RawV2Staging, SqlMeter]:
    path.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path / "raw-v2-index.duckdb"))
    measured = SqlMeter(connection)
    staging = RawV2Staging(path, measured if meter else connection)
    staging.open()
    return staging, measured


def _close_staging(staging: RawV2Staging, meter: SqlMeter) -> None:
    staging.close()
    meter.raw.close()


def _bar(
    index: int, *, symbol: str = "AAPL", close_adjustment: Decimal = Decimal("0")
) -> AlpacaStockBar:
    timestamp = _timestamp(index)
    close = Decimal("150") + Decimal(index) * Decimal("0.03125") + close_adjustment
    open_value = close - Decimal("0.125")
    high = close + Decimal("0.875")
    low = close - Decimal("0.75")
    volume = Decimal("10000") + Decimal(index * 17)
    return AlpacaStockBar(
        symbol=symbol,
        timestamp=timestamp,
        open=open_value,
        high=high,
        low=low,
        close=close,
        volume=volume,
        trade_count=Decimal("100") + Decimal(index),
        vwap=close,
        raw_values={
            "t": timestamp.isoformat().replace("+00:00", "Z"),
            "o": str(open_value),
            "h": str(high),
            "l": str(low),
            "c": str(close),
            "v": str(volume),
            "n": str(Decimal("100") + Decimal(index)),
            "vw": str(close),
        },
    )


def _timestamp(index: int) -> datetime:
    """Use an irregular daily calendar while keeping each bar strictly ordered."""
    return _BASE + timedelta(days=index + index // 17)


def _raw_and_observations(
    bar: AlpacaStockBar,
    *,
    retrieved_at: datetime,
    configuration: AlpacaAssetConfiguration | None = None,
) -> tuple[object, tuple[object, ...]]:
    raw = bar_to_raw_record(
        bar,
        retrieved_at=retrieved_at,
        request_url="https://data.alpaca.markets/smoke",
        configuration=configuration,
    )
    observations = bar_to_observations(
        bar,
        raw,
        normalized_at=retrieved_at + timedelta(minutes=1),
        configuration=configuration,
    )
    return raw, tuple(observations)


def _stage_bars(
    staging: RawV2Staging,
    indexes: Iterable[int],
    *,
    retrieved_at: datetime | None = None,
    correction_index: int | None = None,
    symbol: str = "AAPL",
    configuration: AlpacaAssetConfiguration | None = None,
) -> None:
    records = []
    observations = []
    for index in indexes:
        bar = _bar(
            index,
            symbol=symbol,
            close_adjustment=(Decimal("0.75") if index == correction_index else Decimal("0")),
        )
        available_at = retrieved_at or _timestamp(index) + timedelta(hours=2)
        raw, normalized = _raw_and_observations(
            bar,
            retrieved_at=available_at,
            configuration=configuration,
        )
        records.append(raw)
        observations.extend(normalized)
    for start in range(0, len(records), 256):
        staging.save_many(records[start : start + 256])
    for start in range(0, len(observations), 256):
        staging.save_observations(observations[start : start + 256])


def _stage_noise(staging: RawV2Staging) -> int:
    records = []
    observations = []
    bars_per_asset = 64
    for asset_index in range(32):
        symbol = f"N{asset_index:03d}"
        configuration = AlpacaAssetConfiguration(
            asset_id=f"equity:us:{symbol.lower()}",
            symbol=symbol,
            feed="iex",
            adjustment="all",
            source_id=alpaca_source_id(symbol),
            name=f"Synthetic Asset {asset_index}",
            asset_class=AssetClass.EQUITY,
            quote_currency="USD",
            exchange="NYSE",
        )
        for index in range(bars_per_asset):
            bar = _bar(index, symbol=symbol)
            raw, normalized = _raw_and_observations(
                bar,
                retrieved_at=_timestamp(index) + timedelta(hours=2),
                configuration=configuration,
            )
            records.append(raw)
            observations.extend(normalized)
    for start in range(0, len(records), 256):
        staging.save_many(records[start : start + 256])
    for start in range(0, len(observations), 256):
        staging.save_observations(observations[start : start + 256])
    return len(records)


def _request(
    *,
    history_count: int,
    output_start_index: int,
    known_at: datetime = _KNOWN_AT,
    computed_at: datetime | None = None,
    requested_end_index: int | None = None,
) -> IncrementalMarketRequest:
    last_index = history_count - 1 if requested_end_index is None else requested_end_index - 1
    history_end = _timestamp(last_index) + timedelta(days=1)
    return IncrementalMarketRequest(
        statistics=MarketStatisticsRequest(
            query=HistoricalBarQuery(
                asset_id=ASSET_ID,
                source_id=ALPACA_SOURCE_ID,
                start=_timestamp(output_start_index),
                end=history_end,
                known_at=known_at,
            ),
            sma_windows=(5, 10, 20),
            volatility_window=10,
            relative_volume_window=10,
            bollinger_window=20,
            ema_windows=_WINDOWS,
            rsi_window=14,
            atr_window=14,
            macd_fast_window=12,
            macd_slow_window=26,
            macd_signal_window=9,
        ),
        history_start=_BASE,
        history_end=history_end,
        computed_at=computed_at or known_at + timedelta(hours=1),
    )


def _run(
    staging: RawV2Staging,
    meter: SqlMeter,
    request: IncrementalMarketRequest,
) -> tuple[IncrementalMarketReceipt, dict[str, int], int]:
    meter.reset()
    started = perf_counter_ns()
    receipt = IncrementalMarketService(staging).run(request)
    elapsed = (perf_counter_ns() - started) // 1_000
    return receipt, meter.snapshot(), elapsed


def _metric_models(
    staging: RawV2Staging,
    meter: SqlMeter,
    *,
    start: datetime,
    end: datetime,
) -> dict[UUID, MetricResult]:
    rows = meter.raw.execute(
        "SELECT result_id FROM metric_results_v2 WHERE asset_id = ? AND as_of >= ? AND as_of < ? "
        "ORDER BY as_of, metric_key, result_id",
        [ASSET_ID, start.isoformat(), end.isoformat()],
    ).fetchall()
    result: dict[UUID, MetricResult] = {}
    identifiers = [UUID(str(row[0])) for row in rows]
    for offset in range(0, len(identifiers), 256):
        result.update(staging.get_metrics(identifiers[offset : offset + 256]))
    return result


def _assert_same_metrics(
    expected: dict[UUID, MetricResult],
    actual: dict[UUID, MetricResult],
    *,
    label: str,
) -> None:
    if expected.keys() != actual.keys():
        raise SmokeAssertionError(
            f"{label}: metric IDs differ ({len(expected)} versus {len(actual)})"
        )
    attributes = (
        "asset_id",
        "metric_key",
        "value",
        "unit",
        "as_of",
        "available_at",
        "parameters",
        "input_observation_ids",
        "input_metric_result_ids",
        "algorithm_version",
        "quality",
    )
    for identifier, expected_metric in expected.items():
        actual_metric = actual[identifier]
        if any(
            getattr(expected_metric, attribute) != getattr(actual_metric, attribute)
            for attribute in attributes
        ):
            raise SmokeAssertionError(f"{label}: metric {identifier} differs semantically")


def _counts(connection: duckdb.DuckDBPyConnection) -> dict[str, dict[str, int]]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    output: dict[str, dict[str, int]] = {}
    for table in _TABLES:
        if table not in tables:
            continue
        columns = [
            str(row[0])
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
            if str(row[0]) != "inserted_at"
        ]
        terms = [
            f'COALESCE(OCTET_LENGTH(CAST(CAST("{column}" AS VARCHAR) AS BLOB)), 0)'
            for column in columns
        ]
        byte_sum = " + ".join(terms) if terms else "0"
        row = connection.execute(
            f'SELECT COUNT(*), COALESCE(SUM({byte_sum}), 0) FROM "{table}"'
        ).fetchone()
        output[table] = {"rows": int(row[0]), "logical_bytes": int(row[1])}
    return output


def _storage_size(path: Path, connection: duckdb.DuckDBPyConnection) -> dict[str, int]:
    connection.execute("CHECKPOINT")
    files = [item for item in path.rglob("*") if item.is_file() and not item.is_symlink()]
    database_bytes = sum(
        item.stat().st_size for item in files if item.name.startswith("raw-v2-index")
    )
    raw_root = path / "raw"
    blob_bytes = sum(item.stat().st_size for item in files if raw_root in item.parents)
    return {
        "scratch_file_count": len(files),
        "scratch_bytes": sum(item.stat().st_size for item in files),
        "database_bytes": database_bytes,
        "raw_blob_bytes": blob_bytes,
    }


def _rss_max_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage if platform.system() == "Darwin" else usage * 1024)


def _receipt_summary(
    receipt: IncrementalMarketReceipt,
    sql: dict[str, int],
    elapsed_microseconds: int,
) -> dict[str, object]:
    return {
        **receipt.model_dump(mode="json"),
        "sql": sql,
        "service_elapsed_microseconds": elapsed_microseconds,
        "rss_high_water_bytes": _rss_max_bytes(),
    }


def _profile_full_delta(root: Path) -> dict[str, object]:
    count = 257
    output_start = 170
    full_path = root / "full"
    full, full_meter = _open_staging(full_path)
    try:
        _stage_bars(full, range(count))
        full_request = _request(history_count=count, output_start_index=output_start)
        first, sql, elapsed = _run(full, full_meter, full_request)
        full_rows = _metric_models(
            full,
            full_meter,
            start=full_request.statistics.query.start,
            end=full_request.statistics.query.end,
        )
        if first.selected_bars != count or first.bars_recalculated != count:
            raise SmokeAssertionError("full pass did not seed every eligible historical bar")

        repeated_request = _request(
            history_count=count,
            output_start_index=output_start,
            known_at=_KNOWN_AT + timedelta(days=1),
            computed_at=_KNOWN_AT + timedelta(days=2),
        )
        repeated, repeated_sql, repeated_elapsed = _run(full, full_meter, repeated_request)
        if any(
            (
                repeated.close_prefixes_created,
                repeated.hlc_prefixes_created,
                repeated.checkpoints_created,
                repeated.metrics_created,
                repeated.bar_models_hydrated,
                repeated.finite_bar_models_hydrated,
            )
        ):
            raise SmokeAssertionError("clock-only repeat created or hydrated new artifacts")

        latest_histogram = next(
            (
                item
                for item in full_rows.values()
                if item.metric_key == "market.technical.macd.histogram"
                and item.as_of == _timestamp(count - 1)
            ),
            None,
        )
        if latest_histogram is None:
            raise SmokeAssertionError("full pass did not emit the terminal MACD histogram")
        artifact_digests = market_artifact_digests_for_metrics(
            full_meter,
            (latest_histogram.result_id,),
            validation_context=AnalyticalV2ValidationContext(),
        )
        if not artifact_digests:
            raise SmokeAssertionError("direct and indirect daily lineage was not resolved")

        resumed_path = root / "resumed"
        resumed, resumed_meter = _open_staging(resumed_path)
        try:
            split = count - 1
            _stage_bars(resumed, range(split))
            first_request = _request(
                history_count=split,
                output_start_index=output_start,
                requested_end_index=split,
            )
            _run(resumed, resumed_meter, first_request)
            _stage_bars(resumed, range(split, count))
            continuation_request = _request(
                history_count=count,
                output_start_index=output_start,
            )
            continuation, continuation_sql, continuation_elapsed = _run(
                resumed,
                resumed_meter,
                continuation_request,
            )
            resumed_rows = _metric_models(
                resumed,
                resumed_meter,
                start=continuation_request.statistics.query.start,
                end=continuation_request.statistics.query.end,
            )
            _assert_same_metrics(full_rows, resumed_rows, label="full versus resumed")
            if continuation.bars_recalculated != 1:
                raise SmokeAssertionError("resume did not calculate only the final appended bar")
        finally:
            _close_staging(resumed, resumed_meter)

        max_original_available = _timestamp(count - 1) + timedelta(hours=2, minutes=1)
        corrected_available = max_original_available + timedelta(hours=1)
        _stage_bars(
            full,
            (180,),
            retrieved_at=corrected_available,
            correction_index=180,
        )
        old_cut_request = _request(
            history_count=count,
            output_start_index=output_start,
            known_at=max_original_available + timedelta(minutes=1),
            computed_at=max_original_available + timedelta(hours=2),
        )
        old_cut, old_cut_sql, old_cut_elapsed = _run(full, full_meter, old_cut_request)
        if any(
            (old_cut.close_prefixes_created, old_cut.checkpoints_created, old_cut.metrics_created)
        ):
            raise SmokeAssertionError("a pre-correction point-in-time cut changed the artifact set")
        revised_request = _request(
            history_count=count,
            output_start_index=output_start,
            known_at=corrected_available + timedelta(minutes=2),
            computed_at=corrected_available + timedelta(hours=1),
        )
        revised, revised_sql, revised_elapsed = _run(full, full_meter, revised_request)
        if (
            revised.close_divergence_index != 180
            or revised.hlc_divergence_index != 180
            or revised.checkpoints_created == 0
            or revised.finite_window_calculations == 0
        ):
            raise SmokeAssertionError(
                "historical correction did not invalidate the dependent suffix"
            )
        revised_rows = _metric_models(
            full,
            full_meter,
            start=revised_request.statistics.query.start,
            end=revised_request.statistics.query.end,
        )
        if revised_rows.keys() == full_rows.keys():
            raise SmokeAssertionError("revised point-in-time inputs retained every old metric ID")
        inventory = _counts(full_meter.raw)
        storage = _storage_size(full_path, full_meter.raw)
        return {
            "profile": "market_incremental_v2_full_delta",
            "rows": count,
            "output_start_index": output_start,
            "full": _receipt_summary(first, sql, elapsed),
            "clock_only_repeat": _receipt_summary(repeated, repeated_sql, repeated_elapsed),
            "resume_delta_1": _receipt_summary(
                continuation,
                continuation_sql,
                continuation_elapsed,
            ),
            "old_point_in_time_cut": _receipt_summary(old_cut, old_cut_sql, old_cut_elapsed),
            "correction": _receipt_summary(revised, revised_sql, revised_elapsed),
            "semantic_metric_count": len(full_rows),
            "resumed_metric_count": len(resumed_rows),
            "revised_metric_count": len(revised_rows),
            "reachable_daily_artifact_digests": len(artifact_digests),
            "inventory": inventory,
            "storage": storage,
        }
    finally:
        _close_staging(full, full_meter)


def _profile_restore_resume(root: Path) -> dict[str, object]:
    count = 257
    output_start = count - 32
    reference, reference_meter = _open_staging(root / "reference")
    interrupted, interrupted_meter = _open_staging(root / "interrupted")
    try:
        _stage_bars(reference, range(count))
        reference_request = _request(history_count=count, output_start_index=output_start)
        reference_receipt, reference_sql, reference_elapsed = _run(
            reference,
            reference_meter,
            reference_request,
        )
        reference_rows = _metric_models(
            reference,
            reference_meter,
            start=reference_request.statistics.query.start,
            end=reference_request.statistics.query.end,
        )

        _stage_bars(interrupted, range(count))
        interrupted_meter.fail_prefix_insert_number = 3
        interrupted_meter.reset()
        try:
            IncrementalMarketService(interrupted).run(reference_request)
        except SimulatedInterruption:
            pass
        else:
            raise SmokeAssertionError("interruption injection did not stop the second prefix page")
        durable_prefix_count = int(
            interrupted_meter.raw.execute(
                "SELECT COUNT(*) FROM market_daily_prefixes_v2"
            ).fetchone()[0]
        )
        durable_checkpoint_count = int(
            interrupted_meter.raw.execute(
                "SELECT COUNT(*) FROM market_recursive_checkpoints_v2"
            ).fetchone()[0]
        )
        if durable_prefix_count != 512 or durable_checkpoint_count == 0:
            raise SmokeAssertionError("the completed first page was not durable after interruption")

        backup_path = root / "backup-v5"
        backup_create_started = perf_counter_ns()
        backup_manifest = RawV2StagingBackupService().create(
            interrupted,
            interrupted_meter.raw,
            backup_path,
        )
        backup_create_elapsed = (perf_counter_ns() - backup_create_started) // 1_000
        if backup_manifest.schema_version != RAW_V2_BACKUP_MANIFEST_SCHEMA_V5:
            raise SmokeAssertionError("incremental staging backup did not use manifest v5")

        corrupt_path = root / "corrupt-backup"
        shutil.copytree(backup_path, corrupt_path)
        corrupt_file_entry = next(item for item in backup_manifest.files if item.size_bytes > 0)
        corrupt_file = corrupt_path / corrupt_file_entry.path
        with corrupt_file.open("r+b") as handle:
            original = handle.read(1)
            handle.seek(0)
            handle.write(bytes([original[0] ^ 1]))
        corrupt_destination = root / "corrupt-promoted-destination"
        corrupt_restore_started = perf_counter_ns()
        try:
            RawV2StagingBackupService().restore(corrupt_path, corrupt_destination)
        except RawV2BackupError:
            pass
        else:
            raise SmokeAssertionError("corrupt backup copy was accepted")
        corrupt_restore_validation_elapsed = (perf_counter_ns() - corrupt_restore_started) // 1_000
        if corrupt_destination.exists():
            raise SmokeAssertionError("corrupt backup created or promoted its destination")

        restore_path = root / "restored"
        restore_started = perf_counter_ns()
        restored_manifest = RawV2StagingBackupService().restore(backup_path, restore_path)
        restore_elapsed = (perf_counter_ns() - restore_started) // 1_000
        if restored_manifest.backup_id != backup_manifest.backup_id:
            raise SmokeAssertionError("restore changed the backup identity")
        restored, restored_meter = _open_staging(restore_path)
        try:
            resumed, resume_sql, resume_elapsed = _run(
                restored,
                restored_meter,
                reference_request,
            )
            resumed_rows = _metric_models(
                restored,
                restored_meter,
                start=reference_request.statistics.query.start,
                end=reference_request.statistics.query.end,
            )
            _assert_same_metrics(
                reference_rows, resumed_rows, label="uninterrupted versus restored"
            )
            if resumed.bars_recalculated != 1:
                raise SmokeAssertionError("restore-resume recalculated more than the missing bar")
            inventory = _counts(restored_meter.raw)
            storage = _storage_size(restore_path, restored_meter.raw)
        finally:
            _close_staging(restored, restored_meter)

        return {
            "profile": "market_incremental_v2_restore_resume",
            "rows": count,
            "interruption": {
                "after_durable_prefix_rows": durable_prefix_count,
                "after_durable_checkpoint_rows": durable_checkpoint_count,
            },
            "backup_schema": backup_manifest.schema_version,
            "backup_id": str(backup_manifest.backup_id),
            "backup_create_elapsed_microseconds": backup_create_elapsed,
            "corrupt_restore_validation_elapsed_microseconds": (corrupt_restore_validation_elapsed),
            "restore_elapsed_microseconds": restore_elapsed,
            "corrupt_copy_rejected": True,
            "corrupt_destination_promoted": False,
            "uninterrupted": _receipt_summary(
                reference_receipt,
                reference_sql,
                reference_elapsed,
            ),
            "restore_resume": _receipt_summary(resumed, resume_sql, resume_elapsed),
            "uninterrupted_metric_count": len(reference_rows),
            "restored_metric_count": len(resumed_rows),
            "inventory": inventory,
            "storage": storage,
        }
    finally:
        _close_staging(reference, reference_meter)
        _close_staging(interrupted, interrupted_meter)


def _profile_scaling(root: Path) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for count in (257, 1537):
        path = root / f"rows-{count}"
        staging, meter = _open_staging(path)
        try:
            _stage_bars(staging, range(count))
            output_start = max(0, count - 40)
            initial_request = _request(history_count=count, output_start_index=output_start)
            initial, initial_sql, initial_elapsed = _run(staging, meter, initial_request)
            expected_recurrence_count = count * 6
            if initial.checkpoints_created != expected_recurrence_count:
                raise SmokeAssertionError(
                    f"N={count}: expected {expected_recurrence_count} recurrence checkpoints, "
                    f"got {initial.checkpoints_created}"
                )

            before_noise, before_noise_sql, before_noise_elapsed = _run(
                staging,
                meter,
                _request(
                    history_count=count,
                    output_start_index=output_start,
                    known_at=_KNOWN_AT + timedelta(days=1),
                    computed_at=_KNOWN_AT + timedelta(days=2),
                ),
            )
            noise_rows = _stage_noise(staging)
            after_noise, after_noise_sql, after_noise_elapsed = _run(
                staging,
                meter,
                _request(
                    history_count=count,
                    output_start_index=output_start,
                    known_at=_KNOWN_AT + timedelta(days=2),
                    computed_at=_KNOWN_AT + timedelta(days=3),
                ),
            )
            if noise_rows != 2048:
                raise SmokeAssertionError("noise profile did not insert 2048 unrelated market bars")
            if before_noise.bar_models_hydrated != 0 or after_noise.bar_models_hydrated != 0:
                raise SmokeAssertionError("unrelated assets increased target-bar hydration")

            _stage_bars(staging, (count,))
            delta_one, delta_one_sql, delta_one_elapsed = _run(
                staging,
                meter,
                _request(
                    history_count=count + 1,
                    output_start_index=output_start,
                    requested_end_index=count + 1,
                ),
            )
            _stage_bars(staging, range(count + 1, count + 4))
            delta_three, delta_three_sql, delta_three_elapsed = _run(
                staging,
                meter,
                _request(
                    history_count=count + 4,
                    output_start_index=output_start,
                    requested_end_index=count + 4,
                ),
            )
            for delta, receipt in ((1, delta_one), (3, delta_three)):
                if receipt.bars_recalculated != delta:
                    raise SmokeAssertionError(f"N={count}, delta={delta}: wrong recurrence work")
                if receipt.maximum_metric_batch > 256:
                    raise SmokeAssertionError(
                        f"N={count}, delta={delta}: metric batch exceeded 256"
                    )
                if receipt.metrics_created > 30 * delta:
                    raise SmokeAssertionError(
                        f"N={count}, delta={delta}: default output exceeded 30 rows per new bar"
                    )
                if receipt.bar_models_hydrated > delta + receipt.finite_window_halo:
                    raise SmokeAssertionError(
                        f"N={count}, delta={delta}: hydration exceeded the declared finite halo"
                    )

            inventory = _counts(meter.raw)
            storage = _storage_size(path, meter.raw)
            cases.append(
                {
                    "history_bars": count,
                    "unrelated_asset_count": 32,
                    "bars_per_unrelated_asset": 64,
                    "unrelated_bars": noise_rows,
                    "initial_full": _receipt_summary(initial, initial_sql, initial_elapsed),
                    "delta_zero_before_noise": _receipt_summary(
                        before_noise,
                        before_noise_sql,
                        before_noise_elapsed,
                    ),
                    "delta_zero_after_noise": _receipt_summary(
                        after_noise,
                        after_noise_sql,
                        after_noise_elapsed,
                    ),
                    "delta_1": _receipt_summary(delta_one, delta_one_sql, delta_one_elapsed),
                    "delta_3": _receipt_summary(delta_three, delta_three_sql, delta_three_elapsed),
                    "inventory": inventory,
                    "storage": storage,
                }
            )
        finally:
            _close_staging(staging, meter)
    return {"profile": "market_incremental_v2_scaling", "cases": cases}


def _source_metadata() -> dict[str, object]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        head = "unknown"
        tree = "unknown"
        status = "unknown"
    worktree_dirty = bool(status.strip())
    return {
        "head_sha": head,
        "tree_sha": tree,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "code_sha": None if worktree_dirty else head,
        "worktree_dirty": worktree_dirty,
        "working_tree_paths": [line[3:] for line in status.splitlines() if len(line) >= 4],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("all", *_PROFILE_NAMES),
        default="all",
        help="run all registered profiles or one profile by its stable ID",
    )
    args = parser.parse_args()
    chosen = _PROFILE_NAMES if args.profile == "all" else (args.profile,)
    started_at = datetime.now(UTC)
    started = perf_counter_ns()
    with tempfile.TemporaryDirectory(prefix="investment-analyst-market-incremental-v2-") as root:
        scratch = Path(root).resolve()
        results: list[dict[str, object]] = []
        for profile in chosen:
            if profile == "market_incremental_v2_full_delta":
                results.append(_profile_full_delta(scratch / "full-delta"))
            elif profile == "market_incremental_v2_restore_resume":
                results.append(_profile_restore_resume(scratch / "restore-resume"))
            else:
                results.append(_profile_scaling(scratch / "scaling"))
    code_metadata = _source_metadata()
    document = {
        "schema_version": "market-incremental-v2-smoke-v1",
        "status": "PASS",
        **code_metadata,
        "synthetic_data_manifest": {
            "classification": "synthetic daily market bars generated in-process",
            "live_provider_calls": 0,
            "permanent_workspace_access": False,
            "target": {
                "asset_id": ASSET_ID,
                "symbol": "AAPL",
                "source_id": ALPACA_SOURCE_ID,
                "provider": "Alpaca Market Data fixture; no network request",
                "feed": "iex",
                "adjustment": "all",
                "frequency": "daily",
            },
            "inventory": {
                "full_delta_profile_history_bars": 257,
                "restore_resume_profile_history_bars": 257,
                "scaling_history_bars": [257, 1537],
                "unrelated_assets_per_scaling_case": 32,
                "bars_per_unrelated_asset": 64,
                "unrelated_bars_per_scaling_case": 2048,
                "scaling_case_count": 2,
            },
            "parameters": {
                "base_timestamp": _BASE.isoformat(),
                "known_at": _KNOWN_AT.isoformat(),
                "full_delta_output_start_index": 170,
                "restore_resume_output_start_index": 225,
                "scaling_output_start_rule": "max(0, history_bars - 40)",
                "scaling_deltas": [0, 1, 3],
                "recursive_windows": list(_WINDOWS),
                "sma_windows": [5, 10, 20],
                "volatility_window": 10,
                "relative_volume_window": 10,
                "bollinger_window": 20,
                "rsi_window": 14,
                "atr_window": 14,
                "macd_windows": {"fast": 12, "slow": 26, "signal": 9},
            },
            "generator": {
                "timestamps": "base + index days + floor(index / 17) days",
                "close": "150 + index / 8",
                "volume": "100 + index",
                "trade_count": "100 + index",
                "random_seed": None,
                "randomness": "none; fixed Decimal formulas, no PRNG",
            },
        },
        "command": [sys.executable, "scripts/smoke_market_incremental_v2.py", *sys.argv[1:]],
        "environment": {
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "platform": platform.platform(),
        },
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "elapsed_microseconds": (perf_counter_ns() - started) // 1_000,
        "rss_high_water_bytes": _rss_max_bytes(),
        "scratch_root": "temporary directory, removed after successful completion",
        "profiles": results,
    }
    print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
