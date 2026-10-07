"""Run the isolated incremental-runtime-v2 acceptance probes and save their evidence."""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns

import duckdb

_FAMILY_TESTS = {
    "market_daily": (
        "tests/integration/analytics/test_incremental_runtime_v2_integration.py",
        "tests/integration/analytics/market/test_market_identity_v2_adoption.py",
        "tests/integration/analytics/test_existing_metric_lookup_by_identity.py",
    ),
    "derivatives": (
        "tests/integration/analytics/crypto/test_derivatives_pipeline.py",
        "tests/integration/analytics/crypto/test_derivatives_identity_v2_adoption.py",
    ),
    "fundamentals": (
        "tests/integration/providers/test_sec_fundamental_metric_pipeline_integration.py",
    ),
    "valuation": (
        "tests/unit/analytics/valuation/test_persistence.py",
        "tests/unit/analytics/valuation/test_semantic_identity_v2.py",
    ),
    "institutions_and_weights": (
        "tests/integration/analytics/test_institutional_metric_pipeline_integration.py",
        "tests/integration/analytics/test_institutional_weight_pipeline_integration.py",
        "tests/unit/analytics/cazatiburones/test_institutional_metric_identity.py",
        "tests/unit/analytics/cazatiburones/test_institutional_weight_identity.py",
    ),
    "activity_and_events": (
        "tests/integration/analytics/test_activity_metric_pipeline_integration.py",
        "tests/integration/analytics/test_activity_event_flow.py",
        "tests/integration/analytics/test_institutional_event_service_integration.py",
        "tests/unit/analytics/cazatiburones/test_activity_metric_identity.py",
    ),
    "access_and_recovery": (
        "tests/unit/analytics/test_analytical_access_unit.py",
        "tests/unit/storage/test_workspace_incremental_v2_unit.py",
        "tests/unit/workspace/test_workspace_v2_backup_unit.py",
    ),
}


class SmokeError(RuntimeError):
    """The requested smoke run cannot be completed safely or did not pass."""


def _git_value(root: Path, expression: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", expression],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _output_path(value: str, repository_root: Path) -> Path:
    requested = Path(value).expanduser().absolute()
    for ancestor in (requested, *requested.parents):
        if ancestor.exists() and ancestor.is_symlink():
            raise SmokeError("output path cannot contain a symbolic link")
    target = requested.resolve(strict=False)
    if target.exists() or target.is_symlink():
        raise SmokeError("output path already exists or is a symbolic link")
    if target == repository_root or repository_root in target.parents:
        raise SmokeError("output file must be outside the repository")
    permanent_workspace = Path.home() / ".local/share/investment-analyst/workspaces/default"
    if target == permanent_workspace or permanent_workspace in target.parents:
        raise SmokeError("output file must be outside the permanent workspace")
    if not target.parent.is_dir():
        raise SmokeError("output parent directory must already exist")
    return target


def _rss_high_water_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage if platform.system() == "Darwin" else usage * 1024)


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    started_at: datetime,
) -> dict[str, object]:
    started = perf_counter_ns()
    print(f"SMOKE START {command[1] if len(command) > 1 else command[0]}", flush=True)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    finished_at = datetime.now(UTC)
    is_market_smoke = "scripts/smoke_market_incremental_v2.py" in command
    print(
        f"SMOKE END rc={completed.returncode} "
        f"elapsed_seconds={(perf_counter_ns() - started) / 1_000_000_000:.1f}",
        flush=True,
    )
    result: dict[str, object] = {
        "command": command,
        "started_at": started_at.isoformat(),
        "completed_at": finished_at.isoformat(),
        "elapsed_microseconds": (perf_counter_ns() - started) // 1_000,
        "return_code": completed.returncode,
        "stdout_tail": "" if is_market_smoke else completed.stdout[-16000:],
        "stderr_tail": completed.stderr[-8000:],
        "rss_high_water_bytes": _rss_high_water_bytes(),
    }
    if is_market_smoke:
        try:
            result["market_smoke"] = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise SmokeError("market incremental smoke did not emit valid JSON") from error
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        help="new JSON evidence file outside the repository and workspace",
    )
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    output = _output_path(args.output, repository_root)
    head_sha = _git_value(repository_root, "HEAD")
    tree_sha = _git_value(repository_root, "HEAD^{tree}")
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    started_at = datetime.now(UTC)
    overall_started = perf_counter_ns()
    commands: list[dict[str, object]] = []
    family_results: dict[str, dict[str, object]] = {}

    with tempfile.TemporaryDirectory(prefix="investment-analyst-runtime-v2-") as scratch_text:
        scratch = Path(scratch_text).resolve()
        env = os.environ.copy()
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join((str(repository_root / "src"), str(repository_root))),
                "TMPDIR": str(scratch),
            }
        )
        market_command = [
            sys.executable,
            "scripts/smoke_market_incremental_v2.py",
            "--profile",
            "all",
        ]
        commands.append(
            _run(
                market_command,
                cwd=repository_root,
                env=env,
                started_at=datetime.now(UTC),
            )
        )
        for family, test_paths in _FAMILY_TESTS.items():
            test_command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                *test_paths,
            ]
            result = _run(
                test_command,
                cwd=repository_root,
                env=env,
                started_at=datetime.now(UTC),
            )
            result["family"] = family
            result["test_paths"] = list(test_paths)
            family_results[family] = result
            commands.append(result)

    passed = all(item["return_code"] == 0 for item in commands)
    finished_at = datetime.now(UTC)
    document: dict[str, object] = {
        "schema_version": "incremental-runtime-v2-smoke-v1",
        "status": "PASS" if passed else "FAIL",
        "head_sha": head_sha,
        "tree_sha": tree_sha,
        "code_sha": None if dirty else head_sha,
        "worktree_dirty": dirty,
        "command": [sys.executable, "scripts/smoke_incremental_runtime_v2.py", *sys.argv[1:]],
        "environment": {
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "platform": platform.platform(),
        },
        "started_at": started_at.isoformat(),
        "completed_at": finished_at.isoformat(),
        "elapsed_microseconds": (perf_counter_ns() - overall_started) // 1_000,
        "rss_high_water_bytes": _rss_high_water_bytes(),
        "scratch": "exclusive temporary directory outside the repository, removed after exit",
        "families": family_results,
        "commands": commands,
    }
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, allow_nan=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise SmokeError("could not write the smoke evidence file") from error
    print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
