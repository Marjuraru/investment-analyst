#!/usr/bin/env python3
"""Run or inspect the locked, stateful Apple operational workflow."""

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.application.aapl_bootstrap import (
    AaplWorkspaceBootstrapError,
    BootstrapIncompleteError,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplRefreshMode,
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.aapl_daily_runner import (
    AaplDailyRunExecutionError,
    AaplDailyRunner,
)
from investment_analyst.application.operational_state import (
    AaplDailyRunAlreadyRunningError,
    AaplOperationalStateError,
)
from investment_analyst.application.runtime import ApplicationRuntimeError
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from error


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be valid ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _frequency(value: str) -> DataFrequency:
    mapping = {"annual": DataFrequency.ANNUAL, "quarterly": DataFrequency.QUARTERLY}
    try:
        return mapping[value.casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError(
            "fundamental-frequency must be annual or quarterly"
        ) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="execute one locked refresh")
    run.add_argument("--workspace", type=Path)
    run.add_argument("--market-start", required=True, type=_date_value)
    run.add_argument("--market-end", required=True, type=_date_value)
    run.add_argument("--fundamental-frequency", required=True, type=_frequency)
    run.add_argument("--known-at", type=_aware_datetime)
    run.add_argument("--refresh-mode", choices=("auto", "full"), default="auto")
    run.add_argument(
        "--allow-partial",
        action="store_true",
        help="return success when only one independent diagnostic is available",
    )

    health = commands.add_parser("health", help="inspect workspace and latest run")
    health.add_argument("--workspace", type=Path)
    return parser


def _run(arguments: argparse.Namespace) -> int:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_API_SECRET", "")
    sec_user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not api_key.strip() or not secret_key.strip() or not sec_user_agent.strip():
        print(
            "ALPACA_API_KEY, ALPACA_API_SECRET, and SEC_USER_AGENT are required.",
            file=sys.stderr,
        )
        return 2

    try:
        state = AaplDailyRunner.create_default().run(
            AaplWorkspaceBootstrapRequest(
                market_start=arguments.market_start,
                market_end=arguments.market_end,
                fundamental_frequency=arguments.fundamental_frequency,
                refresh_mode=(
                    AaplRefreshMode.FULL
                    if arguments.refresh_mode == "full"
                    else AaplRefreshMode.AUTO
                ),
                requested_known_at=arguments.known_at,
                require_complete=not arguments.allow_partial,
            ),
            workspace=arguments.workspace,
            alpaca_credentials=AlpacaCredentials(api_key=api_key, secret_key=secret_key),
            sec_identity=SecEdgarIdentity(sec_user_agent),
        )
        print(json.dumps(state.to_json_dict(), indent=2, sort_keys=True))
        return 0
    except AaplDailyRunAlreadyRunningError as error:
        print(f"Apple daily run already active: {error}", file=sys.stderr)
        return 4
    except AaplDailyRunExecutionError as error:
        if isinstance(error.cause, BootstrapIncompleteError):
            print(f"Apple daily run incomplete: {error}", file=sys.stderr)
            return 3
        print(f"Apple daily run failed: {error}", file=sys.stderr)
        return 1 if error.failure.category == "unexpected_error" else 2
    except (
        AaplOperationalStateError,
        AaplWorkspaceBootstrapError,
        ApplicationRuntimeError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"Apple daily run failed: {error}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001
        print("Apple daily run failed unexpectedly.", file=sys.stderr)
        return 1


def _health(arguments: argparse.Namespace) -> int:
    try:
        health = AaplDailyRunner.create_default().inspect(workspace=arguments.workspace)
        print(json.dumps(health.to_json_dict(), indent=2, sort_keys=True))
        return 0 if health.status.value in {"ready", "running"} else 3
    except (
        AaplOperationalStateError,
        ApplicationRuntimeError,
        StorageError,
        WorkspaceError,
    ) as error:
        print(f"Apple operational health failed: {error}", file=sys.stderr)
        return 2


def main() -> int:
    """Dispatch one explicit operational command."""
    arguments = _parser().parse_args()
    return _run(arguments) if arguments.command == "run" else _health(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
