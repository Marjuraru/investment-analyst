from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = ROOT / "scripts" / "manage_storage_retirement.py"


def _run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--allowed-root", "/tmp", *arguments],
        cwd=Path("/tmp"),
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), **(env or {})},
        check=False,
        capture_output=True,
        text=True,
    )


def _request(root: Path, path: str) -> dict[str, object]:
    return {
        "schema_version": "operational-retention-request-v1",
        "operation_id": "22222222-2222-2222-2222-222222222222",
        "root": str(root),
        "entries": [
            {
                "path": path,
                "kind": "directory",
                "retention_class": "restore_staging",
                "decision": "REMOTE_ONLY",
                "reason": "fixture has no consumer",
                "last_consumer": "fixture",
                "remote_evidence": ["rclone:ia_drive_crypt:fixture/control.json"],
                "dependencies": [],
            }
        ],
    }


def _fake_rclone(path: Path) -> Path:
    binary = path / "rclone"
    binary.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os, shutil, sys",
                "args = sys.argv[1:]",
                "if not args: sys.exit(2)",
                "if args[0] == 'version':",
                "    print('rclone v1.0-fixture')",
                "    sys.exit(0)",
                "if args[0] == 'lsd':",
                "    sys.exit(0)",
                "if args[0] == 'copyto':",
                "    source, destination = args[1], args[2]",
                "    root = os.environ['FAKE_DRIVE_ROOT']",
                "    prefix = 'ia_drive_crypt:'",
                "    if source.startswith(prefix):",
                "        relative = source[len(prefix):].lstrip('/')",
                "        shutil.copyfile(os.path.join(root, relative), destination)",
                "    else:",
                "        relative = destination[len(prefix):].lstrip('/')",
                "        target = os.path.join(root, relative)",
                "        os.makedirs(os.path.dirname(target), exist_ok=True)",
                "        shutil.copyfile(source, target)",
                "    sys.exit(0)",
                "sys.exit(2)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary


def test_cli_plan_publish_retire_and_status(tmp_path: Path) -> None:
    root = tmp_path / "authorized"
    stage = root / "ops" / "stage"
    stage.mkdir(parents=True)
    (stage / "payload.bin").write_bytes(b"retire-me" * 128)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(_request(root, "ops/stage")), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    planned = _run("plan", "--request", str(request), "--output", str(plan_path))
    assert planned.returncode == 0, planned.stderr
    summary = json.loads(planned.stdout)
    assert summary["plan_sha256"]
    assert json.loads(plan_path.read_text())["schema_version"] == "operational-retention-plan-v1"

    verified_path = tmp_path / "verified.json"
    verified = _run(
        "verify",
        "--plan",
        str(plan_path),
        "--plan-sha256",
        summary["plan_sha256"],
        "--output",
        str(verified_path),
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified_path.read_text())["verified"] is True

    drive = tmp_path / "drive"
    drive.mkdir()
    binary = _fake_rclone(tmp_path)
    remote_receipt = tmp_path / "remote.json"
    published = _run(
        "publish",
        "--plan",
        str(plan_path),
        "--plan-sha256",
        summary["plan_sha256"],
        "--rclone-path",
        str(binary),
        "--encrypted-remote",
        "ia_drive_crypt",
        "--folder-id",
        "fixture",
        "--remote-prefix",
        "v2-readiness-20261026",
        "--receipt",
        str(remote_receipt),
        env={"FAKE_DRIVE_ROOT": str(drive)},
    )
    assert published.returncode == 0, published.stderr
    remote = json.loads(remote_receipt.read_text())
    assert remote["readback_verified"] is True
    assert len(remote["artifacts"]) == 2
    for artifact in remote["artifacts"]:
        assert (drive / artifact["object_name"]).is_file()

    checkpoint = tmp_path / "retention.checkpoint.json"
    receipt = tmp_path / "retention.receipt.json"
    retired = _run(
        "retire",
        "--plan",
        str(plan_path),
        "--plan-sha256",
        summary["plan_sha256"],
        "--checkpoint",
        str(checkpoint),
        "--receipt",
        str(receipt),
    )
    assert retired.returncode == 0, retired.stderr
    assert not stage.exists()
    terminal = json.loads(receipt.read_text())
    assert terminal["retired_paths"] == ["ops/stage"]
    assert terminal["idempotent_replay"] is False

    status = _run("status", "--checkpoint", str(checkpoint), "--receipt", str(receipt))
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["state"] == "retired"

    replay = _run(
        "retire",
        "--plan",
        str(plan_path),
        "--plan-sha256",
        summary["plan_sha256"],
        "--checkpoint",
        str(checkpoint),
        "--receipt",
        str(receipt),
    )
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["idempotent_replay"] is True


def test_cli_rejects_unsafe_request_without_output(tmp_path: Path) -> None:
    root = tmp_path / "authorized"
    root.mkdir()
    request = tmp_path / "request.json"
    document = _request(root, "../escape")
    request.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "plan.json"
    result = _run("plan", "--request", str(request), "--output", str(output))
    assert result.returncode == 2
    assert not output.exists()
    assert "normalized and relative" in result.stderr


def test_cli_reports_budget_refusal_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "authorized"
    stage = root / "ops" / "stage"
    stage.mkdir(parents=True)
    (stage / "payload.bin").write_bytes(b"keep-me")
    request = tmp_path / "request.json"
    request.write_text(json.dumps(_request(root, "ops/stage")), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    planned = _run("plan", "--request", str(request), "--output", str(plan_path))
    assert planned.returncode == 0, planned.stderr
    summary = json.loads(planned.stdout)
    result = _run(
        "retire",
        "--plan",
        str(plan_path),
        "--plan-sha256",
        summary["plan_sha256"],
        "--checkpoint",
        str(tmp_path / "retention.checkpoint.json"),
        "--receipt",
        str(tmp_path / "retention.receipt.json"),
        "--reserve-bytes",
        str(10**18),
    )
    assert result.returncode == 3
    assert stage.is_dir()
