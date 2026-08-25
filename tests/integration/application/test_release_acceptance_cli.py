"""Integration tests for the one-shot release acceptance CLI."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts.observe_release_acceptance import main

SHA = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
TREE = "b6f8115ffdddb0915cae50736dbc821c5355d3ac"


def _arguments(tmp_path: Path) -> list[str]:
    return [
        "--sha",
        SHA,
        "--tree",
        TREE,
        "--workspace-root",
        str(tmp_path / "workspace"),
        "--jsonl",
        str(tmp_path / "scratch" / "observations.jsonl"),
        "--summary",
        str(tmp_path / "scratch" / "summary.json"),
        "--duration-seconds",
        "0",
        "--interval-seconds",
        "0.1",
    ]


def test_cli_reports_pass_without_exposing_paths_or_payloads(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI returns a compact result and delegates only to the observer."""
    summary = {
        "schema_version": "release-acceptance-observation-v1",
        "status": "PASS",
        "sample_count": 1,
        "failures": [],
    }
    with patch(
        "scripts.observe_release_acceptance.observe_release_acceptance", return_value=summary
    ):
        assert main(_arguments(tmp_path)) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "schema_version": "release-acceptance-observation-v1",
        "status": "PASS",
        "sample_count": 1,
        "failures": [],
    }
    assert str(tmp_path) not in output


def test_cli_accepts_finite_duration_and_rejects_invalid_interval(tmp_path: Path) -> None:
    """Duration is operator-selected; technical interval bounds remain fail-closed."""
    duration_args = _arguments(tmp_path)
    duration_args[duration_args.index("--duration-seconds") + 1] = "999999"
    summary = {
        "schema_version": "release-acceptance-observation-v1",
        "status": "PASS",
        "sample_count": 1,
        "failures": [],
    }
    with patch(
        "scripts.observe_release_acceptance.observe_release_acceptance", return_value=summary
    ):
        assert main(duration_args) == 0

    invalid_interval_args = _arguments(tmp_path)
    invalid_interval_args[invalid_interval_args.index("--interval-seconds") + 1] = "0"
    with pytest.raises(SystemExit) as invalid_interval:
        main(invalid_interval_args)
    assert invalid_interval.value.code == 2
