#!/usr/bin/env python3
"""Serve the loopback analysis UI and its optional watchlist scheduler."""

import argparse
import contextlib
import os
import signal
import sys
import threading
import time as time_module
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import FrameType
from uuid import uuid4

from investment_analyst.alerts.analytical_backtest import AnalyticalBacktestService
from investment_analyst.alerts.analytical_monitor import AnalyticalScreeningMonitor
from investment_analyst.alerts.analytical_rule_catalog import INITIAL_ANALYTICAL_RULES
from investment_analyst.alerts.analytical_rule_registry import (
    AnalyticalRuleRegistryStore,
)
from investment_analyst.alerts.analytical_state import AnalyticalScreeningStateStore
from investment_analyst.alerts.candidate_notifications import (
    CandidateNotificationMonitor,
    CandidateNotificationStore,
)
from investment_analyst.application.aapl_bootstrap_models import AaplRefreshMode
from investment_analyst.application.aapl_daily_runner import AaplDailyRunner
from investment_analyst.application.aapl_scheduler import (
    AaplLocalServiceAlreadyRunningError,
    AaplLocalServiceLock,
)
from investment_analyst.application.asset_preferences import (
    AssetPreferencesError,
    AssetPreferencesService,
    AssetPreferencesStore,
    cli_seed_asset_preferences,
    effective_asset_preferences,
    scheduled_available_asset_ids,
)
from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesRefreshRequest,
    CryptoDerivativesRefreshSummary,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.manual_operations import (
    ManualOperationQueue,
    ManualOperationStateStore,
)
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetScheduleStateStore,
    RegisteredScheduledJob,
)
from investment_analyst.application.operational_alerts import (
    OperationalAlertMonitor,
    OperationalAlertStateStore,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
    StorageLocationRequest,
)
from investment_analyst.application.runtime_lifecycle import notify_ready, wait_for_overview_ready
from investment_analyst.application.scheduled_observers import ScheduledJobObserverChain
from investment_analyst.core.models import DataFrequency
from investment_analyst.frontend.local_schedule_jobs import (
    LocalWatchlistScheduleConfig,
    build_local_watchlist_jobs,
)
from investment_analyst.frontend.local_web import (
    AaplLocalController,
    AaplLocalHttpServer,
    AaplLocalWebApplication,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.macro.fred_alfred import FredApiKey
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError

_SCHEDULE_STATE_FILE = "multi_asset_schedule_state_v1.json"
_ALERT_STATE_FILE = "operational_alert_state_v1.json"
_ANALYTICAL_STATE_FILE = "analytical_screening_state_v1.json"
_NOTIFICATION_OUTBOX_STATE_FILE = "candidate_notification_outbox_state_v1.json"
_ANALYTICAL_RULE_REGISTRY_FILE = "analytical_rule_registry_state_v1.json"
_MANUAL_OPERATION_STATE_FILE = "manual_operation_state_v1.json"
_ASSET_PREFERENCES_STATE_FILE = "asset_preferences_state_v1.json"
_SERVICE_LOCK_FILE = "aapl_local_service.lock"
_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS = 60.0


class _DerivativesLocalController(AaplLocalController):
    """Extend the existing ``AaplLocalController(...)`` application boundary.

    The added scheduler route deliberately reuses the controller's writer mutex.
    """

    def crypto_derivatives_refresh_request(
        self,
        request: CryptoDerivativesRefreshRequest,
    ) -> CryptoDerivativesRefreshSummary:
        with self._writer_lock:
            try:
                return self._application.refresh_crypto_derivatives(
                    request,
                    location=StorageLocationRequest(workspace=self._workspace),
                )
            finally:
                self._refresh_health_snapshot()


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from error


def _time_value(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("schedule-at must use HH:MM") from error
    if parsed.tzinfo is not None or parsed.second != 0 or parsed.microsecond != 0:
        raise argparse.ArgumentTypeError("schedule-at must be a whole local minute")
    return parsed


def _frequency(value: str) -> DataFrequency:
    mapping = {"annual": DataFrequency.ANNUAL, "quarterly": DataFrequency.QUARTERLY}
    try:
        return mapping[value.casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError(
            "fundamental-frequency must be annual or quarterly"
        ) from error


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def _lag(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("market-end-lag-days must be an integer") from error
    if not 0 <= parsed <= 30:
        raise argparse.ArgumentTypeError("market-end-lag-days must be between 0 and 30")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--port", type=_port, default=8765)
    parser.add_argument("--no-scheduler", action="store_true")
    parser.add_argument("--schedule-at", type=_time_value, default=time(hour=7))
    parser.add_argument("--timezone", default="America/Lima")
    parser.add_argument("--market-start", type=_date_value, default=date(2025, 1, 1))
    parser.add_argument("--market-end-lag-days", type=_lag, default=1)
    parser.add_argument(
        "--fundamental-frequency",
        type=_frequency,
        default=DataFrequency.QUARTERLY,
    )
    parser.add_argument("--refresh-mode", choices=("auto", "full"), default="auto")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument(
        "--schedule-asset",
        action="append",
        default=[],
        help="repeat to restrict automatic jobs to explicit catalog asset IDs",
    )
    parser.add_argument("--no-schedule-intraday", action="store_true")
    parser.add_argument("--no-schedule-smv", action="store_true")
    parser.add_argument("--no-schedule-macro", action="store_true")
    return parser


def _credentials() -> tuple[AlpacaCredentials, SecEdgarIdentity, FredApiKey | None] | None:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_API_SECRET", "")
    sec_user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not api_key.strip() or not secret_key.strip() or not sec_user_agent.strip():
        return None
    fred_value = os.environ.get("FRED_API_KEY", "").strip()
    return (
        AlpacaCredentials(api_key=api_key, secret_key=secret_key),
        SecEdgarIdentity(sec_user_agent),
        FredApiKey(fred_value) if fred_value else None,
    )


def _serve(
    arguments: argparse.Namespace,
    credentials: tuple[AlpacaCredentials, SecEdgarIdentity, FredApiKey | None],
) -> int:
    runtime = ApplicationRuntime.create_default()
    paths = runtime.workspace_service.resolve(arguments.workspace)
    lock = AaplLocalServiceLock(
        paths.state_root / _SERVICE_LOCK_FILE,
        service_id=uuid4(),
        started_at=datetime.now(UTC).isoformat(),
    )
    with lock:
        runtime.workspace_service.inspect(paths.root)
        return _serve_after_lock(
            arguments,
            credentials,
            runtime,
            workspace_root=paths.root,
            state_root=paths.state_root,
        )


def _serve_after_lock(
    arguments: argparse.Namespace,
    credentials: tuple[AlpacaCredentials, SecEdgarIdentity, FredApiKey | None],
    runtime: ApplicationRuntime,
    *,
    workspace_root: Path,
    state_root: Path,
) -> int:
    application = InvestmentAnalystApplication(runtime)
    runner = AaplDailyRunner(application, runtime.workspace_service)
    alpaca_credentials, sec_identity, fred_api_key = credentials
    controller = _DerivativesLocalController(
        runner,
        application,
        workspace=workspace_root,
        alpaca_credentials=alpaca_credentials,
        sec_identity=sec_identity,
        fred_api_key=fred_api_key,
    )

    scheduler: MultiAssetScheduler | None = None
    alert_store = OperationalAlertStateStore(state_root / _ALERT_STATE_FILE)
    analytical_store = AnalyticalScreeningStateStore(state_root / _ANALYTICAL_STATE_FILE)
    notification_store = CandidateNotificationStore(state_root / _NOTIFICATION_OUTBOX_STATE_FILE)
    analytical_rule_store = AnalyticalRuleRegistryStore(
        state_root / _ANALYTICAL_RULE_REGISTRY_FILE,
        INITIAL_ANALYTICAL_RULES,
    )
    analytical_backtest = AnalyticalBacktestService(
        runtime,
        workspace_root,
        analytical_rule_store,
    )
    selected_asset_ids = tuple(
        sorted(
            {
                value.strip()
                for value in arguments.schedule_asset
                if isinstance(value, str) and value.strip()
            }
        )
    )
    schedule_config = LocalWatchlistScheduleConfig(
        timezone=arguments.timezone,
        run_at=arguments.schedule_at,
        market_start=arguments.market_start,
        market_end_lag_days=arguments.market_end_lag_days,
        fundamental_frequency=arguments.fundamental_frequency,
        refresh_mode=(
            AaplRefreshMode.FULL if arguments.refresh_mode == "full" else AaplRefreshMode.AUTO
        ),
        selected_asset_ids=selected_asset_ids,
        include_intraday=not arguments.no_schedule_intraday,
        include_smv_registry=not arguments.no_schedule_smv,
        include_macro=fred_api_key is not None and not arguments.no_schedule_macro,
        crypto_derivatives_asset_ids=application.list_crypto_derivatives_assets(),
    )
    preference_store = AssetPreferencesStore(state_root / _ASSET_PREFERENCES_STATE_FILE)
    preference_seed = cli_seed_asset_preferences(
        controller.market_assets(),
        selected_asset_ids,
    )
    initial_preferences = effective_asset_preferences(
        preference_store.load(),
        preference_seed,
    )

    def jobs_for_preferences(
        asset_ids: tuple[str, ...],
    ) -> tuple[RegisteredScheduledJob, ...]:
        return build_local_watchlist_jobs(
            controller,
            controller.market_assets(),
            schedule_config.model_copy(
                update={
                    "selected_asset_ids": asset_ids,
                    "selection_is_explicit": True,
                }
            ),
        )

    if not arguments.no_scheduler:
        scheduled_asset_ids = scheduled_available_asset_ids(
            initial_preferences,
            controller.market_assets(),
        )
        if not scheduled_asset_ids:
            raise ValueError("scheduler-enabled preferences require at least one scheduled asset")
        jobs = jobs_for_preferences(scheduled_asset_ids)
        schedule_store = MultiAssetScheduleStateStore(state_root / _SCHEDULE_STATE_FILE)
        schedule_attempts = schedule_store.load().attempts
        alert_monitor = OperationalAlertMonitor(alert_store)
        alert_monitor.reconcile(schedule_attempts)
        analytical_monitor = AnalyticalScreeningMonitor(
            analytical_store,
            runtime,
            workspace_root,
            analytical_rule_store.rules,
        )
        analytical_monitor.reconcile(schedule_attempts)
        notification_monitor = CandidateNotificationMonitor(notification_store, analytical_store)
        notification_monitor.reconcile()
        scheduler = MultiAssetScheduler(
            jobs,
            schedule_store,
            observer=ScheduledJobObserverChain(
                (
                    alert_monitor,
                    analytical_monitor,
                    notification_monitor,
                )
            ),
        )

    preference_service = AssetPreferencesService(
        preference_store,
        controller.market_assets(),
        preference_seed,
        scheduler=scheduler,
        job_factory=jobs_for_preferences if scheduler is not None else None,
    )

    web_application = AaplLocalWebApplication(
        controller,
        scheduler,
        alert_store,
        analytical_store,
        analytical_rule_store,
        analytical_backtest,
        notification_store=notification_store,
        asset_preferences=preference_service,
    )
    manual_operations = ManualOperationQueue(
        ManualOperationStateStore(state_root / _MANUAL_OPERATION_STATE_FILE),
        web_application.execute_manual_operation,
    )
    web_application.set_manual_operations(manual_operations)
    server = AaplLocalHttpServer(("127.0.0.1", arguments.port), web_application)
    stop_event = threading.Event()
    scheduler_thread: threading.Thread | None = None
    if scheduler is not None:
        scheduler_thread = threading.Thread(
            target=scheduler.run_forever,
            args=(stop_event,),
            kwargs={"error_handler": lambda message: print(message, file=sys.stderr)},
            name="multi-asset-scheduler",
            daemon=True,
        )

    shutdown_started = threading.Event()

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        stop_event.set()
        threading.Thread(
            target=server.shutdown,
            name="local-interface-shutdown",
            daemon=True,
        ).start()

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    with contextlib.nullcontext():
        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="local-interface-accept-loop",
            daemon=True,
        )
        server_thread.start()
        try:
            wait_for_overview_ready(arguments.port)
            notify_ready()
            if scheduler_thread is not None:
                scheduler_thread.start()
            manual_operations.start()
            print(
                f"Investment Analyst available at http://127.0.0.1:{arguments.port}",
                flush=True,
            )
            print("Press Ctrl+C to stop the local service.", flush=True)
            server_thread.join()
        except KeyboardInterrupt:
            request_shutdown(signal.SIGINT, None)
        finally:
            shutdown_deadline = time_module.monotonic() + _SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS
            try:
                stop_event.set()
                manual_operations.stop(
                    timeout=min(5.0, max(shutdown_deadline - time_module.monotonic(), 0.0))
                )
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=max(shutdown_deadline - time_module.monotonic(), 0.0))
                if server_thread.is_alive():
                    raise RuntimeError("local HTTP server did not stop before shutdown deadline")
                if scheduler_thread is not None:
                    scheduler_thread.join(
                        timeout=max(shutdown_deadline - time_module.monotonic(), 0.0)
                    )
                    if scheduler_thread.is_alive():
                        raise RuntimeError(
                            "scheduler did not stop cooperatively before shutdown deadline"
                        )
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGINT, previous_sigint)
    return 0


def main() -> int:
    """Validate local configuration and serve until interrupted."""
    arguments = _parser().parse_args()
    credentials = _credentials()
    if credentials is None:
        print(
            "ALPACA_API_KEY, ALPACA_API_SECRET, and SEC_USER_AGENT are required.",
            file=sys.stderr,
        )
        return 2
    try:
        return _serve(arguments, credentials)
    except AaplLocalServiceAlreadyRunningError as error:
        print(f"local interface already active: {error}", file=sys.stderr)
        return 4
    except (
        AaplOperationalStateError,
        AssetPreferencesError,
        ApplicationRuntimeError,
        OSError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"local interface failed: {error}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001
        print("local interface failed unexpectedly.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
