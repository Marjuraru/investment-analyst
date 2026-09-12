#!/usr/bin/env python3
"""Temporary smoke for the retirement of the legacy BTC contracts.

Phase 1 boots ``scripts/serve_investment_analyst.py`` on a loopback port over a
temporary workspace and its state root, seeds synthetic Coinbase evidence without
provider traffic, and reads the BTC daily chart from the running server to prove it is
served by the generic ``crypto-spot-daily-market-chart-v1`` declared by the universe
descriptor.

Phase 2 reads the BTC intraday chart from the same server and proves the response carries
the explicit requested identity under ``crypto-spot-intraday-chart-v1``.

Phase 3 proves the read path fails closed for a catalog asset without
``market.minute_bars`` instead of serving another asset's evidence.

No outbound network access, no permanent workspace, and no local service restart.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.catalog.provider_configuration import (
    resolve_coinbase_configuration,
)
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseCandle
from investment_analyst.providers.crypto.coinbase_intraday_normalizer import (
    candle_to_intraday_observations,
    candle_to_intraday_raw_record,
    create_coinbase_intraday_source,
)
from investment_analyst.providers.crypto.coinbase_normalizer import (
    candle_to_observations,
    candle_to_raw_record,
    create_coinbase_asset,
    create_coinbase_source,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
_SERVE_SCRIPT = _REPOSITORY_ROOT / "scripts" / "serve_investment_analyst.py"
_BTC_ASSET_ID = "crypto:btc-usd"
_BTC_DAILY_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
_BTC_MINUTE_SOURCE_ID = "coinbase-exchange:btc-usd:minute-1-candles"
_RETIRED_SCHEMAS = (
    "btc-market-chart-v1",
    "btc-market-refresh-v1",
    "btc-intraday-chart-v1",
    "btc-intraday-refresh-v1",
)
_KNOWN_AT = datetime(2026, 7, 16, 15, 46, tzinfo=UTC)
_DAILY_SESSIONS = 30
_INTRADAY_MINUTES = 30
_INTRADAY_INTERVAL = "5m"
_PLACEHOLDER_CREDENTIALS = "smoke-loopback-placeholder"
_SEC_USER_AGENT = "investment-analyst smoke smoke@example.invalid"


def _daily_candle(index: int) -> CoinbaseCandle:
    start = _KNOWN_AT.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=_DAILY_SESSIONS - index
    )
    low = Decimal("100000") + Decimal(index)
    return CoinbaseCandle(
        product_id="BTC-USD",
        start=start,
        low=low,
        high=low + Decimal("500"),
        open=low + Decimal("100"),
        close=low + Decimal("250"),
        volume=Decimal("120.5"),
        raw_values=(
            str(int(start.timestamp())),
            str(low),
            str(low + Decimal("500")),
            str(low + Decimal("100")),
            str(low + Decimal("250")),
            str(Decimal("120.5")),
        ),
    )


def _minute_candle(index: int) -> CoinbaseCandle:
    start = _KNOWN_AT - timedelta(minutes=_INTRADAY_MINUTES - index)
    low = Decimal("117000") + Decimal(index)
    return CoinbaseCandle(
        product_id="BTC-USD",
        start=start,
        low=low,
        high=low + Decimal("50"),
        open=low + Decimal("10"),
        close=low + Decimal("25"),
        volume=Decimal("1.25"),
        raw_values=(
            str(int(start.timestamp())),
            str(low),
            str(low + Decimal("50")),
            str(low + Decimal("10")),
            str(low + Decimal("25")),
            str(Decimal("1.25")),
        ),
    )


def _seed_coinbase_evidence(workspace_root: Path) -> dict[str, int]:
    """Persist synthetic Coinbase daily and minute evidence without provider traffic."""
    configuration = resolve_coinbase_configuration(
        ApplicationRuntime.create_default().provider_resolver,
        asset_id=_BTC_ASSET_ID,
    )
    storage_paths = StoragePaths.from_root(workspace_root / "storage")
    normalized_at = _KNOWN_AT + timedelta(seconds=1)
    daily_created = 0
    minute_created = 0
    with LocalStorage(storage_paths) as storage:
        storage.assets.upsert(create_coinbase_asset(configuration))
        storage.sources.upsert(create_coinbase_source(configuration))
        storage.assets.upsert(create_coinbase_asset())
        storage.sources.upsert(create_coinbase_intraday_source())
        for index in range(_DAILY_SESSIONS):
            candle = _daily_candle(index)
            raw_record = candle_to_raw_record(
                candle,
                retrieved_at=_KNOWN_AT,
                request_url="https://api.exchange.coinbase.com/smoke/daily",
                configuration=configuration,
            )
            storage.raw_records.save(raw_record)
            for observation in candle_to_observations(
                candle,
                raw_record,
                normalized_at=normalized_at,
                configuration=configuration,
            ):
                storage.observations.save(observation)
            daily_created += 1
        for index in range(_INTRADAY_MINUTES):
            candle = _minute_candle(index)
            raw_record = candle_to_intraday_raw_record(
                candle,
                retrieved_at=_KNOWN_AT,
                request_url="https://api.exchange.coinbase.com/smoke/minute",
            )
            storage.raw_records.save(raw_record)
            for observation in candle_to_intraday_observations(
                candle,
                raw_record,
                normalized_at=normalized_at,
            ):
                storage.observations.save(observation)
            minute_created += 1
    return {"daily_candles": daily_created, "minute_candles": minute_created}


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("loopback response is not a JSON object")
    return payload


def _error_status(url: str) -> tuple[int, dict[str, object]]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _await_loopback(base_url: str, process: subprocess.Popen[str]) -> dict[str, object]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"local service exited early with code {process.returncode}")
        try:
            return _read_json(f"{base_url}/api/market-assets")
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.25)
    raise RuntimeError("local service did not answer on loopback")


def _start_local_service(workspace_root: Path, port: int) -> subprocess.Popen[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "ALPACA_API_KEY": _PLACEHOLDER_CREDENTIALS,
            "ALPACA_API_SECRET": _PLACEHOLDER_CREDENTIALS,
            "SEC_USER_AGENT": _SEC_USER_AGENT,
            "PYTHONPATH": str(_SOURCE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return subprocess.Popen(
        [
            sys.executable,
            str(_SERVE_SCRIPT),
            "--workspace",
            str(workspace_root),
            "--port",
            str(port),
            "--no-scheduler",
        ],
        cwd=str(_REPOSITORY_ROOT),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop_local_service(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive cleanup
        process.kill()
        process.wait(timeout=15)


def _descriptor(universe: dict[str, object], asset_id: str) -> dict[str, object]:
    assets = universe["assets"]
    if not isinstance(assets, list):
        raise RuntimeError("market asset universe is not a list")
    return next(item for item in assets if item["asset_id"] == asset_id)


def _assert_no_retired_schema(payload: dict[str, object], label: str) -> None:
    serialized = json.dumps(payload)
    for retired in _RETIRED_SCHEMAS:
        if retired in serialized:
            raise RuntimeError(f"{label} still emits the retired contract {retired}")


def _loopback_phases(workspace_root: Path) -> list[dict[str, object]]:
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    known_at = _KNOWN_AT.isoformat().replace("+00:00", "Z")
    process = _start_local_service(workspace_root, port)
    try:
        universe = _await_loopback(base_url, process)
        btc_descriptor = _descriptor(universe, _BTC_ASSET_ID)
        daily = _read_json(
            f"{base_url}/api/market-chart?asset_id=crypto%3Abtc-usd&known_at={known_at}"
        )
        intraday = _read_json(
            f"{base_url}/api/market-intraday"
            f"?asset_id=crypto%3Abtc-usd&known_at={known_at}&interval={_INTRADAY_INTERVAL}"
        )
        foreign_status, foreign_error = _error_status(
            f"{base_url}/api/market-intraday"
            f"?asset_id=crypto%3Aeth-usd&known_at={known_at}&interval={_INTRADAY_INTERVAL}"
        )
    finally:
        _stop_local_service(process)

    if daily["schema_version"] != "crypto-spot-daily-market-chart-v1":
        raise RuntimeError("BTC daily chart is not served by the generic crypto spot contract")
    if daily["schema_version"] != btc_descriptor["chart_schema_version"]:
        raise RuntimeError("descriptor and daily response schema versions disagree")
    if daily["asset_id"] != _BTC_ASSET_ID or daily["source_id"] != _BTC_DAILY_SOURCE_ID:
        raise RuntimeError("BTC daily chart identity is not explicit and exact")
    if daily["volume_unit"] != "BTC":
        raise RuntimeError("BTC daily chart volume unit is not preserved")
    if not daily["points"]:
        raise RuntimeError("the seeded daily sessions were not served")
    _assert_no_retired_schema(daily, "daily chart")

    if intraday["schema_version"] != "crypto-spot-intraday-chart-v1":
        raise RuntimeError("BTC intraday chart is not served by the generic crypto spot contract")
    if intraday["schema_version"] != btc_descriptor["intraday_schema_version"]:
        raise RuntimeError("descriptor and intraday response schema versions disagree")
    if intraday["asset_id"] != _BTC_ASSET_ID or intraday["source_id"] != _BTC_MINUTE_SOURCE_ID:
        raise RuntimeError("BTC intraday chart does not reflect the requested identity")
    if not intraday["bars"]:
        raise RuntimeError("the seeded minute evidence was not served")
    _assert_no_retired_schema(intraday, "intraday chart")

    if foreign_status != 400:
        raise RuntimeError("an asset without market.minute_bars did not fail closed")
    if foreign_error.get("error", {}).get("code") != "invalid_request":
        raise RuntimeError("the fail-closed response is not the bounded invalid_request error")

    return [
        {
            "phase": "loopback-generic-btc-daily-chart",
            "schema_version": daily["schema_version"],
            "asset_id": daily["asset_id"],
            "source_id": daily["source_id"],
            "volume_unit": daily["volume_unit"],
            "displayed_points": len(daily["points"]),
            "descriptor_matches_response": True,
        },
        {
            "phase": "loopback-identity-bearing-btc-intraday",
            "schema_version": intraday["schema_version"],
            "asset_id": intraday["asset_id"],
            "source_id": intraday["source_id"],
            "interval": intraday["interval"],
            "displayed_bars": len(intraday["bars"]),
            "descriptor_matches_response": True,
        },
        {
            "phase": "loopback-intraday-without-capability-fails-closed",
            "asset_id": "crypto:eth-usd",
            "status": foreign_status,
            "error_code": foreign_error.get("error", {}).get("code"),
        },
    ]


def main() -> int:
    """Run the loopback smoke on temporary roots and print one compact summary."""
    with tempfile.TemporaryDirectory(prefix="investment-analyst-smoke-") as temporary:
        temporary_root = Path(temporary).resolve()
        workspace_root = temporary_root / "workspace"
        WorkspaceService(environ={}, home=temporary_root).initialize(explicit_path=workspace_root)
        seeded = _seed_coinbase_evidence(workspace_root)
        if not workspace_root.resolve().is_relative_to(temporary_root):
            raise RuntimeError("the smoke must only use a temporary workspace")
        summary = {
            "temporary_workspace": True,
            "permanent_workspace_opened": False,
            "outbound_network_used": False,
            "seeded_evidence": seeded,
            "phases": _loopback_phases(workspace_root),
        }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
