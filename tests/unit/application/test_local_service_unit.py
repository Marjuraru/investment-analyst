"""Tests for the generated persistent local systemd user service."""

from datetime import date, time
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_analyst.application.aapl_scheduler import AaplDailyScheduleConfig
from investment_analyst.application.local_service_unit import (
    AaplLocalServiceUnitConfig,
    render_local_service_unit,
    write_local_service_unit,
)


def _config(tmp_path: Path, *, scheduled: bool = True) -> AaplLocalServiceUnitConfig:
    schedule = None
    if scheduled:
        schedule = AaplDailyScheduleConfig(
            timezone="America/Lima",
            run_at=time(hour=7),
            market_start=date(2025, 1, 1),
            market_end_lag_days=1,
        )
    return AaplLocalServiceUnitConfig(
        repository_root=tmp_path / "repository%",
        environment_file=tmp_path / "private.env",
        workspace_root=tmp_path / "workspace",
        port=8765,
        schedule=schedule,
    )


def test_service_unit_quotes_paths_and_contains_restart_and_schedule(tmp_path: Path) -> None:
    document = render_local_service_unit(_config(tmp_path))

    assert "WorkingDirectory=/" in document
    assert "repository%%" in document
    assert "EnvironmentFile=/" in document
    assert '"--schedule-at" "07:00"' in document
    assert '"--market-end-lag-days" "1"' in document
    assert "Restart=on-failure" in document
    assert "UMask=0077" in document
    assert "NoNewPrivileges=true" in document
    assert "PrivateTmp=true" in document
    assert "ALPACA_API_SECRET" not in document


def test_service_unit_can_disable_scheduler_and_writes_atomically(tmp_path: Path) -> None:
    document = render_local_service_unit(_config(tmp_path, scheduled=False))
    target = tmp_path / "systemd" / "investment-analyst.service"

    written = write_local_service_unit(target, document)

    assert written == target
    assert target.read_text(encoding="utf-8") == document
    assert '"--no-scheduler"' in document
    assert list(target.parent.glob(".*.tmp")) == []


def test_service_unit_rejects_relative_paths_and_boolean_port(tmp_path: Path) -> None:
    values = _config(tmp_path).model_dump()
    values["repository_root"] = Path("relative")
    with pytest.raises(ValidationError, match="must be absolute"):
        AaplLocalServiceUnitConfig(**values)

    values = _config(tmp_path).model_dump()
    values["port"] = True
    with pytest.raises(ValidationError, match="must be an integer"):
        AaplLocalServiceUnitConfig(**values)

    values = _config(tmp_path).model_dump()
    values["environment_file"] = tmp_path / "private env"
    with pytest.raises(ValidationError, match="must not contain whitespace"):
        AaplLocalServiceUnitConfig(**values)
