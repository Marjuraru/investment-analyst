"""CLI integration tests for local SMV/BVL registry entry points."""

import json
import os
import subprocess
import sys
from pathlib import Path

from investment_analyst.storage import LocalStorage, StoragePaths

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
QUERY_SCRIPT = REPOSITORY_ROOT / "scripts" / "query_bvl_registry.py"
REFRESH_SCRIPT = REPOSITORY_ROOT / "scripts" / "refresh_bvl_registry.py"
ASSET_ID = "equity:pe:bvl:cverdec1"


def _run(args: list[str], *, cwd: Path):
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=dict(os.environ),
        text=True,
        capture_output=True,
        check=False,
    )


def test_query_script_is_read_only_cwd_independent_and_lists_configured_identity(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    with LocalStorage(StoragePaths.from_root(storage_root)):
        pass

    completed = _run(
        [
            str(QUERY_SCRIPT),
            "--root",
            str(storage_root),
            "--known-at",
            "2026-07-29T12:00:00Z",
        ],
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "bvl-registry-universe-v1"
    assert len(payload["assets"]) == 6
    assert {item["status"] for item in payload["assets"]} == {"not_imported"}
    assert payload["traceability_verified"] is True


def test_refresh_script_rejects_duplicate_batch_before_network_access(tmp_path: Path) -> None:
    completed = _run(
        [
            str(REFRESH_SCRIPT),
            "--root",
            str(tmp_path / "unused"),
            "--asset-id",
            ASSET_ID,
            "--asset-id",
            ASSET_ID,
        ],
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "asset_ids must be unique and sorted" in completed.stderr
