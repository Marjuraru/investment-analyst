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


def _prepare_storage(root: Path) -> None:
    retrieved = datetime(2026, 7, 5, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        for day in (2, 3):
            timestamp = datetime(2026, 7, day, tzinfo=UTC)
            candle = CoinbaseCandle(
                product_id="BTC-USD",
                start=timestamp,
                low=Decimal("100000"),
                high=Decimal("110000"),
                open=Decimal("102000"),
                close=Decimal("108000"),
                volume=Decimal("12000"),
                raw_values=(
                    str(int(timestamp.timestamp())),
                    "100000",
                    "110000",
                    "102000",
                    "108000",
                    "12000",
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
