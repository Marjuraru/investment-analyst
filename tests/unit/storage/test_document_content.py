import hashlib
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from investment_analyst.storage.document_content import DocumentContentError, DocumentContentStore
from investment_analyst.storage.paths import StoragePaths


def _paths(tmp_path: Path, *, format_version: int) -> StoragePaths:
    workspace_root = tmp_path / "workspace"
    if format_version == 1:
        return StoragePaths.from_root(workspace_root)
    return StoragePaths.from_workspace_root(
        workspace_root,
        format_version=2,
        workspace_id=UUID("d5ce313d-2d6f-4e34-8ad7-161455ae0a79"),
    )


def _filesystem_snapshot(root: Path) -> tuple[tuple[str, str, bytes | str], ...] | None:
    if root.is_symlink():
        return (("symlink", ".", os.readlink(root)),)
    if root.is_file():
        return (("file", ".", root.read_bytes()),)
    if not root.is_dir():
        return None

    entries: list[tuple[str, str, bytes | str]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append(("symlink", relative, os.readlink(path)))
            else:
                entries.append(("directory", relative, ""))
        for name in files:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append(("symlink", relative, os.readlink(path)))
            else:
                entries.append(("file", relative, path.read_bytes()))
    return tuple(sorted(entries))


@pytest.mark.parametrize("format_version", [1, 2])
def test_content_store_round_trip_is_deduplicated_and_verified(
    tmp_path: Path, format_version: int
) -> None:
    paths = _paths(tmp_path, format_version=format_version)
    store = DocumentContentStore(paths)
    assert paths.documents_boundary_root == (
        paths.root if format_version == 1 else paths.root.parent.parent
    )

    first = store.put(b"<html>exact</html>")
    second = store.put(b"<html>exact</html>")

    assert first.created is True
    assert second.created is False
    assert first.sha256 == second.sha256
    assert store.read(first.sha256) == b"<html>exact</html>"
    store.verify(first.sha256, size_bytes=first.size_bytes)


@pytest.mark.parametrize("format_version", [1, 2])
def test_content_store_rejects_missing_corrupt_and_wrong_size_blobs(
    tmp_path: Path, format_version: int
) -> None:
    paths = _paths(tmp_path, format_version=format_version)
    store = DocumentContentStore(paths)
    receipt = store.put(b"content")
    target = (
        paths.documents_dir / "sha256" / receipt.sha256[:2] / receipt.sha256[2:4] / receipt.sha256
    )

    with pytest.raises(DocumentContentError, match="size does not match"):
        store.verify(receipt.sha256, size_bytes=receipt.size_bytes + 1)

    target.write_bytes(b"tampered")

    with pytest.raises(DocumentContentError, match="checksum mismatch"):
        store.read(receipt.sha256)
    with pytest.raises(DocumentContentError, match="missing"):
        store.read("a" * 64)
    with pytest.raises(DocumentContentError, match="missing"):
        store.verify("b" * 64)


@pytest.mark.parametrize("format_version", [1, 2])
def test_read_only_store_never_creates_directories(tmp_path: Path, format_version: int) -> None:
    paths = _paths(tmp_path, format_version=format_version)
    store = DocumentContentStore(paths, read_only=True)

    with pytest.raises(DocumentContentError, match="read-only"):
        store.put(b"content")
    with pytest.raises(DocumentContentError, match="missing"):
        store.read("b" * 64)
    assert not paths.documents_dir.exists()


@pytest.mark.parametrize("format_version", [1, 2])
@pytest.mark.parametrize("checksum", ["A" * 64, "a" * 63])
@pytest.mark.parametrize("operation", ["read", "verify"])
def test_store_rejects_malformed_checksums_before_access(
    tmp_path: Path, format_version: int, checksum: str, operation: str
) -> None:
    store = DocumentContentStore(_paths(tmp_path, format_version=format_version))

    with pytest.raises(DocumentContentError, match="checksum is invalid"):
        if operation == "read":
            store.read(checksum)
        else:
            store.verify(checksum)


@pytest.mark.parametrize("format_version", [1, 2])
def test_store_rejects_external_document_directory_without_creating_it(
    tmp_path: Path, format_version: int
) -> None:
    paths = _paths(tmp_path, format_version=format_version)
    external_documents = tmp_path / "outside" / "documents"

    with pytest.raises(DocumentContentError, match="layout is invalid|escapes"):
        DocumentContentStore(replace(paths, documents_dir=external_documents)).put(b"content")
    assert not external_documents.exists()


def test_store_rejects_inconsistent_v2_root_layout_before_writes(tmp_path: Path) -> None:
    paths = _paths(tmp_path, format_version=2)
    inconsistent = replace(paths, root=paths.root.parent / "v3")

    with pytest.raises(DocumentContentError, match="layout is invalid"):
        DocumentContentStore(inconsistent)
    assert not paths.documents_dir.exists()


@pytest.mark.parametrize("format_version", [1, 2])
@pytest.mark.parametrize("component", ["data", "documents", "sha256", "shard1", "shard2", "leaf"])
@pytest.mark.parametrize("target_kind", ["external", "internal", "dangling"])
@pytest.mark.parametrize("operation", ["put", "read", "verify"])
def test_store_rejects_symlinks_at_every_path_component_without_io(
    tmp_path: Path,
    format_version: int,
    component: str,
    target_kind: str,
    operation: str,
) -> None:
    paths = _paths(tmp_path, format_version=format_version)
    boundary = paths.documents_boundary_root
    checksum = hashlib.sha256(b"content").hexdigest()
    documents = paths.documents_dir
    sha_root = documents / "sha256"
    first_shard = sha_root / checksum[:2]
    second_shard = first_shard / checksum[2:4]
    target_blob = second_shard / checksum
    link_path = {
        "data": boundary / "data",
        "documents": documents,
        "sha256": sha_root,
        "shard1": first_shard,
        "shard2": second_shard,
        "leaf": target_blob,
    }[component]
    link_path.parent.mkdir(parents=True, exist_ok=True)

    link_target = (
        boundary / ".redirect-target" if target_kind == "internal" else tmp_path / "outside-target"
    )
    if component == "leaf":
        if target_kind != "dangling":
            link_target.write_bytes(b"protected target bytes")
        symlink_target = link_target
    else:
        if target_kind != "dangling":
            link_target.mkdir(parents=True)
            (link_target / "sentinel.txt").write_text("protected", encoding="utf-8")
        symlink_target = link_target
    before = _filesystem_snapshot(link_target)
    link_path.symlink_to(symlink_target, target_is_directory=component != "leaf")

    store = DocumentContentStore(paths)
    with pytest.raises(DocumentContentError, match="symbolic links"):
        if operation == "put":
            store.put(b"content")
        elif operation == "read":
            store.read(checksum)
        else:
            store.verify(checksum)

    assert _filesystem_snapshot(link_target) == before
