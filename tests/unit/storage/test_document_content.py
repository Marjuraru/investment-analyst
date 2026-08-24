from pathlib import Path

import pytest

from investment_analyst.storage.document_content import DocumentContentError, DocumentContentStore
from investment_analyst.storage.paths import StoragePaths


def test_content_store_round_trip_is_deduplicated_and_verified(tmp_path: Path) -> None:
    store = DocumentContentStore(StoragePaths.from_root(tmp_path))

    first = store.put(b"<html>exact</html>")
    second = store.put(b"<html>exact</html>")

    assert first.created is True
    assert second.created is False
    assert first.sha256 == second.sha256
    assert store.read(first.sha256) == b"<html>exact</html>"
    store.verify(first.sha256, size_bytes=first.size_bytes)


def test_content_store_rejects_missing_and_corrupt_blobs(tmp_path: Path) -> None:
    store = DocumentContentStore(StoragePaths.from_root(tmp_path))
    receipt = store.put(b"content")
    target = (
        tmp_path
        / "data/documents/sha256"
        / receipt.sha256[:2]
        / receipt.sha256[2:4]
        / receipt.sha256
    )
    target.write_bytes(b"tampered")

    with pytest.raises(DocumentContentError, match="checksum mismatch"):
        store.read(receipt.sha256)
    with pytest.raises(DocumentContentError, match="missing"):
        store.read("a" * 64)


def test_read_only_store_never_creates_directories(tmp_path: Path) -> None:
    store = DocumentContentStore(StoragePaths.from_root(tmp_path), read_only=True)

    with pytest.raises(DocumentContentError, match="read-only"):
        store.put(b"content")
    with pytest.raises(DocumentContentError, match="missing"):
        store.read("b" * 64)
    assert not (tmp_path / "data/documents").exists()


def test_store_rejects_symbolic_link_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    documents = tmp_path / "data/documents"
    documents.parent.mkdir()
    documents.symlink_to(target, target_is_directory=True)
    store = DocumentContentStore(StoragePaths.from_root(tmp_path))

    with pytest.raises(DocumentContentError, match="symbolic links"):
        store.put(b"content")
