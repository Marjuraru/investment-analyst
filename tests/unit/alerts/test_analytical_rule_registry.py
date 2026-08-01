"""Tests for versioned local analytical rule configuration."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_analyst.alerts.analytical_models import AnalyticalRuleState
from investment_analyst.alerts.analytical_rule_catalog import (
    INITIAL_ANALYTICAL_RULES,
    INITIAL_MARKET_ACTIVITY_RULE,
)
from investment_analyst.alerts.analytical_rule_registry import (
    AnalyticalConditionConfigurationUpdate,
    AnalyticalRuleConfigurationUpdate,
    AnalyticalRuleRegistryConflictError,
    AnalyticalRuleRegistryStore,
)
from investment_analyst.application.operational_state import AaplOperationalStateError

_RECORDED_AT = datetime(2026, 7, 29, 18, tzinfo=UTC)


def _request(
    store: AnalyticalRuleRegistryStore,
    *,
    threshold: str = "1.5",
    exit_threshold: str | None = "1.2",
    state: AnalyticalRuleState = AnalyticalRuleState.SILENT,
    confirmations: int = 2,
    cooldown_seconds: int = 86_400,
) -> AnalyticalRuleConfigurationUpdate:
    current = store.get(INITIAL_MARKET_ACTIVITY_RULE.rule_id)
    return AnalyticalRuleConfigurationUpdate(
        rule_id=current.rule_id,
        expected_fingerprint=current.semantic_fingerprint(),
        state=state,
        confirmations_required=confirmations,
        cooldown_seconds=cooldown_seconds,
        conditions=(
            AnalyticalConditionConfigurationUpdate(
                condition_id=current.conditions[0].condition_id,
                threshold=threshold,
                exit_threshold=exit_threshold,
            ),
        ),
    )


def test_registry_uses_packaged_defaults_without_creating_state(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    store = AnalyticalRuleRegistryStore(path, INITIAL_ANALYTICAL_RULES)

    snapshot = store.snapshot()

    assert not path.exists()
    assert snapshot.total_revisions == 0
    assert tuple(item.rule for item in snapshot.configurations) == INITIAL_ANALYTICAL_RULES
    assert all(not item.customized for item in snapshot.configurations)


def test_registry_persists_audited_revision_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    store = AnalyticalRuleRegistryStore(path, INITIAL_ANALYTICAL_RULES)

    outcome = store.update(
        _request(
            store,
            threshold="2.0",
            exit_threshold="1.6",
            confirmations=3,
            cooldown_seconds=172_800,
        ),
        recorded_at=_RECORDED_AT,
    )
    restarted = AnalyticalRuleRegistryStore(path, INITIAL_ANALYTICAL_RULES)
    current = restarted.get(INITIAL_MARKET_ACTIVITY_RULE.rule_id)

    assert outcome.changed is True
    assert outcome.revision_id is not None
    if not path.is_relative_to("/mnt"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert current.rule_version == "1.0.local.1"
    assert str(current.conditions[0].threshold) == "2.0"
    assert str(current.conditions[0].exit_threshold) == "1.6"
    assert current.confirmations_required == 3
    assert current.cooldown_seconds == 172_800
    assert restarted.snapshot().total_revisions == 1


def test_registry_noop_is_idempotent_and_does_not_create_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    store = AnalyticalRuleRegistryStore(path, INITIAL_ANALYTICAL_RULES)

    outcome = store.update(_request(store), recorded_at=_RECORDED_AT)

    assert outcome.changed is False
    assert outcome.revision_id is None
    assert not path.exists()


def test_registry_can_restore_defaults_without_erasing_revision_history(
    tmp_path: Path,
) -> None:
    store = AnalyticalRuleRegistryStore(tmp_path / "rules.json", INITIAL_ANALYTICAL_RULES)
    store.update(
        _request(store, threshold="2.0", exit_threshold="1.6"),
        recorded_at=_RECORDED_AT,
    )
    current = store.get(INITIAL_MARKET_ACTIVITY_RULE.rule_id)
    default = INITIAL_MARKET_ACTIVITY_RULE

    outcome = store.update(
        AnalyticalRuleConfigurationUpdate(
            rule_id=current.rule_id,
            expected_fingerprint=current.semantic_fingerprint(),
            state=default.state,
            confirmations_required=default.confirmations_required,
            cooldown_seconds=default.cooldown_seconds,
            conditions=tuple(
                AnalyticalConditionConfigurationUpdate(
                    condition_id=item.condition_id,
                    threshold=item.threshold,
                    exit_threshold=item.exit_threshold,
                )
                for item in default.conditions
            ),
        ),
        recorded_at=_RECORDED_AT + timedelta(seconds=1),
    )

    assert outcome.changed is True
    assert outcome.configuration.customized is False
    assert outcome.configuration.revision_count == 2
    assert outcome.configuration.rule.rule_version == "1.0.local.2"
    assert store.snapshot().total_revisions == 2


def test_registry_rejects_stale_or_incomplete_edits(tmp_path: Path) -> None:
    store = AnalyticalRuleRegistryStore(tmp_path / "rules.json", INITIAL_ANALYTICAL_RULES)
    stale = _request(store, threshold="2")
    store.update(stale, recorded_at=_RECORDED_AT)

    with pytest.raises(AnalyticalRuleRegistryConflictError, match="changed since"):
        store.update(stale, recorded_at=_RECORDED_AT + timedelta(seconds=1))

    current = store.get(INITIAL_MARKET_ACTIVITY_RULE.rule_id)
    incomplete = AnalyticalRuleConfigurationUpdate(
        rule_id=current.rule_id,
        expected_fingerprint=current.semantic_fingerprint(),
        state=current.state,
        confirmations_required=current.confirmations_required,
        cooldown_seconds=current.cooldown_seconds,
        conditions=(
            AnalyticalConditionConfigurationUpdate(
                condition_id="unknown-condition",
                threshold="1",
            ),
        ),
    )
    with pytest.raises(ValueError, match="exactly match"):
        store.update(incomplete, recorded_at=_RECORDED_AT + timedelta(seconds=2))


def test_registry_validates_hysteresis_numbers_and_corrupt_history(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    store = AnalyticalRuleRegistryStore(path, INITIAL_ANALYTICAL_RULES)
    with pytest.raises(ValidationError, match="must not exceed"):
        store.update(
            _request(store, threshold="1.5", exit_threshold="1.6"),
            recorded_at=_RECORDED_AT,
        )
    with pytest.raises(ValidationError, match="Decimal"):
        AnalyticalConditionConfigurationUpdate(
            condition_id="relative_volume_gte_1_5",
            threshold=1.5,
            exit_threshold="1.2",
        )
    with pytest.raises(ValidationError, match="integers"):
        AnalyticalRuleConfigurationUpdate.model_validate(
            {
                **_request(store).model_dump(mode="json"),
                "confirmations_required": True,
            }
        )

    path.write_text('{"schema_version":"analytical-rule-registry-state-v1"}', encoding="utf-8")
    assert store.load().revisions == ()
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(AaplOperationalStateError, match="malformed"):
        store.load()
