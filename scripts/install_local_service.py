#!/usr/bin/env python3
"""Generate the private systemd user unit for the local interface."""

import argparse
import re
import stat
import sys
from datetime import date, time
from pathlib import Path

from investment_analyst.application.aapl_bootstrap_models import AaplRefreshMode
from investment_analyst.application.aapl_scheduler import AaplDailyScheduleConfig
from investment_analyst.application.local_service_unit import (
    LOCAL_SERVICE_UNIT_NAME,
    AaplLocalServiceUnitConfig,
    render_local_service_unit,
    write_local_service_unit,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_ENVIRONMENT = frozenset({"ALPACA_API_KEY", "ALPACA_API_SECRET", "SEC_USER_AGENT"})


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-scheduler", action="store_true")
    parser.add_argument("--schedule-at", type=_time_value, default=time(hour=7))
    parser.add_argument("--timezone", default="America/Lima")
    parser.add_argument("--market-start", type=_date_value, default=date(2025, 1, 1))
    parser.add_argument("--market-end-lag-days", type=int, default=1)
    parser.add_argument(
        "--fundamental-frequency",
        type=_frequency,
        default=DataFrequency.QUARTERLY,
    )
    parser.add_argument("--refresh-mode", choices=("auto", "full"), default="auto")
    parser.add_argument("--allow-partial", action="store_true")
    return parser


def _validate_environment_file(path: Path) -> None:
    """Require a private systemd-compatible file with all credential variables."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError("environment file permissions must be 0600 or more restrictive")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError("environment file could not be read") from error
    present: set[str] = set()
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or not _ENVIRONMENT_NAME.fullmatch(name):
            raise ValueError("environment file must contain only NAME=value entries")
        if name in seen:
            raise ValueError("environment file contains a duplicate variable")
        seen.add(name)
        if name in _REQUIRED_ENVIRONMENT and value.strip() not in {"", "''", '""'}:
            present.add(name)
    if present != _REQUIRED_ENVIRONMENT:
        raise ValueError("environment file is missing a required non-empty variable")


def main() -> int:
    """Validate local paths and generate, but do not start, the user service."""
    arguments = _parser().parse_args()
    repository = Path(__file__).resolve().parents[1]
    environment_file = repository / ".env"
    if arguments.env_file is not None:
        environment_file = arguments.env_file.expanduser()
        if not environment_file.is_absolute():
            print("--env-file must be an absolute path", file=sys.stderr)
            return 2
    environment_file = environment_file.resolve(strict=False)
    if not environment_file.is_file():
        print("local service environment file was not found", file=sys.stderr)
        return 2
    python = repository / ".venv" / "bin" / "python"
    server_script = repository / "scripts" / "serve_investment_analyst.py"
    if not python.is_file() or not server_script.is_file():
        print("repository virtual environment or server script was not found", file=sys.stderr)
        return 2
    try:
        _validate_environment_file(environment_file)
        workspace_service = WorkspaceService()
        paths = workspace_service.resolve(arguments.workspace)
        workspace_service.inspect(paths.root)
        schedule = None
        if not arguments.no_scheduler:
            schedule = AaplDailyScheduleConfig(
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
            )
        config = AaplLocalServiceUnitConfig(
            repository_root=repository,
            environment_file=environment_file,
            workspace_root=paths.root,
            port=arguments.port,
            schedule=schedule,
        )
        target = arguments.output
        if target is None:
            target = Path.home() / ".config" / "systemd" / "user" / LOCAL_SERVICE_UNIT_NAME
        elif not target.expanduser().is_absolute():
            raise ValueError("--output must be an absolute path")
        written = write_local_service_unit(target, render_local_service_unit(config))
    except (OSError, ValueError, WorkspaceError) as error:
        print(f"local service unit could not be generated: {error}", file=sys.stderr)
        return 2
    print(f"Generated {written}")
    print("Next commands:")
    print("  systemctl --user daemon-reload")
    print(f"  systemctl --user enable --now {LOCAL_SERVICE_UNIT_NAME}")
    print(f"  systemctl --user status {LOCAL_SERVICE_UNIT_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
