"""Tests for the generated persistent local systemd user service."""

import runpy
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

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _script_parser(script_name: str):  # type: ignore[no-untyped-def]
    namespace = runpy.run_path(
        str(_PROJECT_ROOT / "scripts" / script_name),
        run_name=f"_test_{script_name.replace('.', '_')}",
    )
    return namespace["_parser"]


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
    assert "Type=notify" in document
    assert "NotifyAccess=main" in document
    assert "TimeoutStartSec=120s" in document
    assert "Restart=on-failure" in document
    assert "UMask=0077" in document
    assert "NoNewPrivileges=true" in document
    assert "PrivateTmp=true" in document
    assert "ALPACA_API_SECRET" not in document


def test_unit_render_without_memory_config_is_byte_identical_to_base(tmp_path: Path) -> None:
    document = render_local_service_unit(_config(tmp_path))
    repository = tmp_path / "repository%"
    repository_unit = str(repository).replace("%", "%%")
    workspace = tmp_path / "workspace"
    expected = "\n".join(
        (
            "[Unit]",
            "Description=Investment Analyst local interface and watchlist scheduler",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=notify",
            "NotifyAccess=main",
            "TimeoutStartSec=120s",
            f"WorkingDirectory={repository_unit}",
            f"EnvironmentFile={tmp_path / 'private.env'}",
            (
                f'ExecStart="{repository_unit}/.venv/bin/python" '
                f'"{repository_unit}/scripts/serve_investment_analyst.py" '
                f'"--workspace" "{workspace}" "--port" "8765" "--schedule-at" "07:00" '
                '"--timezone" "America/Lima" "--market-start" "2025-01-01" '
                '"--market-end-lag-days" "1" "--fundamental-frequency" "quarterly" '
                '"--refresh-mode" "auto"'
            ),
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

    assert document == expected


def test_unit_render_emits_memory_max_accounting_and_start_limit(tmp_path: Path) -> None:
    values = _config(tmp_path).model_dump()
    values["memory_max_bytes"] = 256 * 1024 * 1024
    values["memory_ceiling_bytes"] = 128 * 1024 * 1024

    document = render_local_service_unit(AaplLocalServiceUnitConfig(**values))

    assert "StartLimitIntervalSec=900s" in document
    assert "StartLimitBurst=3" in document
    assert "MemoryAccounting=yes" in document
    assert "MemoryMax=268435456" in document
    assert '"--memory-ceiling-mb" "128"' in document


def test_process_ceiling_must_be_below_cgroup_max(tmp_path: Path) -> None:
    values = _config(tmp_path).model_dump()
    values["memory_max_bytes"] = 128 * 1024 * 1024
    values["memory_ceiling_bytes"] = 128 * 1024 * 1024

    with pytest.raises(ValidationError, match="below memory max"):
        AaplLocalServiceUnitConfig(**values)


@pytest.mark.parametrize(
    ("parser_script", "option"),
    [
        ("serve_investment_analyst.py", "--memory-ceiling-mb"),
        ("install_local_service.py", "--memory-ceiling-mb"),
        ("install_local_service.py", "--memory-max-mb"),
    ],
)
@pytest.mark.parametrize("value", ("0", "-1"))
def test_non_positive_memory_ceiling_fails_closed(
    parser_script: str,
    option: str,
    value: str,
) -> None:
    with pytest.raises(SystemExit) as error:
        _script_parser(parser_script)().parse_args([option, value])
    assert error.value.code == 2


def test_service_unit_can_disable_scheduler_and_writes_atomically(tmp_path: Path) -> None:
    document = render_local_service_unit(_config(tmp_path, scheduled=False))
    target = tmp_path / "systemd" / "investment-analyst.service"

    written = write_local_service_unit(target, document)

    assert written == target
    assert target.read_text(encoding="utf-8") == document
    assert '"--no-scheduler"' in document
    assert list(target.parent.glob(".*.tmp")) == []


def test_service_unit_can_restrict_watchlist_and_disable_intraday(tmp_path: Path) -> None:
    values = _config(tmp_path).model_dump()
    values["scheduled_asset_ids"] = (
        "crypto:btc-usd",
        "equity:us:amd",
    )
    values["schedule_intraday"] = False

    document = render_local_service_unit(AaplLocalServiceUnitConfig(**values))

    assert document.count('"--schedule-asset"') == 2
    assert '"crypto:btc-usd"' in document
    assert '"equity:us:amd"' in document
    assert '"--no-schedule-intraday"' in document


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
