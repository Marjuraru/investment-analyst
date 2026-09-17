"""Version-aware point-in-time cut eligibility for persisted metric reads.

The module answers exactly one question: given a persisted ``MetricResult`` and a
``known_at`` cut, is the row eligible for that cut, and under which metric
identity version?

The rule is pure, total, in-memory and O(1): it never reads storage or the
filesystem, it never computes an identity, and it never raises for a historical
identifier. The identity version is resolved only from the shape of
``result_id`` — the UUID version bits — and never from ``parameters``, the name
of a key, the ``metric_key`` or the asset domain.

The version vocabulary mirrors the audited v1/v2 identity convention of this
package instead of importing its adapter from production code: the audited
identity regression test forbids any production reference to that module, so the
v2 rule is consumed — never redefined — and the case-by-case equivalence with the
adapter plus the totality it deliberately does not provide are pinned by
``tests/unit/analytics/test_metric_identity_cut.py``.

- v1 (and any degraded identifier) keeps the legacy rule: ``parameters["known_at"]``
  is mandatory and must equal the cut. Malformed legacy parameters are reported as
  data so each read path preserves its own legacy error class and message.
- v2 is eligible when ``available_at <= known_at`` and must not carry the legacy
  ``known_at`` parameter; carrying it violates the identity contract and fails
  closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from investment_analyst.core.models import MetricResult

_LEGACY_KNOWN_AT_PARAMETER = "known_at"
_V2_UUID_VERSION = 8


class MetricCutContractError(RuntimeError):
    """Raised when a metric row violates the contract of its resolved version."""


class CutIdentityVersion(StrEnum):
    """Identity version label of the cut rule; mirrors the audited v1/v2 vocabulary."""

    V1 = "v1"
    V2 = "v2"


class LegacyKnownAtState(StrEnum):
    """Outcome of probing the legacy ``known_at`` parameter of a metric row."""

    VALUE = "value"
    ABSENT = "absent"
    NOT_STRING = "not_string"
    UNPARSEABLE = "unparseable"
    NAIVE = "naive"


@dataclass(frozen=True, slots=True)
class LegacyKnownAtProbe:
    """Total, never-raising probe of the legacy ``known_at`` parameter."""

    state: LegacyKnownAtState
    value: datetime | None = None


@dataclass(frozen=True, slots=True)
class MetricCutEligibility:
    """Eligibility of one metric row at one cut, and its resolved identity version."""

    version: CutIdentityVersion
    eligible: bool
    legacy: LegacyKnownAtProbe | None = None


def resolve_cut_identity_version(result: MetricResult) -> CutIdentityVersion:
    """Resolve the identity version from the ``result_id`` UUID shape only.

    UUIDv5 is v1 and UUIDv8 is v2. Any other UUID version — including identifiers
    without RFC 4122 variant bits — degrades to the legacy rule instead of
    raising, so no historical row of any domain becomes unreadable.
    """
    if result.result_id.version == _V2_UUID_VERSION:
        return CutIdentityVersion.V2
    return CutIdentityVersion.V1


def probe_legacy_known_at(result: MetricResult) -> LegacyKnownAtProbe:
    """Read the legacy ``known_at`` parameter without raising, preserving its states."""
    value = result.parameters.get(_LEGACY_KNOWN_AT_PARAMETER)
    if value is None:
        return LegacyKnownAtProbe(LegacyKnownAtState.ABSENT)
    if not isinstance(value, str):
        return LegacyKnownAtProbe(LegacyKnownAtState.NOT_STRING)
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return LegacyKnownAtProbe(LegacyKnownAtState.UNPARSEABLE)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return LegacyKnownAtProbe(LegacyKnownAtState.NAIVE)
    return LegacyKnownAtProbe(LegacyKnownAtState.VALUE, parsed.astimezone(UTC))


def require_absent_legacy_cut_parameter(result: MetricResult) -> None:
    """Fail closed when a v2 row still carries the legacy ``known_at`` parameter."""
    if _LEGACY_KNOWN_AT_PARAMETER in result.parameters:
        raise MetricCutContractError(
            f"metric result {result.result_id} carries the legacy known_at parameter "
            "under metric identity v2"
        )


def metric_cut_eligibility(result: MetricResult, known_at: datetime) -> MetricCutEligibility:
    """Return cut eligibility and identity version for one metric row.

    Raises ``MetricCutContractError`` when a v2 row carries the legacy
    ``known_at`` parameter. Legacy malformed parameters are reported through
    ``MetricCutEligibility.legacy`` instead of raising.
    """
    if known_at.tzinfo is None or known_at.utcoffset() is None:
        raise ValueError("cut known_at must include timezone information")
    cut = known_at.astimezone(UTC)
    version = resolve_cut_identity_version(result)
    if version is CutIdentityVersion.V2:
        require_absent_legacy_cut_parameter(result)
        return MetricCutEligibility(
            version=version,
            eligible=result.available_at <= cut,
        )
    legacy = probe_legacy_known_at(result)
    return MetricCutEligibility(
        version=version,
        eligible=legacy.state is LegacyKnownAtState.VALUE and legacy.value == cut,
        legacy=legacy,
    )


__all__ = [
    "CutIdentityVersion",
    "LegacyKnownAtProbe",
    "LegacyKnownAtState",
    "MetricCutContractError",
    "MetricCutEligibility",
    "metric_cut_eligibility",
    "probe_legacy_known_at",
    "require_absent_legacy_cut_parameter",
    "resolve_cut_identity_version",
]
