"""Subprocess tests for the local market-history query script."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseCandle
from investment_analyst.providers.crypto.coinbase_normalizer import (
    candle_to_observations,
    candle_to_raw_record,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _ROOT / "scripts" / "query_market_history.py"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"


def _store_candles(
    root: Path,
    timestamps: tuple[datetime, ...],
    *,
    retrieved_at: datetime,
) -> None:
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        for index, timestamp in enumerate(timestamps):
            close = Decimal("108000") + Decimal(index)
            candle = CoinbaseCandle(
                product_id="BTC-USD",
                start=timestamp,
                low=close - Decimal("8000"),
                high=close + Decimal("2000"),
                open=close - Decimal("6000"),
                close=close,
                volume=Decimal("12000") + Decimal(index),
                raw_values=(
                    str(int(timestamp.timestamp())),
                    str(close - Decimal("8000")),
                    str(close + Decimal("2000")),
                    str(close - Decimal("6000")),
                    str(close),
                    str(Decimal("12000") + Decimal(index)),
                ),
            )
            raw = candle_to_raw_record(
                candle,
                retrieved_at=retrieved_at,
                request_url="https://api.exchange.coinbase.com/test",
            )
            storage.raw_records.save(raw)
            for observation in candle_to_observations(
                candle,
                raw,
                normalized_at=retrieved_at + timedelta(minutes=1),
            ):
                storage.observations.save(observation)


def _prepare_storage(root: Path) -> None:
    _store_candles(
        root,
        (
            datetime(2026, 7, 2, tzinfo=UTC),
            datetime(2026, 7, 3, tzinfo=UTC),
        ),
        retrieved_at=datetime(2026, 7, 5, tzinfo=UTC),
    )


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_ROOT / "src")
    for key in tuple(environment):
        if "API_KEY" in key or "SECRET" in key:
            environment.pop(key)
    return environment


def test_script_prints_valid_bounded_json_without_keys_or_network(tmp_path) -> None:
    _prepare_storage(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(tmp_path),
            "--asset-id",
            "crypto:btc-usd",
            "--source-id",
            _SOURCE_ID,
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-05",
            "--known-at",
            "2026-07-06T00:00:00Z",
            "--limit",
            "1",
        ],
        cwd=_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["bars"]) == 1
    assert payload["coverage"]["bar_count"] == 2
    assert payload["truncated"] is True
    assert payload["traceability_verified"] is True
    assert "raw_candle" not in result.stdout


def test_script_includes_entire_final_date_and_excludes_next_day(tmp_path) -> None:
    expected = (
        datetime(2026, 7, 10, 4, tzinfo=UTC),
        datetime(2026, 7, 13, 4, tzinfo=UTC),
    )
    _store_candles(
        tmp_path,
        expected
        + (
            datetime(2026, 7, 14, tzinfo=UTC),
            datetime(2026, 7, 14, 4, tzinfo=UTC),
        ),
        retrieved_at=datetime(2026, 7, 14, 12, tzinfo=UTC),
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(tmp_path),
            "--asset-id",
            "crypto:btc-usd",
            "--source-id",
            _SOURCE_ID,
            "--start",
            "2026-07-10",
            "--end",
            "2026-07-13",
            "--known-at",
            "2026-07-15T00:00:00Z",
        ],
        cwd=_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    timestamps = tuple(
        datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")) for item in payload["bars"]
    )
    assert timestamps == expected
    assert payload["coverage"]["bar_count"] == 2
    assert payload["query"]["end"] in {
        "2026-07-14T00:00:00Z",
        "2026-07-14T00:00:00+00:00",
    }


def test_script_reports_unknown_source_without_traceback(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(tmp_path),
            "--asset-id",
            "crypto:btc-usd",
            "--source-id",
            "unsupported:source",
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-05",
            "--known-at",
            "2026-07-06T00:00:00+00:00",
        ],
        cwd=_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "unsupported market source" in result.stderr
    assert "Traceback" not in result.stderr
