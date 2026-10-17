"""Unit tests for the resumable raw v1 to v2 importer."""

import json
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
            assert summary.max_page_hydrated == 2
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


def test_verify_complete_is_read_only_and_detects_index_divergence(tmp_path: Path) -> None:
    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as writer:
        _seed(writer, 3)
    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            importer.run(page_limit=2)
            state_path = staging.destination / "raw-v2-import-state.json"
            state_before = state_path.read_bytes()

            verified = importer.verify_complete()

            assert verified.complete is True
            assert verified.imported_count == 3
            assert verified.max_page_hydrated == 2
            assert state_path.read_bytes() == state_before
            staging._connection.execute("DELETE FROM raw_v2_index")
            with pytest.raises(RawV2ImportError, match="prefix does not match|staged inventory"):
                importer.verify_complete()
            assert state_path.read_bytes() == state_before


def test_explicit_verification_does_not_trust_or_rewrite_a_completed_prefix(tmp_path: Path) -> None:
    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as writer:
        _seed(writer, 3)
    with (
        LocalStorage(source_paths, read_only=True) as source,
        _staging(tmp_path, "staging") as staging,
    ):
        importer = RawV2Importer(
            source,
            staging,
            source_workspace_id="source",
            source_fingerprint="B",
            historic_source_fingerprint="A",
        )
        importer.run(page_limit=2)
        importer.run(page_limit=2)
        paths = (
            staging.destination / "raw-v2-import-state.json",
            staging.destination / "raw-v2-import-verification-work.json",
        )
        before = {path: path.read_bytes() for path in paths}
        assert importer.verify_complete().max_page_hydrated == 2
        assert all(path.read_bytes() == payload for path, payload in before.items())
        staging._connection.execute("DELETE FROM raw_v2_index")
        with pytest.raises(RawV2ImportError, match="prefix does not match|staged inventory"):
            importer.verify_complete()
        assert all(path.read_bytes() == payload for path, payload in before.items())


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


def test_resume_emits_prefix_and_checkpoint_progress(tmp_path: Path) -> None:
    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as writer:
        _seed(writer, 3)
    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            events = []
            importer = RawV2Importer(
                source,
                staging,
                source_workspace_id="workspace-progress",
                source_fingerprint="fingerprint-progress",
                progress=events.append,
            )
            with pytest.raises(RawV2ImportError, match="interrupted"):
                importer.run(page_limit=2, fail_after_page=0)
            resumed = importer.run(page_limit=2)
    assert resumed.complete is True
    phases = [event.phase for event in events]
    assert "prefix_validation_page_verified" in phases
    assert "import_page_checkpointed" in phases
    assert "completion_validation_completed" in phases
    assert all(event.page_size <= 2 for event in events)


def test_completed_import_revalidates_prefix_without_second_deep_pass(tmp_path: Path) -> None:
    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as writer:
        _seed(writer, 3)
    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            events = []
            importer = RawV2Importer(
                source,
                staging,
                source_workspace_id="workspace-complete",
                source_fingerprint="fingerprint-complete",
                progress=events.append,
            )
            assert importer.run(page_limit=2).complete is True
            events.clear()

            rerun = importer.run(page_limit=2)

    assert rerun.complete is True
    phases = [event.phase for event in events]
    assert phases.count("prefix_validation_page_verified") == 2
    assert "completion_validation_page_verified" not in phases
    assert phases[-1] == "completion_validation_completed"


def test_snapshot_prefix_verification_resumes_from_a_durable_page_checkpoint(
    tmp_path: Path,
) -> None:
    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as writer:
        _seed(writer, 3)
    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            initial = RawV2Importer(
                source,
                staging,
                source_workspace_id="workspace-snapshot-prefix",
                source_fingerprint="fingerprint-snapshot-prefix",
            )
            assert initial.run(page_limit=2).complete is True

            def interrupt_after_first_verified_page(event) -> None:
                if event.phase == "prefix_validation_page_verified" and event.confirmed_count == 2:
                    raise RuntimeError("interrupt checkpointed verification")

            interrupted = RawV2Importer(
                source,
                staging,
                source_workspace_id="workspace-snapshot-prefix",
                source_fingerprint="fingerprint-snapshot-prefix",
                historic_source_fingerprint="historic-fingerprint",
                progress=interrupt_after_first_verified_page,
            )
            with pytest.raises(RawV2ImportError, match="progress observer failed"):
                interrupted.run(page_limit=2)

            checkpoint = json.loads(
                (staging.destination / "raw-v2-import-verification-work.json").read_text(
                    encoding="utf-8"
                )
            )
            assert checkpoint["confirmed_count"] == 2

            events = []
            resumed = RawV2Importer(
                source,
                staging,
                source_workspace_id="workspace-snapshot-prefix",
                source_fingerprint="fingerprint-snapshot-prefix",
                historic_source_fingerprint="historic-fingerprint",
                progress=events.append,
            ).run(page_limit=2)

    verified_counts = [
        event.confirmed_count
        for event in events
        if event.phase == "prefix_validation_page_verified"
    ]
    assert verified_counts == [3]
    assert resumed.counts_by_source == {"alpaca:bars": 3}
    assert sum(resumed.counts_by_schema.values()) == 3

    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            complete_resume = RawV2Importer(
                source,
                staging,
                source_workspace_id="workspace-snapshot-prefix",
                source_fingerprint="fingerprint-snapshot-prefix",
                historic_source_fingerprint="historic-fingerprint",
            ).run(page_limit=2)

    assert complete_resume.complete is True
    assert complete_resume.imported_count == 3


@pytest.mark.parametrize("corruption", [None, "blob", "metadata", "projection"])
def test_bounded_epoch_reuses_only_exact_bytes_and_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str | None
) -> None:
    from investment_analyst.storage.raw_v2 import RawV2StagingError

    source_paths = StoragePaths.from_root(tmp_path / "source")
    with LocalStorage(source_paths) as source:
        records = _seed(source, 3)
    destination = (tmp_path / "staging").absolute()
    destination.mkdir()
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    with (
        LocalStorage(source_paths, read_only=True) as source,
        RawV2Staging(destination, connection) as staging,
    ):
        baseline = RawV2Importer(
            source, staging, source_workspace_id="source", source_fingerprint="frozen"
        ).run(page_limit=2)
        connection.execute(
            "CREATE TABLE workspace_raw_json_projections_v2 "
            "(record_id VARCHAR,field_name VARCHAR,field_value VARCHAR,"
            "PRIMARY KEY(record_id,field_name))"
        )

        def repeated_work(*args, **kwargs):
            raise AssertionError("settled epoch page must not repeat writer hydration")

        monkeypatch.setattr(staging, "save_many", repeated_work)
        monkeypatch.setattr(staging, "get_many", repeated_work)
        if corruption == "blob":
            row = connection.execute(
                "SELECT relative_path FROM raw_v2_index WHERE record_id=?",
                [str(records[0].record_id)],
            ).fetchone()
            path = staging._resolve_blob(row[0])
            original = path.read_bytes()
            path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        elif corruption == "metadata":
            connection.execute(
                "UPDATE raw_v2_index SET source_id='foreign' WHERE record_id=?",
                [str(records[0].record_id)],
            )
        elif corruption == "projection":
            connection.execute(
                "INSERT INTO workspace_raw_json_projections_v2 VALUES (?,'unknown','value')",
                [str(records[0].record_id)],
            )
        importer = RawV2Importer(
            source,
            staging,
            source_workspace_id="source",
            source_fingerprint="frozen",
            epoch=uuid4(),
            destination_connection=connection,
        )
        if corruption is None:
            result = importer.run(page_limit=2)
            assert result.corpus_digest == baseline.corpus_digest
            assert result.reused_count == result.imported_count == 3
            assert importer.verify_complete().corpus_digest == result.corpus_digest
        else:
            with pytest.raises(
                (RawV2StagingError, RawV2ImportError), match="checksum|projections|canonical"
            ):
                importer.run(page_limit=2)
