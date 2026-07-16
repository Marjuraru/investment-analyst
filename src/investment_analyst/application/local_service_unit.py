"""Safe rendering and atomic installation of the local systemd user service."""

import os
from pathlib import Path
from uuid import uuid4

from pydantic import ConfigDict, Field, field_validator

from investment_analyst.application.aapl_scheduler import AaplDailyScheduleConfig
from investment_analyst.core.models.base import ContractModel

LOCAL_SERVICE_UNIT_NAME = "investment-analyst.service"


class AaplLocalServiceUnitConfig(ContractModel):
    """Explicit filesystem and runtime inputs for one generated user unit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_root: Path
    environment_file: Path
    workspace_root: Path
    port: int = Field(default=8765, ge=1, le=65_535)
    schedule: AaplDailyScheduleConfig | None

    @field_validator(
        "repository_root",
        "environment_file",
        "workspace_root",
        mode="before",
    )
    @classmethod
    def normalize_absolute_path(cls, value: object) -> object:
        """Normalize explicit paths without accepting ambiguous relative locations."""
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("service paths must not be empty")
            path = Path(value).expanduser()
        elif isinstance(value, Path):
            path = value.expanduser()
        else:
            return value
        if not path.is_absolute():
            raise ValueError("service paths must be absolute")
        resolved = path.resolve(strict=False)
        if any(character in str(resolved) for character in ("\x00", "\n", "\r")):
            raise ValueError("service paths contain unsupported control characters")
        if any(character.isspace() for character in str(resolved)):
            raise ValueError("service paths must not contain whitespace")
        if any(character in str(resolved) for character in ('"', "'", "\\")):
            raise ValueError("service paths contain unsupported quoting characters")
        return resolved

    @field_validator("port", mode="before")
    @classmethod
    def reject_boolean_port(cls, value: object) -> object:
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError("port must be an integer")
        return value


def render_local_service_unit(config: AaplLocalServiceUnitConfig) -> str:
    """Render a fixed user service without shell interpolation or embedded secrets."""
    python = config.repository_root / ".venv" / "bin" / "python"
    script = config.repository_root / "scripts" / "serve_investment_analyst.py"
    arguments = [
        str(python),
        str(script),
        "--workspace",
        str(config.workspace_root),
        "--port",
        str(config.port),
    ]
    if config.schedule is None:
        arguments.append("--no-scheduler")
    else:
        arguments.extend(
            [
                "--schedule-at",
                config.schedule.run_at.strftime("%H:%M"),
                "--timezone",
                config.schedule.timezone,
                "--market-start",
                config.schedule.market_start.isoformat(),
                "--market-end-lag-days",
                str(config.schedule.market_end_lag_days),
                "--fundamental-frequency",
                config.schedule.fundamental_frequency.value,
                "--refresh-mode",
                config.schedule.refresh_mode.value,
            ]
        )
        if not config.schedule.require_complete:
            arguments.append("--allow-partial")
    command = " ".join(_quote_systemd(item) for item in arguments)
    return "\n".join(
        (
            "[Unit]",
            "Description=Investment Analyst local Apple interface and scheduler",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={_unit_path(str(config.repository_root))}",
            f"EnvironmentFile={_unit_path(str(config.environment_file))}",
            f"ExecStart={command}",
            "Restart=on-failure",
            "RestartSec=5s",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )
    )


def write_local_service_unit(target: Path, document: str) -> Path:
    """Atomically write a private unit file chosen explicitly by the caller."""
    destination = target.expanduser().resolve(strict=False)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n", closefd=True) as stream:
            descriptor = None
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return destination


def _quote_systemd(value: str) -> str:
    """Quote one systemd argument and disable percent-specifier expansion."""
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unit_path(value: str) -> str:
    """Disable percent specifiers in a validated unquoted unit path."""
    return value.replace("%", "%%")
