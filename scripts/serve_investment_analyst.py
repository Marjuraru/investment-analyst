#!/usr/bin/env python3
"""Serve the loopback analysis UI and its optional Apple daily scheduler."""

import argparse
import os
import signal
import sys
import threading
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import FrameType
from uuid import uuid4

from investment_analyst.application.aapl_bootstrap_models import AaplRefreshMode
from investment_analyst.application.aapl_daily_runner import AaplDailyRunner
from investment_analyst.application.aapl_scheduler import (
    AaplDailyScheduleConfig,
    AaplDailyScheduler,
    AaplDailyScheduleStateStore,
    AaplLocalServiceAlreadyRunningError,
    AaplLocalServiceLock,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.application.runtime import ApplicationRuntime, ApplicationRuntimeError
from investment_analyst.core.models import DataFrequency
from investment_analyst.frontend.local_web import (
    AaplLocalController,
    AaplLocalHttpServer,
    AaplLocalWebApplication,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError

_SCHEDULE_STATE_FILE = "aapl_daily_schedule_state.json"
_SERVICE_LOCK_FILE = "aapl_local_service.lock"


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
    return parser


def _credentials() -> tuple[AlpacaCredentials, SecEdgarIdentity] | None:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_API_SECRET", "")
    sec_user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not api_key.strip() or not secret_key.strip() or not sec_user_agent.strip():
        return None
    return (
        AlpacaCredentials(api_key=api_key, secret_key=secret_key),
        SecEdgarIdentity(sec_user_agent),
    )


def _serve(
    arguments: argparse.Namespace, credentials: tuple[AlpacaCredentials, SecEdgarIdentity]
) -> int:
    runtime = ApplicationRuntime.create_default()
    paths = runtime.workspace_service.resolve(arguments.workspace)
    runtime.workspace_service.inspect(paths.root)
    application = InvestmentAnalystApplication(runtime)
    runner = AaplDailyRunner(application, runtime.workspace_service)
    alpaca_credentials, sec_identity = credentials
    controller = AaplLocalController(
        runner,
        application,
        workspace=paths.root,
        alpaca_credentials=alpaca_credentials,
        sec_identity=sec_identity,
    )

    scheduler: AaplDailyScheduler | None = None
    if not arguments.no_scheduler:
        scheduler = AaplDailyScheduler(
            AaplDailyScheduleConfig(
                timezone=arguments.timezone,
                run_at=arguments.schedule_at,
                market_start=arguments.market_start,
                market_end_lag_days=arguments.market_end_lag_days,
                fundamental_frequency=arguments.fundamental_frequency,
                refresh_mode=(
                    AaplRefreshMode.FULL
                    if arguments.refresh_mode == "full"
                    else AaplRefreshMode.AUTO
                ),
                require_complete=not arguments.allow_partial,
            ),
            AaplDailyScheduleStateStore(paths.state_root / _SCHEDULE_STATE_FILE),
            controller.run_request,
        )

    server = AaplLocalHttpServer(
        ("127.0.0.1", arguments.port),
        AaplLocalWebApplication(controller, scheduler),
    )
    stop_event = threading.Event()
    scheduler_thread: threading.Thread | None = None
    if scheduler is not None:
        scheduler_thread = threading.Thread(
            target=scheduler.run_forever,
            args=(stop_event,),
            kwargs={"error_handler": lambda message: print(message, file=sys.stderr)},
            name="aapl-daily-scheduler",
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

    service_started_at = datetime.now(UTC).isoformat()
    lock = AaplLocalServiceLock(
        paths.state_root / _SERVICE_LOCK_FILE,
        service_id=uuid4(),
        started_at=service_started_at,
    )
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    with lock:
        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        if scheduler_thread is not None:
            scheduler_thread.start()
        print(
            f"Investment Analyst available at http://127.0.0.1:{arguments.port}",
            flush=True,
        )
        print("Press Ctrl+C to stop the local service.", flush=True)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            request_shutdown(signal.SIGINT, None)
        finally:
            stop_event.set()
            server.server_close()
            if scheduler_thread is not None:
                scheduler_thread.join(timeout=5)
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
