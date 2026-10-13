"""Streaming inventory tests for workspace-v2 backup files."""

import json
from pathlib import Path

import pytest

from investment_analyst.workspace.workspace_v2_backup_inventory import (
    WorkspaceV2BackupInventoryError,
    WorkspaceV2BackupInventoryTrailer,
    load_inventory,
    write_inventory,
)


def test_inventory_writes_hash_bound_jsonl(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "record.bin").write_bytes(b"verified")

    summary = write_inventory(root, tmp_path / "inventory.jsonl")

    entries = [
        json.loads(line)
        for line in (tmp_path / "inventory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    trailer = WorkspaceV2BackupInventoryTrailer.model_validate(entries.pop())
    assert summary.file_count == len(entries) == 1
    assert entries[0]["path"] == "nested/record.bin"
    assert entries[0]["size_bytes"] == len(b"verified")
    assert trailer.digest_sha256 == summary.digest_sha256


def test_inventory_rejects_symbolic_links(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    (root / "link").symlink_to(tmp_path / "outside")

    with pytest.raises(WorkspaceV2BackupInventoryError, match="symbolic"):
        write_inventory(root, tmp_path / "inventory.jsonl")


def test_inventory_resumes_owned_partial_progress(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    (root / "a.bin").write_bytes(b"a")
    (root / "b.bin").write_bytes(b"b")
    output = tmp_path / "inventory.jsonl"
    partial = tmp_path / ".inventory.jsonl.fixture.tmp"
    partial.write_text(
        json.dumps(
            {
                "path": "a.bin",
                "sha256": "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb",
                "size_bytes": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    summary = write_inventory(root, output)

    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [line["path"] for line in lines[:-1]] == ["a.bin", "b.bin"]
    assert lines[-1]["file_count"] == summary.file_count == 2
    assert not partial.exists()


def test_load_inventory_rejects_a_trailer_that_does_not_bind_entries(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    (root / "record.bin").write_bytes(b"verified")
    inventory = tmp_path / "inventory.jsonl"
    write_inventory(root, inventory)
    lines = inventory.read_text(encoding="utf-8").splitlines()
    trailer = json.loads(lines[-1])
    trailer["file_count"] = 2
    lines[-1] = json.dumps(trailer, sort_keys=True, separators=(",", ":"))
    inventory.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(WorkspaceV2BackupInventoryError, match="trailer"):
        load_inventory(inventory)
