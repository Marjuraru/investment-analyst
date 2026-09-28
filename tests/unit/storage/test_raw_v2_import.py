"""Unit tests for the resumable raw v1 to v2 importer."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.raw_v2_import import (
    RawV2Importer,
    RawV2ImportError,
)

from .conftest import make_raw_record

_RECEIVED = datetime(2026, 7, 10, 16, 3, tzinfo=UTC)


def _seed(storage: LocalStorage, count: int):
    records = [
        make_raw_record(record_id=uuid4(), received_at=_RECEIVED).model_copy(
            update={"payload": {"close": "210.50", "sequence": index}}
        )
        for index in range(count)
    ]
    for record in records:
        storage.raw_records.save(record)
    return records


def _staging(tmp_path: Path, name: str) -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _importer(source: LocalStorage, staging: RawV2Staging) -> RawV2Importer:
    inspection = source.store.connection.execute("SELECT count(*) FROM raw_record_index").fetchone()
    assert inspection is not None
    workspace_id = f"workspace-{inspection[0]}"
    return RawV2Importer(
        source,
        staging,
        source_workspace_id=workspace_id,
        source_fingerprint=f"fingerprint-{inspection[0]}",
    )


def test_import_resume_replays_unconfirmed_batch_idempotently(tmp_path: Path) -> None:
    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as writer:
        _seed(writer, 3)
    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            with pytest.raises(RawV2ImportError, match="interrupted"):
                importer.run(page_limit=2, fail_after_page=0)
            confirmed_before = staging.list_record_ids()
            assert len(confirmed_before) == 2
            summary = importer.run(page_limit=2)
            assert summary.complete is True
            assert summary.imported_count == 3
            assert summary.max_page_requested == 2
            assert summary.max_page_hydrated == 1
            assert sorted(staging.list_record_ids()) == sorted(source.raw_records.list_record_ids())
            rerun = importer.run(page_limit=2)
            assert rerun.complete is True
            assert rerun.imported_count == 3


def test_import_rejects_unbounded_pages_and_foreign_state(tmp_path: Path) -> None:
    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as writer:
        _seed(writer, 1)
    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            with pytest.raises(RawV2ImportError, match="between 1 and 256"):
                importer.run(page_limit=257)
            summary = importer.run(page_limit=1)
            assert summary.complete is True
            state_path = staging.destination / "raw-v2-import-state.json"
            state_path.write_text('{"truncated":true}', encoding="utf-8")
            with pytest.raises(RawV2ImportError, match="truncated"):
                importer.run(page_limit=1)


def test_import_requires_read_only_source(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path / "source")) as writer:
        _seed(writer, 1)
        staging = _staging(tmp_path, "staging")
        with staging, pytest.raises(RawV2ImportError, match="read-only"):
            RawV2Importer(
                writer,
                staging,
                source_workspace_id="workspace-1",
                source_fingerprint="fingerprint-1",
            )


def test_legacy_v1_checkpoint_keeps_same_path_semantics(tmp_path) -> None:
    import json

    from investment_analyst.storage.raw_v2_import import (
        RAW_V2_IMPORT_STATE_FORMAT,
        RawV2ImportState,
    )

    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as writer:
        _seed(writer, 2)
    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            summary = importer.run(page_limit=2)
            assert summary.complete is True
            state_path = staging.destination / "raw-v2-import-state.json"
            document = json.loads(state_path.read_text(encoding="utf-8"))
            assert document["format"] == "raw-v2-import-state-v2"
            legacy = dict(document)
            legacy["format"] = RAW_V2_IMPORT_STATE_FORMAT
            legacy["staging_id"] = None
            RawV2ImportState.model_validate(legacy)
            state_path.write_text(
                json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            rerun = importer.run(page_limit=2)
            assert rerun.complete is True
            other = _staging(tmp_path, "other")
            with other:
                state_document = json.loads(state_path.read_text(encoding="utf-8"))
                (other.destination / "raw-v2-import-state.json").write_text(
                    json.dumps(state_document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                foreign = RawV2Importer(
                    source,
                    other,
                    source_workspace_id=importer._workspace_id,
                    source_fingerprint=importer._fingerprint,
                )
                import pytest as _pytest

                with _pytest.raises(RawV2ImportError, match="another source or destination"):
                    foreign.run(page_limit=2)


def test_resume_and_completion_never_accumulate_full_inventory(tmp_path) -> None:
    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as writer:
        _seed(writer, 5)
    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            calls: list = []
            original_source = source.raw_records.list_import_page
            original_staged = staging.list_inventory_page

            def _counting_source(**kwargs):
                page = original_source(**kwargs)
                calls.append(("source", kwargs.get("limit")))
                return page

            def _counting_staged(**kwargs):
                page = original_staged(**kwargs)
                calls.append(("staged", kwargs.get("limit")))
                return page

            source.raw_records.list_import_page = _counting_source  # type: ignore[method-assign]
            staging.list_inventory_page = _counting_staged  # type: ignore[method-assign]
            summary = importer.run(page_limit=2)
            assert summary.complete is True
            assert calls
            assert all(limit is not None and limit <= 2 for _, limit in calls)
            assert all(limit is not None and limit <= 256 for _, limit in calls)
