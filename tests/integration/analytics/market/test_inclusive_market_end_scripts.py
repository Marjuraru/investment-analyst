"""Regression coverage for inclusive calendar-date market analytics commands."""

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
_STATISTICS_SCRIPT = _ROOT / "scripts" / "compute_market_statistics.py"
_DIAGNOSTIC_SCRIPT = _ROOT / "scripts" / "compute_market_diagnostic.py"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_ROOT / "src")
    environment.pop("ALPACA_API_KEY", None)
    environment.pop("ALPACA_API_SECRET", None)
    return environment


def _prepare(root: Path) -> None:
    timestamps = (
        datetime(2026, 1, 13, 5, tzinfo=UTC),
        datetime(2026, 1, 14, 5, tzinfo=UTC),
        datetime(2026, 1, 15, 5, tzinfo=UTC),
        datetime(2026, 1, 16, 5, tzinfo=UTC),
    )
    retrieved = datetime(2026, 1, 18, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        for timestamp in timestamps:
            close = Decimal("100")
            candle = CoinbaseCandle(
                product_id="BTC-USD",
                start=timestamp,
                low=close - 1,
                high=close + 1,
                open=close,
                close=close,
                volume=Decimal("100"),
                raw_values=(
                    str(int(timestamp.timestamp())),
                    str(close - 1),
                    str(close + 1),
                    str(close),
                    str(close),
                    "100",
                ),
            )
            raw = candle_to_raw_record(
                candle,
                retrieved_at=retrieved,
                request_url="https://api.exchange.coinbase.com/test",
            )
            storage.raw_records.save(raw)
            for observation in candle_to_observations(
                candle,
                raw,
                normalized_at=retrieved + timedelta(minutes=1),
            ):
                storage.observations.save(observation)


def _base_command(script: Path, root: Path) -> list[str]:
    return [
        sys.executable,
        str(script),
        "--root",
        str(root),
        "--asset-id",
        "crypto:btc-usd",
        "--source-id",
        _SOURCE_ID,
        "--start",
        "2026-01-13",
        "--end",
        "2026-01-15",
        "--known-at",
        "2026-01-20T00:00:00Z",
    ]


def test_statistics_and_diagnostic_include_final_date_but_not_next_day(
    tmp_path,
) -> None:
    _prepare(tmp_path)
    statistics = subprocess.run(
        _base_command(_STATISTICS_SCRIPT, tmp_path)
        + [
            "--sma-window",
            "1",
            "--sma-window",
            "2",
            "--volatility-window",
            "2",
            "--relative-volume-window",
            "1",
            "--output-limit",
            "100",
        ],
        cwd=_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert statistics.returncode == 0, statistics.stderr
    statistics_payload = json.loads(statistics.stdout)
    assert statistics_payload["summary"]["bar_count"] == 3
    result_times = {
        datetime.fromisoformat(item["as_of"].replace("Z", "+00:00"))
        for item in statistics_payload["results"]
    }
    assert datetime(2026, 1, 15, 5, tzinfo=UTC) in result_times
    assert datetime(2026, 1, 16, 5, tzinfo=UTC) not in result_times

    diagnostic = subprocess.run(
        _base_command(_DIAGNOSTIC_SCRIPT, tmp_path)
        + [
            "--short-sma-window",
            "1",
            "--long-sma-window",
            "2",
            "--volatility-window",
            "2",
            "--relative-volume-window",
            "1",
        ],
        cwd=_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert diagnostic.returncode == 0, diagnostic.stderr
    diagnostic_payload = json.loads(diagnostic.stdout)
    as_of = datetime.fromisoformat(diagnostic_payload["summary"]["as_of"].replace("Z", "+00:00"))
    assert as_of == datetime(2026, 1, 15, 5, tzinfo=UTC)
