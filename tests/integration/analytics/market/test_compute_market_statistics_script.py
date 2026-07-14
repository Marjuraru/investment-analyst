"""Subprocess tests for the local market-statistics command."""

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
_SCRIPT = _ROOT / "scripts" / "compute_market_statistics.py"
_SOURCE = "coinbase-exchange:btc-usd:daily-candles"


def _prepare(root: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        for index in range(4):
            timestamp = start + timedelta(days=index)
            retrieved = timestamp + timedelta(hours=1)
            close = Decimal("100") + Decimal(index)
            candle = CoinbaseCandle(
                product_id="BTC-USD",
                start=timestamp,
                low=close - 1,
                high=close + 1,
                open=close,
                close=close,
                volume=Decimal("100") + Decimal(index * 10),
                raw_values=(
                    str(int(timestamp.timestamp())),
                    str(close - 1),
                    str(close + 1),
                    str(close),
                    str(close),
                    str(Decimal("100") + Decimal(index * 10)),
                ),
            )
            raw = candle_to_raw_record(
                candle,
                retrieved_at=retrieved,
                request_url="https://api.exchange.coinbase.com/test",
            )
            storage.raw_records.save(raw)
            for observation in candle_to_observations(
                candle, raw, normalized_at=retrieved + timedelta(minutes=1)
            ):
                storage.observations.save(observation)


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_ROOT / "src")
    environment.pop("ALPACA_API_KEY", None)
    environment.pop("ALPACA_API_SECRET", None)
    return environment


def _command(root: Path, source: str = _SOURCE) -> list[str]:
    return [
        sys.executable,
        str(_SCRIPT),
        "--root",
        str(root),
        "--asset-id",
        "crypto:btc-usd",
        "--source-id",
        source,
        "--start",
        "2026-01-01",
        "--end",
        "2026-01-05",
        "--known-at",
        "2026-03-01T00:00:00Z",
        "--sma-window",
        "1",
        "--sma-window",
        "2",
        "--volatility-window",
        "2",
        "--relative-volume-window",
        "2",
        "--output-limit",
        "2",
    ]


def test_script_outputs_json_limit_and_reuses_results(tmp_path) -> None:
    _prepare(tmp_path)
    first = subprocess.run(
        _command(tmp_path),
        capture_output=True,
        check=False,
        text=True,
        env=_environment(),
    )
    second = subprocess.run(
        _command(tmp_path),
        capture_output=True,
        check=False,
        text=True,
        env=_environment(),
    )

    assert first.returncode == second.returncode == 0
    payload = json.loads(first.stdout)
    reused = json.loads(second.stdout)
    assert len(payload["results"]) == 2
    assert payload["truncated"] is True
    assert reused["summary"]["results_created"] == 0
    assert reused["summary"]["results_reused"] == reused["summary"]["results_generated"]
    assert "RawRecord" not in first.stdout


def test_script_empty_storage_and_unknown_source(tmp_path) -> None:
    empty = subprocess.run(
        _command(tmp_path),
        capture_output=True,
        check=False,
        text=True,
        env=_environment(),
    )
    unknown = subprocess.run(
        _command(tmp_path, "unknown:source"),
        capture_output=True,
        check=False,
        text=True,
        env=_environment(),
    )

    assert empty.returncode == 0
    assert json.loads(empty.stdout)["summary"]["results_generated"] == 0
    assert unknown.returncode != 0
    assert "unknown" in unknown.stderr.lower()
    assert "traceback" not in unknown.stderr.lower()
