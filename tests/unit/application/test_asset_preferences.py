"""Tests for durable watchlist preferences and scheduler reconciliation."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.application.asset_preferences import (
    AssetPreferenceEntry,
    AssetPreferencesConflictError,
    AssetPreferencesService,
    AssetPreferencesStateError,
    AssetPreferencesStore,
    AssetPreferencesUpdate,
    asset_preferences_fingerprint,
    cli_seed_asset_preferences,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetScheduleStateStore,
    RegisteredScheduledJob,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobInvocation,
)
from investment_analyst.application.runtime import ApplicationRuntime


def _universe():
    return InvestmentAnalystApplication(ApplicationRuntime.create_default()).list_market_assets()


def _entries(*asset_ids: str) -> tuple[AssetPreferenceEntry, ...]:
    return tuple(
        AssetPreferenceEntry(
            asset_id=asset_id,
            watchlist=True,
            favorite=index == 0,
            scheduled_refresh=True,
        )
        for index, asset_id in enumerate(sorted(asset_ids))
    )


def _update(
    entries: tuple[AssetPreferenceEntry, ...],
    *,
    revision_id: UUID | None,
    fingerprint: str,
) -> AssetPreferencesUpdate:
    return AssetPreferencesUpdate(
        expected_revision_id=revision_id,
        expected_fingerprint=fingerprint,
        entries=entries,
    )


def test_contracts_reject_extras_ambiguous_booleans_duplicates_and_order() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        AssetPreferenceEntry(
            asset_id="equity:us:aapl",
            watchlist=True,
            favorite=False,
            scheduled_refresh=True,
            copied_provider="alpaca",
        )
    with pytest.raises(ValidationError, match="must be a bool"):
        AssetPreferenceEntry(
            asset_id="equity:us:aapl",
            watchlist=1,
            favorite=False,
            scheduled_refresh=True,
        )
    with pytest.raises(ValidationError, match="belong to the watchlist"):
        AssetPreferenceEntry(
            asset_id="equity:us:aapl",
            watchlist=False,
            favorite=True,
            scheduled_refresh=False,
        )

    entries = _entries("equity:us:amd", "equity:us:aapl")
    with pytest.raises(ValidationError, match="sorted"):
        AssetPreferencesUpdate(
            expected_revision_id=None,
            expected_fingerprint="0" * 64,
            entries=tuple(reversed(entries)),
        )
    with pytest.raises(ValidationError, match="duplicates"):
        AssetPreferencesUpdate(
            expected_revision_id=None,
            expected_fingerprint="0" * 64,
            entries=(entries[0], entries[0]),
        )
    with pytest.raises(ValidationError, match="schema_version"):
        AssetPreferencesUpdate.model_validate(
            {
                "schema_version": "asset-preferences-update-v2",
                "expected_revision_id": None,
                "expected_fingerprint": "0" * 64,
                "entries": [],
            }
        )
    oversized = tuple(
        AssetPreferenceEntry(
            asset_id=f"equity:test:{index:03d}",
            watchlist=False,
            favorite=False,
            scheduled_refresh=False,
        )
        for index in range(101)
    )
    with pytest.raises(ValidationError, match="100"):
        AssetPreferencesUpdate(
            expected_revision_id=None,
            expected_fingerprint="0" * 64,
            entries=oversized,
        )


def test_cli_seed_is_effective_without_writing_and_persisted_state_wins(tmp_path: Path) -> None:
    universe = _universe()
    seed = cli_seed_asset_preferences(universe, ("equity:us:aapl",))
    store = AssetPreferencesStore(
        tmp_path / "state/preferences.json",
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
        revision_id_factory=lambda: UUID("00000000-0000-4000-8000-000000000101"),
    )
    service = AssetPreferencesService(
        store,
        universe,
        seed,
        scheduler=None,
        job_factory=None,
    )

    initial = service.view()
    assert initial.source == "cli_seed"
    assert initial.revision_id is None
    assert initial.scheduled_asset_count == 0
    initial_aapl = next(item for item in initial.assets if item.asset_id == "equity:us:aapl")
    assert initial_aapl.scheduled_refresh is True
    assert initial_aapl.effective_scheduled_refresh is False
    assert not store.path.exists()

    requested = _entries("crypto:btc-usd")
    first = service.update(
        _update(
            requested,
            revision_id=None,
            fingerprint=initial.fingerprint,
        )
    )
    restarted_with_conflicting_cli = AssetPreferencesService(
        store,
        universe,
        cli_seed_asset_preferences(universe, ("equity:us:amd",)),
        scheduler=None,
        job_factory=None,
    ).view()

    assert first.source == "persisted"
    assert first.revision_id == UUID("00000000-0000-4000-8000-000000000101")
    assert restarted_with_conflicting_cli.fingerprint == first.fingerprint
    assert restarted_with_conflicting_cli.scheduled_asset_count == 0
    assert restarted_with_conflicting_cli.scheduled_job_count == 0
    favorite = next(item for item in restarted_with_conflicting_cli.assets if item.favorite)
    assert favorite.asset_id == "crypto:btc-usd"
    assert favorite.scheduled_refresh is True
    assert favorite.effective_scheduled_refresh is False


def test_identical_update_is_idempotent_and_distinct_revisions_keep_history(
    tmp_path: Path,
) -> None:
    universe = _universe()
    seed = cli_seed_asset_preferences(universe, ())
    clocks = iter(
        (
            datetime(2026, 8, 2, hour=1, tzinfo=UTC),
            datetime(2026, 8, 2, hour=2, tzinfo=UTC),
        )
    )
    ids = iter(
        (
            UUID("00000000-0000-4000-8000-000000000201"),
            UUID("00000000-0000-4000-8000-000000000202"),
        )
    )
    store = AssetPreferencesStore(
        tmp_path / "preferences.json",
        clock=clocks.__next__,
        revision_id_factory=ids.__next__,
    )
    first_entries = _entries("equity:us:aapl")
    first, changed = store.apply(
        _update(first_entries, revision_id=None, fingerprint=seed.fingerprint),
        seed,
    )
    repeated, repeated_changed = store.apply(
        _update(
            first_entries,
            revision_id=first.revision_id,
            fingerprint=first.fingerprint,
        ),
        seed,
    )
    second_entries = _entries("crypto:btc-usd", "equity:us:aapl")
    second, second_changed = store.apply(
        _update(
            second_entries,
            revision_id=first.revision_id,
            fingerprint=first.fingerprint,
        ),
        seed,
    )

    history = store.load()
    assert changed is True and repeated_changed is False and second_changed is True
    assert repeated == first
    assert history is not None
    assert tuple(item.revision_id for item in history.revisions) == (
        first.revision_id,
        second.revision_id,
    )
    assert history.current.parent_revision_id == first.revision_id
    assert history.revisions[0].created_at != history.revisions[1].created_at
    assert second.fingerprint == asset_preferences_fingerprint(second_entries)


def test_conflict_limit_corruption_and_atomic_failure_are_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = _universe()
    seed = cli_seed_asset_preferences(universe, ())
    store = AssetPreferencesStore(
        tmp_path / "limited.json",
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
        max_revisions=1,
    )
    first, _ = store.apply(
        _update(_entries("equity:us:aapl"), revision_id=None, fingerprint=seed.fingerprint),
        seed,
    )
    with pytest.raises(AssetPreferencesConflictError, match="reload"):
        store.apply(
            _update(
                _entries("equity:us:amd"),
                revision_id=None,
                fingerprint=seed.fingerprint,
            ),
            seed,
        )
    with pytest.raises(AssetPreferencesStateError, match="history limit"):
        store.apply(
            _update(
                _entries("equity:us:amd"),
                revision_id=first.revision_id,
                fingerprint=first.fingerprint,
            ),
            seed,
        )

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text('{"secret":"simulated-secret"}', encoding="utf-8")
    corrupt_store = AssetPreferencesStore(corrupt)
    before = corrupt.read_bytes()
    with pytest.raises(AssetPreferencesStateError) as captured:
        corrupt_store.load()
    assert "simulated-secret" not in str(captured.value)
    assert corrupt.read_bytes() == before

    atomic = AssetPreferencesStore(tmp_path / "atomic.json")

    def fail_replace(*args: object) -> None:
        del args
        raise OSError("simulated-secret")

    monkeypatch.setattr(
        "investment_analyst.application.asset_preferences.os.replace",
        fail_replace,
    )
    with pytest.raises(AssetPreferencesStateError, match="could not be written") as failure:
        atomic.apply(
            _update(_entries("equity:us:aapl"), revision_id=None, fingerprint=seed.fingerprint),
            seed,
        )
    assert "simulated-secret" not in str(failure.value)
    assert not atomic.path.exists()
    assert list(tmp_path.glob(".atomic.json.*.tmp")) == []


def test_history_is_archived_before_hard_limits_and_read_limits_are_enforced(
    tmp_path: Path,
) -> None:
    universe = _universe()
    seed = cli_seed_asset_preferences(universe, ())
    clocks = iter(datetime(2026, 8, 2, hour=hour, tzinfo=UTC) for hour in range(1, 7))
    ids = iter(UUID(int=value) for value in range(1, 7))
    store = AssetPreferencesStore(
        tmp_path / "state/asset_preferences_state_v1.json",
        clock=clocks.__next__,
        revision_id_factory=ids.__next__,
        max_revisions=6,
        archive_at_revisions=4,
        retained_revisions=2,
    )
    current = seed
    expected_ids: list[UUID] = []
    for index in range(6):
        entries = _entries("equity:us:aapl" if index % 2 == 0 else "equity:us:amd")
        current, changed = store.apply(
            _update(
                entries,
                revision_id=current.revision_id,
                fingerprint=current.fingerprint,
            ),
            seed,
        )
        assert changed is True
        assert current.revision_id is not None
        expected_ids.append(current.revision_id)

    document = store.load()
    history = store.load_history()
    assert document is not None
    assert len(document.revisions) == 2
    assert len(document.archives) == 2
    assert sum(item.revision_count for item in document.archives) == 4
    assert tuple(item.revision_id for item in history) == tuple(expected_ids)
    assert all(
        item.parent_revision_id == (history[index - 1].revision_id if index else None)
        for index, item in enumerate(history)
    )
    assert all(
        (store.path.parent / f"{store.path.stem}_archives" / reference.file_name).stat().st_size
        == reference.size_bytes
        for reference in document.archives
    )
    first_archive = (
        store.path.parent / f"{store.path.stem}_archives" / document.archives[0].file_name
    )
    original_archive = first_archive.read_bytes()
    tampered_archive = bytearray(original_archive)
    tampered_archive[1] = ord("x")
    first_archive.write_bytes(tampered_archive)
    with pytest.raises(AssetPreferencesStateError) as hash_failure:
        store.load()
    assert "hash does not match" in str(hash_failure.value.__cause__)

    first_archive.write_bytes(b"x" * ((4 * 1024 * 1024) + 1))
    with pytest.raises(AssetPreferencesStateError, match="malformed or unreadable") as size_failure:
        store.load()
    assert "size limit" in str(size_failure.value.__cause__)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + (b" " * 256))
    with pytest.raises(AssetPreferencesStateError, match="malformed or unreadable") as read_failure:
        AssetPreferencesStore(
            oversized,
            max_document_bytes=128,
            archive_at_document_bytes=64,
        ).load()
    assert "size limit" in str(read_failure.value.__cause__)


def test_failed_active_state_publish_keeps_old_chain_and_reuses_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = _universe()
    seed = cli_seed_asset_preferences(universe, ())
    ids = iter(UUID(int=value) for value in range(101, 106))
    store = AssetPreferencesStore(
        tmp_path / "state/preferences.json",
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
        revision_id_factory=ids.__next__,
        max_revisions=6,
        archive_at_revisions=4,
        retained_revisions=2,
    )
    current = seed
    for asset_id in ("equity:us:aapl", "equity:us:amd", "crypto:btc-usd"):
        current, _ = store.apply(
            _update(
                _entries(asset_id),
                revision_id=current.revision_id,
                fingerprint=current.fingerprint,
            ),
            seed,
        )
    original_replace = __import__("os").replace

    def fail_state_only(source: Path, destination: Path) -> None:
        if destination == store.path:
            raise OSError("simulated-secret")
        original_replace(source, destination)

    monkeypatch.setattr(
        "investment_analyst.application.asset_preferences.os.replace",
        fail_state_only,
    )
    update = _update(
        _entries("equity:us:aapl"),
        revision_id=current.revision_id,
        fingerprint=current.fingerprint,
    )
    with pytest.raises(AssetPreferencesStateError, match="could not be written") as failure:
        store.apply(update, seed)
    assert "simulated-secret" not in str(failure.value)
    assert len(store.load_history()) == 3

    monkeypatch.setattr(
        "investment_analyst.application.asset_preferences.os.replace",
        original_replace,
    )
    recovered, changed = store.apply(update, seed)
    assert changed is True
    assert recovered.revision_id is not None
    assert len(store.load_history()) == 4
    assert len(store.load().archives) == 1


def test_service_reconciles_registry_without_running_providers_and_allows_empty_when_disabled(
    tmp_path: Path,
) -> None:
    universe = _universe()
    seed = cli_seed_asset_preferences(universe, ("equity:us:aapl",))
    provider_calls = 0
    factory_calls: list[tuple[str, ...]] = []

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal provider_calls
        provider_calls += 1
        return ScheduledJobExecution(
            job_id=invocation.definition.job_id,
            effective_known_at=invocation.started_at,
            evidence_changed=False,
            source_ids=("test-source",),
            created_count=0,
            reused_count=1,
        )

    def jobs(asset_ids: tuple[str, ...]) -> tuple[RegisteredScheduledJob, ...]:
        factory_calls.append(asset_ids)
        return tuple(
            RegisteredScheduledJob(
                ScheduledJobDefinition(
                    job_id=f"test:{asset_id}:market-daily",
                    asset_id=asset_id,
                    provider="test",
                    domain=ScheduledJobDomain.MARKET_DAILY,
                    data_frequency="day_1",
                ),
                run,
            )
            for asset_id in asset_ids
        )

    scheduler = MultiAssetScheduler(
        jobs(("equity:us:aapl",)),
        MultiAssetScheduleStateStore(tmp_path / "schedule.json"),
        clock=lambda: datetime(2026, 8, 2, hour=6, tzinfo=UTC),
    )
    store = AssetPreferencesStore(
        tmp_path / "preferences.json",
        clock=lambda: datetime(2026, 8, 2, hour=1, tzinfo=UTC),
    )
    service = AssetPreferencesService(
        store,
        universe,
        seed,
        scheduler=scheduler,
        job_factory=jobs,
    )
    initial = service.view()
    assert all(item.schema_version == "asset-preference-projection-v1" for item in initial.assets)
    updated = service.update(
        _update(
            _entries("crypto:btc-usd"),
            revision_id=None,
            fingerprint=initial.fingerprint,
        )
    )

    assert provider_calls == 0
    assert factory_calls[-1] == ("crypto:btc-usd",)
    assert tuple(item.asset_id for item in scheduler.registered_job_definitions()) == (
        "crypto:btc-usd",
    )
    assert updated.scheduled_job_count == 1
    with pytest.raises(ValueError, match="at least one scheduled asset"):
        service.update(
            _update(
                (),
                revision_id=updated.revision_id,
                fingerprint=updated.fingerprint,
            )
        )
    assert service.view().revision_id == updated.revision_id

    disabled_store = AssetPreferencesStore(tmp_path / "disabled.json")
    disabled = AssetPreferencesService(
        disabled_store,
        universe,
        seed,
        scheduler=None,
        job_factory=None,
    )
    disabled_initial = disabled.view()
    disabled_aapl = next(
        item for item in disabled_initial.assets if item.asset_id == "equity:us:aapl"
    )
    assert disabled_aapl.scheduled_refresh is True
    assert disabled_aapl.effective_scheduled_refresh is False
    assert disabled_initial.scheduled_asset_count == 0
    assert disabled_initial.scheduled_job_count == 0
    empty = disabled.update(
        _update(
            (),
            revision_id=None,
            fingerprint=disabled_initial.fingerprint,
        )
    )
    assert empty.watchlist_count == 0
    assert empty.scheduled_asset_count == 0
    assert empty.scheduler_enabled is False


def test_unavailable_saved_asset_remains_visible_and_new_catalog_assets_are_not_selected(
    tmp_path: Path,
) -> None:
    universe = _universe()
    seed = cli_seed_asset_preferences(universe, ("equity:us:aapl",))
    store = AssetPreferencesStore(
        tmp_path / "preferences.json",
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
    )
    unavailable = AssetPreferenceEntry(
        asset_id="equity:removed:legacy",
        watchlist=True,
        favorite=True,
        scheduled_refresh=True,
    )
    initial_entries = tuple(
        sorted(
            (*_entries("equity:us:aapl"), unavailable),
            key=lambda entry: entry.asset_id,
        )
    )
    persisted, _ = store.apply(
        _update(initial_entries, revision_id=None, fingerprint=seed.fingerprint),
        seed,
    )
    service = AssetPreferencesService(
        store,
        universe,
        seed,
        scheduler=None,
        job_factory=None,
    )

    view = service.view()
    removed = next(item for item in view.assets if item.asset_id == unavailable.asset_id)
    assert removed.available is False
    assert removed.scheduled_refresh is True
    assert removed.effective_scheduled_refresh is False
    assert removed.job_ids == ()
    persisted_ids = {entry.asset_id for entry in initial_entries}
    new_catalog_asset = next(item for item in view.assets if item.asset_id not in persisted_ids)
    assert new_catalog_asset.watchlist is False
    assert new_catalog_asset.scheduled_refresh is False

    available_only = tuple(
        AssetPreferenceEntry(
            asset_id=item.asset_id,
            watchlist=item.watchlist,
            favorite=item.favorite,
            scheduled_refresh=item.scheduled_refresh,
        )
        for item in sorted(
            (item for item in view.assets if item.available),
            key=lambda item: item.asset_id,
        )
    )
    updated = service.update(
        _update(
            available_only,
            revision_id=persisted.revision_id,
            fingerprint=persisted.fingerprint,
        )
    )
    assert any(item.asset_id == unavailable.asset_id for item in updated.assets)


def test_clock_rollback_is_clamped_without_losing_revision_history(tmp_path: Path) -> None:
    universe = _universe()
    seed = cli_seed_asset_preferences(universe, ())
    clocks = iter(
        (
            datetime(2026, 8, 2, hour=2, tzinfo=UTC),
            datetime(2026, 8, 2, hour=1, tzinfo=UTC),
        )
    )
    store = AssetPreferencesStore(tmp_path / "clock.json", clock=clocks.__next__)
    first, _ = store.apply(
        _update(_entries("equity:us:aapl"), revision_id=None, fingerprint=seed.fingerprint),
        seed,
    )
    second, _ = store.apply(
        _update(
            _entries("equity:us:amd"),
            revision_id=first.revision_id,
            fingerprint=first.fingerprint,
        ),
        seed,
    )

    assert first.created_at == second.created_at
    assert store.load() is not None
    assert len(store.load().revisions) == 2
