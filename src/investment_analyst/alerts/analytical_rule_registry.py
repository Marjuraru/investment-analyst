"""Versioned local configuration for deterministic analytical screening rules."""

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.alerts.analytical_models import (
    AnalyticalRuleState,
    AnalyticalScreeningCondition,
    AnalyticalScreeningRule,
    ScreeningDecimal,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_RULE_REVISION_NAMESPACE = UUID("f4f460c3-231c-520b-bdac-354cfae43857")
_MAX_RULE_REVISIONS = 25_000


class AnalyticalRuleRegistryConflictError(RuntimeError):
    """Raised when an optimistic rule update targets a stale fingerprint."""


class AnalyticalConditionConfigurationUpdate(ContractModel):
    """Editable numeric fields for one otherwise immutable condition contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    condition_id: NonEmptyStr
    threshold: ScreeningDecimal
    exit_threshold: ScreeningDecimal | None = None


class AnalyticalRuleConfigurationUpdate(ContractModel):
    """Optimistically locked update of one catalog rule."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["analytical-rule-configuration-update-v1"] = (
        "analytical-rule-configuration-update-v1"
    )
    rule_id: NonEmptyStr
    expected_fingerprint: NonEmptyStr
    state: AnalyticalRuleState
    confirmations_required: int = Field(ge=1, le=20)
    cooldown_seconds: int = Field(ge=0, le=31_536_000)
    conditions: tuple[AnalyticalConditionConfigurationUpdate, ...]

    @field_validator("confirmations_required", "cooldown_seconds", mode="before")
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans before Pydantic can coerce them to integers."""
        if isinstance(value, bool):
            raise ValueError("rule configuration counts must be integers")
        return value

    @model_validator(mode="after")
    def validate_update(self) -> "AnalyticalRuleConfigurationUpdate":
        """Require one deterministic ordered set of condition updates."""
        identifiers = tuple(item.condition_id for item in self.conditions)
        if not identifiers:
            raise ValueError("rule configuration update must include conditions")
        if identifiers != tuple(sorted(set(identifiers))):
            raise ValueError("rule condition updates must be unique and sorted")
        return self


class AnalyticalRuleRevision(ContractModel):
    """Append-only audit record for one complete local rule revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["analytical-rule-revision-v1"] = "analytical-rule-revision-v1"
    revision_id: UUID
    rule: AnalyticalScreeningRule
    previous_fingerprint: NonEmptyStr
    recorded_at: UTCDateTime
    actor: Literal["local_user"] = "local_user"

    @model_validator(mode="after")
    def validate_revision(self) -> "AnalyticalRuleRevision":
        """Verify deterministic revision identity."""
        if self.revision_id != analytical_rule_revision_id(
            self.rule,
            self.previous_fingerprint,
            self.recorded_at,
        ):
            raise ValueError("analytical rule revision_id is not deterministic")
        if ".local." not in self.rule.rule_version:
            raise ValueError("local analytical revisions require a local rule_version")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact audit primitives."""
        return self.model_dump(mode="json")


class AnalyticalRuleRegistryState(ContractModel):
    """Recoverable append-only local rule history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["analytical-rule-registry-state-v1"] = (
        "analytical-rule-registry-state-v1"
    )
    revisions: tuple[AnalyticalRuleRevision, ...] = Field(
        default=(),
        max_length=_MAX_RULE_REVISIONS,
    )

    @model_validator(mode="after")
    def validate_history(self) -> "AnalyticalRuleRegistryState":
        """Require unique identities and deterministic chronological ordering."""
        identifiers = tuple(item.revision_id for item in self.revisions)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("analytical rule registry contains duplicate revisions")
        ordering = tuple(
            (item.recorded_at, item.rule.rule_id, str(item.revision_id)) for item in self.revisions
        )
        if ordering != tuple(sorted(ordering)):
            raise ValueError("analytical rule revisions are not deterministically ordered")
        versions = tuple((item.rule.rule_id, item.rule.rule_version) for item in self.revisions)
        if len(versions) != len(set(versions)):
            raise ValueError("analytical rule versions must be unique per rule")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the complete recoverable registry document."""
        return {
            "schema_version": self.schema_version,
            "revisions": [item.to_json_dict() for item in self.revisions],
        }


class AnalyticalRuleConfiguration(ContractModel):
    """One current rule paired with its immutable packaged default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: AnalyticalScreeningRule
    default_rule: AnalyticalScreeningRule
    fingerprint: NonEmptyStr
    customized: bool
    revision_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_configuration(self) -> "AnalyticalRuleConfiguration":
        """Keep summary metadata aligned with rule semantics."""
        if self.rule.rule_id != self.default_rule.rule_id:
            raise ValueError("configured and default rule IDs must match")
        if self.fingerprint != self.rule.semantic_fingerprint():
            raise ValueError("configured rule fingerprint is inconsistent")
        expected_customized = not _matches_default_configuration(
            self.rule,
            self.default_rule,
        )
        if self.customized != expected_customized:
            raise ValueError("customized must match the current rule semantics")
        return self


class AnalyticalRuleRegistrySnapshot(ContractModel):
    """Bounded API-safe view of all editable rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["analytical-rule-registry-snapshot-v1"] = (
        "analytical-rule-registry-snapshot-v1"
    )
    configurations: tuple[AnalyticalRuleConfiguration, ...]
    total_revisions: int = Field(ge=0)

    def to_json_dict(self) -> dict[str, object]:
        """Return complete rule contracts without internal filesystem details."""
        return self.model_dump(mode="json")


class AnalyticalRuleUpdateOutcome(ContractModel):
    """Idempotent update response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    changed: bool
    configuration: AnalyticalRuleConfiguration
    revision_id: UUID | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "AnalyticalRuleUpdateOutcome":
        """Require a revision identity exactly when state changed."""
        if self.changed != (self.revision_id is not None):
            raise ValueError("rule update revision identity is inconsistent")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return API-safe update metadata."""
        return self.model_dump(mode="json")


class AnalyticalRuleRegistryStore:
    """Atomically manage packaged defaults and append-only local overrides."""

    def __init__(
        self,
        path: Path,
        defaults: tuple[AnalyticalScreeningRule, ...],
    ) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._defaults = tuple(sorted(defaults, key=lambda item: (item.rule_id, item.rule_version)))
        identifiers = tuple(item.rule_id for item in self._defaults)
        if not identifiers or len(identifiers) != len(set(identifiers)):
            raise ValueError("analytical rule defaults must have unique rule IDs")
        self._defaults_by_id = {item.rule_id: item for item in self._defaults}
        self._lock = threading.RLock()

    def load(self) -> AnalyticalRuleRegistryState:
        """Load and validate local revisions without creating a missing file."""
        with self._lock:
            if not self._path.exists():
                return AnalyticalRuleRegistryState()
            try:
                state = AnalyticalRuleRegistryState.model_validate_json(
                    self._path.read_text(encoding="utf-8")
                )
                self._project(state)
                return state
            except (OSError, UnicodeError, ValueError) as error:
                raise AaplOperationalStateError(
                    "analytical rule registry state is malformed or unreadable"
                ) from error

    def rules(self) -> tuple[AnalyticalScreeningRule, ...]:
        """Return current rules in deterministic catalog order."""
        with self._lock:
            return tuple(item.rule for item in self.snapshot().configurations)

    def get(self, rule_id: str) -> AnalyticalScreeningRule:
        """Return one current configured rule."""
        normalized = rule_id.strip()
        for configuration in self.snapshot().configurations:
            if configuration.rule.rule_id == normalized:
                return configuration.rule
        raise ValueError("analytical rule does not exist")

    def snapshot(self) -> AnalyticalRuleRegistrySnapshot:
        """Return current defaults, overrides, and revision counts."""
        with self._lock:
            state = self.load()
            current, counts = self._project(state)
            return AnalyticalRuleRegistrySnapshot(
                configurations=tuple(
                    AnalyticalRuleConfiguration(
                        rule=current[default.rule_id],
                        default_rule=default,
                        fingerprint=current[default.rule_id].semantic_fingerprint(),
                        customized=not _matches_default_configuration(
                            current[default.rule_id],
                            default,
                        ),
                        revision_count=counts[default.rule_id],
                    )
                    for default in self._defaults
                ),
                total_revisions=len(state.revisions),
            )

    def update(
        self,
        request: AnalyticalRuleConfigurationUpdate,
        *,
        recorded_at: datetime,
    ) -> AnalyticalRuleUpdateOutcome:
        """Append one validated revision or return an idempotent no-op."""
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        recorded_at = recorded_at.astimezone(UTC)
        with self._lock:
            state = self.load()
            current_by_id, counts = self._project(state)
            current = current_by_id.get(request.rule_id)
            if current is None:
                raise ValueError("analytical rule does not exist")
            if request.expected_fingerprint != current.semantic_fingerprint():
                raise AnalyticalRuleRegistryConflictError(
                    "analytical rule changed since it was loaded"
                )
            condition_updates = {item.condition_id: item for item in request.conditions}
            expected_ids = tuple(item.condition_id for item in current.conditions)
            if tuple(condition_updates) != expected_ids:
                raise ValueError("rule update conditions must exactly match the current rule")
            updated_conditions = tuple(
                AnalyticalScreeningCondition.model_validate(
                    {
                        **condition.model_dump(mode="python"),
                        "threshold": condition_updates[condition.condition_id].threshold,
                        "exit_threshold": condition_updates[condition.condition_id].exit_threshold,
                    }
                )
                for condition in current.conditions
            )
            candidate_payload = {
                **current.model_dump(mode="python"),
                "state": request.state,
                "confirmations_required": request.confirmations_required,
                "cooldown_seconds": request.cooldown_seconds,
                "conditions": updated_conditions,
            }
            same_version_candidate = AnalyticalScreeningRule.model_validate(candidate_payload)
            if same_version_candidate == current:
                return AnalyticalRuleUpdateOutcome(
                    changed=False,
                    configuration=self._configuration(
                        current,
                        self._defaults_by_id[current.rule_id],
                        counts[current.rule_id],
                    ),
                )
            base_version = current.rule_version.partition(".local.")[0]
            next_version = f"{base_version}.local.{counts[current.rule_id] + 1}"
            updated = AnalyticalScreeningRule.model_validate(
                {
                    **candidate_payload,
                    "rule_version": next_version,
                }
            )
            revision = AnalyticalRuleRevision(
                revision_id=analytical_rule_revision_id(
                    updated,
                    current.semantic_fingerprint(),
                    recorded_at,
                ),
                rule=updated,
                previous_fingerprint=current.semantic_fingerprint(),
                recorded_at=recorded_at,
            )
            revisions = tuple(
                sorted(
                    (*state.revisions, revision),
                    key=lambda item: (
                        item.recorded_at,
                        item.rule.rule_id,
                        str(item.revision_id),
                    ),
                )
            )
            snapshot = AnalyticalRuleRegistryState(revisions=revisions)
            self._project(snapshot)
            self._write(snapshot)
            return AnalyticalRuleUpdateOutcome(
                changed=True,
                configuration=self._configuration(
                    updated,
                    self._defaults_by_id[updated.rule_id],
                    counts[updated.rule_id] + 1,
                ),
                revision_id=revision.revision_id,
            )

    def _project(
        self,
        state: AnalyticalRuleRegistryState,
    ) -> tuple[dict[str, AnalyticalScreeningRule], dict[str, int]]:
        current = dict(self._defaults_by_id)
        counts = {rule_id: 0 for rule_id in self._defaults_by_id}
        for revision in state.revisions:
            rule_id = revision.rule.rule_id
            prior = current.get(rule_id)
            if prior is None:
                raise ValueError("analytical revision references an unknown default rule")
            if revision.previous_fingerprint != prior.semantic_fingerprint():
                raise ValueError("analytical rule revision chain is inconsistent")
            current[rule_id] = revision.rule
            counts[rule_id] += 1
        return current, counts

    @staticmethod
    def _configuration(
        rule: AnalyticalScreeningRule,
        default: AnalyticalScreeningRule,
        revision_count: int,
    ) -> AnalyticalRuleConfiguration:
        return AnalyticalRuleConfiguration(
            rule=rule,
            default_rule=default,
            fingerprint=rule.semantic_fingerprint(),
            customized=not _matches_default_configuration(rule, default),
            revision_count=revision_count,
        )

    def _write(self, state: AnalyticalRuleRegistryState) -> None:
        document = (
            json.dumps(
                state.to_json_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        descriptor: int | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise AaplOperationalStateError(
                "analytical rule registry state could not be written"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


def analytical_rule_revision_id(
    rule: AnalyticalScreeningRule,
    previous_fingerprint: str,
    recorded_at: datetime,
) -> UUID:
    """Return one stable identity for a complete local rule revision."""
    payload = {
        "rule_fingerprint": rule.semantic_fingerprint(),
        "previous_fingerprint": previous_fingerprint,
        "recorded_at": recorded_at.astimezone(UTC).isoformat(),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return uuid5(_RULE_REVISION_NAMESPACE, encoded)


def _matches_default_configuration(
    rule: AnalyticalScreeningRule,
    default: AnalyticalScreeningRule,
) -> bool:
    return rule.model_dump(mode="python", exclude={"rule_version"}) == default.model_dump(
        mode="python",
        exclude={"rule_version"},
    )


__all__ = [
    "AnalyticalConditionConfigurationUpdate",
    "AnalyticalRuleConfiguration",
    "AnalyticalRuleConfigurationUpdate",
    "AnalyticalRuleRegistryConflictError",
    "AnalyticalRuleRegistrySnapshot",
    "AnalyticalRuleRegistryState",
    "AnalyticalRuleRegistryStore",
    "AnalyticalRuleRevision",
    "AnalyticalRuleUpdateOutcome",
    "analytical_rule_revision_id",
]
