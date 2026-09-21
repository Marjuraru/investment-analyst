"""Unit tests for bounded operational journal append-only contract, digests, and recovery."""

import hashlib
from pathlib import Path

import pytest

from investment_analyst.application.bounded_journal import (
    BoundedOperationalJournal,
    OperationalJournalCorruptionError,
    OperationalJournalDigestMismatchError,
    OperationalJournalError,
)


def test_segments_are_append_only_and_verified_by_digest(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    journal = BoundedOperationalJournal(journal_dir)

    for i in range(3):
        journal.append({"transition_id": f"t-{i}", "status": "completed"})

    journal.rotate(fold=False)

    entries = journal.read_entries()
    assert len(entries) == 3
    assert [e["transition_id"] for e in entries] == ["t-0", "t-1", "t-2"]

    manifest = journal._load_manifest()
    assert manifest is not None
    assert len(manifest.closed_segments) == 1
    segment_info = manifest.closed_segments[0]

    segment_file = journal_dir / segment_info.name
    real_digest = hashlib.sha256(segment_file.read_bytes()).hexdigest()
    assert segment_info.digest == real_digest

    # Tamper with the closed segment
    content = bytearray(segment_file.read_bytes())
    content[10] ^= 0xFF
    segment_file.write_bytes(bytes(content))

    with pytest.raises(OperationalJournalDigestMismatchError):
        journal.read_entries()


def test_one_transition_does_not_reserialize_the_previous_history(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    # Large segment size to avoid auto-rotation
    journal = BoundedOperationalJournal(journal_dir, max_segment_bytes=50_000_000)

    for i in range(200):
        journal.append({"attempt_id": f"att-{i}", "payload": "x" * 100})

    manifest = journal._load_manifest()
    assert manifest is not None
    open_segment = journal_dir / manifest.open_segment_name
    size_before = open_segment.stat().st_size

    new_transition = {"attempt_id": "att-new-1", "payload": "unique_extra_data"}
    journal.append(new_transition)

    size_after = open_segment.stat().st_size
    diff = size_after - size_before

    # The file should only have grown by approximately the size of the new record JSON + newline,
    # never by re-serializing the previous 200 items.
    assert 50 < diff < 200
    assert size_after > size_before


def test_partial_trailing_line_recovers_without_losing_confirmed_transitions(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    journal = BoundedOperationalJournal(journal_dir)

    for i in range(5):
        journal.append({"attempt_id": f"confirmed-{i}", "val": i})

    manifest = journal._load_manifest()
    assert manifest is not None
    open_segment = journal_dir / manifest.open_segment_name

    # Simulate sudden crash/power loss leaving an incomplete trailing line
    with open(open_segment, "ab") as f:
        f.write(b'{"attempt_id": "crashed-6", "incomplete": true')

    # Reading the journal must recover all 5 confirmed transitions without error
    entries = journal.read_entries()
    assert len(entries) == 5
    assert [e["attempt_id"] for e in entries] == [f"confirmed-{i}" for i in range(5)]

    # Subsequent append must succeed and be readable
    journal.append({"attempt_id": "recovered-7", "val": 7})
    entries_after = journal.read_entries()
    assert len(entries_after) == 6
    assert entries_after[-1]["attempt_id"] == "recovered-7"


def test_corrupt_snapshot_fails_closed(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    journal = BoundedOperationalJournal(journal_dir)

    journal.append({"attempt_id": "s1", "data": 10})
    journal.append({"attempt_id": "s2", "data": 20})
    journal.rotate(fold=True)

    snapshot_file = journal_dir / "snapshot.json"
    assert snapshot_file.exists()

    # Corrupt the snapshot file content
    snapshot_file.write_bytes(b'{"malformed_json: true')

    with pytest.raises(OperationalJournalCorruptionError):
        journal.read_entries()


def test_folding_and_rotation_never_drop_a_transition(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    # Small segment size to trigger frequent rotations
    journal = BoundedOperationalJournal(
        journal_dir,
        max_segment_bytes=150,
        max_segment_records=2,
    )

    total_transitions = 25
    for i in range(total_transitions):
        journal.append({"attempt_id": f"trans-{i:03d}", "num": i})

    # Read back all entries
    entries = journal.read_entries()
    assert len(entries) == total_transitions
    for i in range(total_transitions):
        assert any(e["attempt_id"] == f"trans-{i:03d}" for e in entries)


def test_state_root_boundary_enforcement(tmp_path: Path) -> None:
    root = tmp_path / "allowed_root"
    root.mkdir()
    outside = tmp_path / "outside"

    with pytest.raises(OperationalJournalError, match="declared state_root"):
        BoundedOperationalJournal(outside, state_root=root)
