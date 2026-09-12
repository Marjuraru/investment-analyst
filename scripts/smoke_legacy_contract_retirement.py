#!/usr/bin/env python3
"""Temporary smoke for the compatible retirement of the legacy Apple contracts.

Phase 1 boots ``scripts/serve_investment_analyst.py`` on a loopback port over a
temporary workspace and its state root, seeds no provider traffic, and reads the Apple
market chart from the running server to prove it is served by the generic
``listed-market-chart-v1`` contract declared by the universe descriptor.

Phase 2 replays a persisted legacy ``COMPLETE_REFRESH`` payload through the real durable
queue and the real controller seam. The payload has no ``asset_id``, so it is adapted to
the Apple identity it had by contract, resumed after an interruption, and executed
without the state file being rewritten. Provider traffic is replaced by a local runner
double so the smoke never reaches the network; the queue, the adaptation, the writer
mutex, and the persisted state are the production implementations.

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
from uuid import UUID, uuid4

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticStatus,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplMarketRefreshMode,
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.aapl_daily_runner import AaplDailyRunner
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.manual_operations import (
    ManualOperationKind,
    ManualOperationQueue,
    ManualOperationRequest,
    ManualOperationState,
    ManualOperationStateStore,
    ManualOperationStatus,
)
from investment_analyst.application.operational_models import (
    AaplDailyRunCounts,
    AaplDailyRunState,
    AaplDailyRunStatus,
    LegacyCompleteRefreshSnapshotAdapter,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.frontend.local_web import AaplLocalController, AaplLocalWebApplication
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_normalizer import (
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials, AlpacaStockBar
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
_SERVE_SCRIPT = _REPOSITORY_ROOT / "scripts" / "serve_investment_analyst.py"
_AAPL_ASSET_ID = "equity:us:aapl"
_AAPL_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_MANUAL_OPERATION_STATE_FILE = "manual_operation_state_v1.json"
_FIRST_SESSION = datetime(2026, 5, 1, tzinfo=UTC)
_SESSIONS = 30
_RETRIEVED_AT = datetime(2026, 6, 5, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 6, 6, tzinfo=UTC)
_LEGACY_PAYLOAD: dict[str, object] = {
    "market_start": "2026-05-01",
    "market_end": "2026-06-04",
    "fundamental_frequency": "quarterly",
    "refresh_mode": "auto",
    "requested_known_at": _KNOWN_AT.isoformat(),
    "require_complete": True,
}
_CREDENTIALS = AlpacaCredentials(
    api_key="smoke-loopback-placeholder",
    secret_key="smoke-loopback-placeholder",
)
_SEC_IDENTITY = SecEdgarIdentity("investment-analyst smoke smoke@example.invalid")


class _LocalRefreshRunner(AaplDailyRunner):
    """Record the typed bootstrap request without provider traffic."""

    def __init__(
        self,
        application: InvestmentAnalystApplication,
        workspace_service: WorkspaceService,
    ) -> None:
        super().__init__(application, workspace_service)
        self.requests: list[AaplWorkspaceBootstrapRequest] = []

    def run(
        self,
        request: AaplWorkspaceBootstrapRequest,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
    ) -> AaplDailyRunState:
        """Return one deterministic local outcome for the adapted request."""
        del alpaca_credentials, sec_identity
        paths = self._workspace_service.resolve(workspace)
        inspection = self._workspace_service.inspect(paths.root)
        moment = self._clock()
        self.requests.append(request)
        return AaplDailyRunState(
            run_id=uuid4(),
            status=AaplDailyRunStatus.SUCCEEDED,
            workspace_root=paths.root,
            workspace_id=inspection.workspace_id,
            request=request,
            started_at=moment,
            completed_at=moment,
            effective_known_at=moment,
            refresh_mode=AaplMarketRefreshMode.ALREADY_CURRENT,
            overall_status=ConsolidatedDiagnosticStatus.COMPLETE,
            counts=AaplDailyRunCounts(
                raw_records_created=0,
                raw_records_reused=0,
                observations_created=0,
                observations_reused=0,
                metric_results_created=0,
                metric_results_reused=0,
                diagnostics_created=0,
                diagnostics_reused=0,
            ),
            traceability_verified=True,
        )


def _seed_apple_sessions(workspace_root: Path) -> None:
    """Persist synthetic Apple daily bars without any provider traffic."""
    storage_paths = StoragePaths.from_root(workspace_root / "storage")
    normalized_at = _RETRIEVED_AT + timedelta(minutes=1)
    with LocalStorage(storage_paths) as storage:
        for index in range(_SESSIONS):
            timestamp = _FIRST_SESSION + timedelta(days=index)
            close = Decimal("209") + Decimal(index)
            bar = AlpacaStockBar(
                symbol="AAPL",
                timestamp=timestamp,
                open=Decimal("205") + Decimal(index),
                high=Decimal("211") + Decimal(index),
                low=Decimal("203") + Decimal(index),
                close=close,
                volume=Decimal("50000000"),
                trade_count=Decimal("650000"),
                vwap=Decimal("207") + Decimal(index),
                raw_values={
                    "t": timestamp.isoformat().replace("+00:00", "Z"),
                    "o": str(Decimal("205") + Decimal(index)),
                    "h": str(Decimal("211") + Decimal(index)),
                    "l": str(Decimal("203") + Decimal(index)),
                    "c": str(close),
                    "v": "50000000",
                    "n": "650000",
                    "vw": str(Decimal("207") + Decimal(index)),
                },
            )
            raw_record = bar_to_raw_record(
                bar,
                retrieved_at=_RETRIEVED_AT,
                request_url="https://data.alpaca.markets/smoke",
            )
            storage.raw_records.save(raw_record)
            for observation in bar_to_observations(
                bar,
                raw_record,
                normalized_at=normalized_at,
            ):
                storage.observations.save(observation)


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
            "ALPACA_API_KEY": _CREDENTIALS.api_key,
            "ALPACA_API_SECRET": _CREDENTIALS.secret_key,
            "SEC_USER_AGENT": _SEC_IDENTITY.user_agent,
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


def _loopback_chart_phase(workspace_root: Path) -> dict[str, object]:
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    process = _start_local_service(workspace_root, port)
    try:
        universe = _await_loopback(base_url, process)
        descriptor = next(item for item in universe["assets"] if item["asset_id"] == _AAPL_ASSET_ID)
        chart = _read_json(
            f"{base_url}/api/market-chart"
            f"?asset_id=equity%3Aus%3Aaapl&known_at={_KNOWN_AT.isoformat().replace('+00:00', 'Z')}"
        )
    finally:
        _stop_local_service(process)

    if chart["schema_version"] != "listed-market-chart-v1":
        raise RuntimeError("Apple chart is not served by the generic listed contract")
    if chart["schema_version"] != descriptor["chart_schema_version"]:
        raise RuntimeError("descriptor and response schema versions disagree")
    if chart["asset_id"] != _AAPL_ASSET_ID or chart["source_id"] != _AAPL_SOURCE_ID:
        raise RuntimeError("Apple chart identity is not explicit and exact")
    if "aapl-market-chart-v5" in json.dumps(chart):
        raise RuntimeError("retired Apple schema version is still emitted")
    if not chart["points"]:
        raise RuntimeError("the seeded Apple sessions were not served")
    return {
        "phase": "loopback-generic-chart",
        "schema_version": chart["schema_version"],
        "asset_id": chart["asset_id"],
        "source_id": chart["source_id"],
        "displayed_points": len(chart["points"]),
        "descriptor_matches_response": True,
    }


def _legacy_round_trip_phase(workspace_root: Path) -> dict[str, object]:
    state_root = workspace_root / "state"
    state_path = state_root / _MANUAL_OPERATION_STATE_FILE
    store = ManualOperationStateStore(state_path)
    request = ManualOperationRequest(
        operation_kind=ManualOperationKind.COMPLETE_REFRESH,
        payload=dict(_LEGACY_PAYLOAD),
    )
    submitted_at = datetime(2026, 6, 5, tzinfo=UTC)
    store.write(
        ManualOperationState(
            operation_id=UUID("00000000-0000-0000-0000-0000000000f6"),
            fingerprint=request.fingerprint,
            request=request,
            status=ManualOperationStatus.RUNNING,
            submitted_at=submitted_at,
            started_at=submitted_at + timedelta(seconds=1),
        )
    )
    persisted = state_path.read_bytes()

    document = store.load()
    if state_path.read_bytes() != persisted:
        raise RuntimeError("reading the persisted state rewrote the state file")
    stored = document.operations[0]
    if "asset_id" in stored.request.payload:
        raise RuntimeError("the persisted legacy payload unexpectedly carries an identity")
    adapted = LegacyCompleteRefreshSnapshotAdapter.adapt(stored.request.payload)
    if adapted.asset_id != _AAPL_ASSET_ID:
        raise RuntimeError("the legacy payload was not adapted to its contract identity")

    runtime = ApplicationRuntime.create_default(
        workspace_service=WorkspaceService(environ={}, home=workspace_root.parent)
    )
    application = InvestmentAnalystApplication(runtime)
    runner = _LocalRefreshRunner(application, runtime.workspace_service)
    controller = AaplLocalController(
        runner,
        application,
        workspace=workspace_root,
        alpaca_credentials=_CREDENTIALS,
        sec_identity=_SEC_IDENTITY,
    )
    web = AaplLocalWebApplication(controller, None)
    queue = ManualOperationQueue(
        ManualOperationStateStore(state_path),
        web.execute_manual_operation,
    )
    web.set_manual_operations(queue)

    recovered = store.load().operations[0]
    if recovered.status is not ManualOperationStatus.QUEUED or recovered.recovery_count != 1:
        raise RuntimeError("the interrupted legacy operation was not resumed")
    completed = queue.run_next()
    if completed is None or completed.status is not ManualOperationStatus.SUCCEEDED:
        raise RuntimeError("the resumed legacy operation did not complete")
    if [item.asset_id for item in runner.requests] != [_AAPL_ASSET_ID]:
        raise RuntimeError("the resumed execution did not carry the explicit Apple identity")
    if queue.run_next() is not None:
        raise RuntimeError("the completed operation was executed twice")

    persisted_after = json.loads(state_path.read_text(encoding="utf-8"))["operations"][0]
    if persisted_after["request"]["payload"] != _LEGACY_PAYLOAD:
        raise RuntimeError("the stored legacy payload was migrated or rewritten")
    if persisted_after["fingerprint"] != request.fingerprint:
        raise RuntimeError("the stored deduplication fingerprint changed")
    return {
        "phase": "legacy-to-generic-round-trip",
        "adapted_asset_id": adapted.asset_id,
        "recovery_count": recovered.recovery_count,
        "final_status": completed.status.value,
        "state_file_rewritten_during_read": False,
        "stored_payload_migrated": False,
        "fingerprint_preserved": True,
    }


def main() -> int:
    """Run both smoke phases on temporary roots and print one compact summary."""
    with tempfile.TemporaryDirectory(prefix="investment-analyst-smoke-") as temporary:
        temporary_root = Path(temporary).resolve()
        workspace_root = temporary_root / "workspace"
        WorkspaceService(environ={}, home=temporary_root).initialize(explicit_path=workspace_root)
        _seed_apple_sessions(workspace_root)
        if not workspace_root.resolve().is_relative_to(temporary_root):
            raise RuntimeError("the smoke must only use a temporary workspace")
        summary = {
            "temporary_workspace": True,
            "permanent_workspace_opened": False,
            "outbound_network_used": False,
            "phases": [
                _loopback_chart_phase(workspace_root),
                _legacy_round_trip_phase(workspace_root),
            ],
        }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
