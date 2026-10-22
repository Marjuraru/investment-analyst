"""Unit tests for the isolated continuity archive format."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from investment_analyst.workspace.continuity_archive import (
    CONTINUITY_ARCHIVE_MANIFEST_NAME,
    ContinuityArchiveError,
    ContinuityArchiveWriter,
    load_continuity_archive_manifest,
    restore_continuity_archive,
    verify_continuity_archive,
)

_WORKSPACE_ID = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")
_CREATED_AT = datetime(2026, 10, 22, 12, tzinfo=UTC)
_FINGERPRINT = "a" * 64


def _writer(root: Path, *, chunk_bytes: int = 4) -> ContinuityArchiveWriter:
    return ContinuityArchiveWriter(
        root,
        source_workspace_id=_WORKSPACE_ID,
        source_fingerprint=_FINGERPRINT,
        code_sha="0" * 40,
        created_at=_CREATED_AT,
        chunk_bytes=chunk_bytes,
        buffer_bytes=1024,
    )


def test_archive_streams_chunks_and_verifies_every_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    first = source / "index.duckdb"
    first.write_bytes(b"abcdefghij")
    second = source / "documents" / "blob.htm"
    second.parent.mkdir()
    second.write_bytes(b"hello-world")
    writer = _writer(tmp_path / "archive")
    writer.add_file(first, "storage/index.duckdb")
    writer.add_file(second, "documents/blob.htm")
    manifest = writer.finalize()

    assert manifest.complete is True
    assert manifest.total_bytes == 21
    assert len(manifest.files) == 2
    verify_continuity_archive(tmp_path / "archive", manifest)
    reloaded = load_continuity_archive_manifest(tmp_path / "archive")
    assert reloaded.digest == manifest.digest
    assert reloaded.archive_id == manifest.archive_id


def test_archive_rejects_path_traversal_and_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    writer = _writer(tmp_path / "archive")
    with pytest.raises(ContinuityArchiveError):
        writer.add_file(source, "../escape.bin")
    link = tmp_path / "link.bin"
    link.symlink_to(source)
    with pytest.raises(ContinuityArchiveError):
        writer.add_file(link, "link.bin")


def test_archive_verification_rejects_corrupt_or_truncated_chunks(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefghij")
    writer = _writer(tmp_path / "archive")
    writer.add_file(source, "source.bin")
    manifest = writer.finalize()
    chunk = manifest.files[0].chunks[0]
    chunk_path = tmp_path / "archive" / "chunks" / chunk.sha256
    chunk_path.write_bytes(b"XXXX")
    with pytest.raises(ContinuityArchiveError):
        verify_continuity_archive(tmp_path / "archive", manifest)


def test_archive_manifest_is_published_only_after_verification(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefghij")
    writer = _writer(tmp_path / "archive")
    writer.add_file(source, "source.bin")
    assert not (tmp_path / "archive" / CONTINUITY_ARCHIVE_MANIFEST_NAME).exists()
    writer.finalize()
    assert (tmp_path / "archive" / CONTINUITY_ARCHIVE_MANIFEST_NAME).exists()


def test_restore_streams_only_selected_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"abcdefghij")
    (source / "b.bin").write_bytes(b"0123456789")
    writer = _writer(tmp_path / "archive")
    writer.add_file(source / "a.bin", "nested/a.bin")
    writer.add_file(source / "b.bin", "b.bin")
    manifest = writer.finalize()
    destination = tmp_path / "restore"
    restored = restore_continuity_archive(
        tmp_path / "archive",
        manifest,
        destination,
        paths=("nested/a.bin",),
    )
    assert restored == ("nested/a.bin",)
    assert (destination / "nested" / "a.bin").read_bytes() == b"abcdefghij"
    assert not (destination / "b.bin").exists()


def test_manifest_digest_binding_rejects_edited_content(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefghij")
    writer = _writer(tmp_path / "archive")
    writer.add_file(source, "source.bin")
    writer.finalize()
    payload = json.loads(
        (tmp_path / "archive" / CONTINUITY_ARCHIVE_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    payload["total_bytes"] += 1
    (tmp_path / "archive" / CONTINUITY_ARCHIVE_MANIFEST_NAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )
    with pytest.raises(ContinuityArchiveError):
        load_continuity_archive_manifest(tmp_path / "archive")


def test_archive_does_not_overwrite_a_previous_manifest(tmp_path: Path) -> None:
    first_source = tmp_path / "first.bin"
    first_source.write_bytes(b"first")
    first = _writer(tmp_path / "archive")
    first.add_file(first_source, "first.bin")
    first.finalize()
    expected = (tmp_path / "archive" / CONTINUITY_ARCHIVE_MANIFEST_NAME).read_bytes()
    with pytest.raises(ContinuityArchiveError):
        _writer(tmp_path / "archive")
    assert (tmp_path / "archive" / CONTINUITY_ARCHIVE_MANIFEST_NAME).read_bytes() == expected
