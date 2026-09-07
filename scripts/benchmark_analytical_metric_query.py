"""Reproducible local benchmark for bounded analytical metric queries."""

from __future__ import annotations

import json
import tempfile
import tracemalloc
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from uuid import UUID

from investment_analyst.core.models import DataQuality, MetricResult
from investment_analyst.storage import LocalStorage, StoragePaths

_TARGET_ASSET = "asset:target"
_OTHER_ASSET = "asset:other"
_BOUNDED_KEYS = ("metric:target:activity", "metric:target:trend")
_NOISE_KEY = "metric:target:noise"
_ROWS_PER_ASSET = 96
_PASSES = 4
_START = datetime(2026, 7, 1, tzinfo=UTC)


def _metric(
    *,
    asset_id: str,
    metric_key: str,
    identifier: int,
    as_of: datetime,
) -> MetricResult:
    available_at = as_of + timedelta(hours=1)
    return MetricResult(
        result_id=UUID(f"80000000-0000-4000-8000-{identifier:012d}"),
        asset_id=asset_id,
        metric_key=metric_key,
        value=Decimal(identifier) / Decimal("10"),
        unit="ratio",
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        parameters={
            "source_id": "benchmark:metric-query",
            "known_at": available_at.isoformat(),
        },
        input_observation_ids=[UUID(f"81000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version="benchmark-metric-query-v1",
        quality=DataQuality.VALID,
    )


def _populate(storage: LocalStorage) -> None:
    identifier = 1
    for asset_id in (_TARGET_ASSET, _OTHER_ASSET):
        for row_number in range(_ROWS_PER_ASSET):
            if asset_id == _TARGET_ASSET and row_number % 3 == 0:
                metric_key = _NOISE_KEY
            elif asset_id == _TARGET_ASSET:
                metric_key = _BOUNDED_KEYS[row_number % len(_BOUNDED_KEYS)]
            else:
                metric_key = "metric:other:activity"
            storage.metric_results.save(
                _metric(
                    asset_id=asset_id,
                    metric_key=metric_key,
                    identifier=identifier,
                    as_of=_START + timedelta(days=row_number),
                )
            )
            identifier += 1


def _measure(
    storage: LocalStorage,
    *,
    metric_keys: tuple[str, ...] | None,
) -> dict[str, int | float]:
    rows_materialized = 0
    documents_deserialized = 0
    started = perf_counter()
    tracemalloc.start()
    try:
        for _ in range(_PASSES):
            rows_materialized += storage.metric_results.count(
                asset_id=_TARGET_ASSET,
                metric_keys=metric_keys,
            )
            results = storage.metric_results.list(
                asset_id=_TARGET_ASSET,
                metric_keys=metric_keys,
            )
            documents_deserialized += len(results)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        "rows_materialized": rows_materialized,
        "documents_deserialized": documents_deserialized,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        "peak_bytes": peak_bytes,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="investment-analyst-metric-query-") as temporary:
        paths = StoragePaths.from_root(Path(temporary) / "synthetic-workspace")
        with LocalStorage(paths) as storage:
            _populate(storage)
            baseline = _measure(storage, metric_keys=None)
            bounded = _measure(storage, metric_keys=_BOUNDED_KEYS)
    print(
        json.dumps(
            {
                "schema_version": "analytical-metric-query-benchmark-v1",
                "target_asset": _TARGET_ASSET,
                "corpus_rows": _ROWS_PER_ASSET * 2,
                "passes": _PASSES,
                "bounded_metric_keys": list(_BOUNDED_KEYS),
                "baseline": baseline,
                "bounded": bounded,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
