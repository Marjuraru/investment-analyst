"""Pure, typed and versioned domain membership policy for analytical contracts.

Part of DATA-CHASSIS-34.
Declares policy analytical-domain-membership-v1:
- Validates authorized analytical domains and their strict canonical metric namespaces:
  - market <-> prefix 'market.'
  - fundamental <-> prefix 'fundamental.'
  - valuation <-> prefix 'valuation.corporate.'
  - derivatives <-> prefix 'crypto.derivatives.'
  - events <-> prefix 'cazatiburones.'
- Rejects unknown domains, empty domains, mixed families, and cross-domain references.
- Pure and provider-independent; evaluates only typed semantic names and models.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Final
from uuid import UUID

from investment_analyst.core.models.diagnostic import DiagnosticResult
from investment_analyst.core.models.enums import DiagnosticMode

DOMAIN_MEMBERSHIP_POLICY_VERSION: Final[str] = "analytical-domain-membership-v1"


class AnalysisDomain(StrEnum):
    """Canonical analytical domains supported by the architecture."""

    MARKET = "market"
    FUNDAMENTAL = "fundamental"
    VALUATION = "valuation"
    DERIVATIVES = "derivatives"
    EVENTS = "events"


AUTHORIZED_DOMAIN_PREFIXES: Final[dict[str, str]] = {
    AnalysisDomain.MARKET.value: "market.",
    AnalysisDomain.FUNDAMENTAL.value: "fundamental.",
    AnalysisDomain.VALUATION.value: "valuation.corporate.",
    AnalysisDomain.DERIVATIVES.value: "crypto.derivatives.",
    AnalysisDomain.EVENTS.value: "cazatiburones.",
}


class DomainMembershipError(ValueError):
    """Raised when an analytical domain, metric namespace, or reference violates membership."""


def is_authorized_domain(domain: str) -> bool:
    """Return whether domain is an authorized canonical analytical domain."""
    if not isinstance(domain, str) or not domain.strip():
        return False
    return domain in AUTHORIZED_DOMAIN_PREFIXES


def require_authorized_domain(domain: str) -> AnalysisDomain:
    """Validate that domain is an authorized canonical domain, or raise DomainMembershipError."""
    if not isinstance(domain, str) or not domain.strip():
        raise DomainMembershipError("domain must be a non-empty string")
    try:
        return AnalysisDomain(domain)
    except ValueError as error:
        raise DomainMembershipError(f"unknown analytical domain: {domain!r}") from error


def metric_prefix_for_domain(domain: str | AnalysisDomain) -> str:
    """Return the mandatory metric key prefix for the given domain."""
    dom_str = domain.value if isinstance(domain, AnalysisDomain) else domain
    require_authorized_domain(dom_str)
    return AUTHORIZED_DOMAIN_PREFIXES[dom_str]


def domain_for_metric_key(metric_key: str) -> AnalysisDomain:
    """Infer the single authorized domain for a canonical metric key."""
    if not isinstance(metric_key, str) or not metric_key:
        raise DomainMembershipError("metric_key must be a non-empty string")
    for domain_name, prefix in AUTHORIZED_DOMAIN_PREFIXES.items():
        if metric_key.startswith(prefix):
            return AnalysisDomain(domain_name)
    raise DomainMembershipError(
        f"metric key {metric_key!r} does not belong to any authorized analytical domain"
    )


def validate_metric_key_for_domain(metric_key: str, domain: str | AnalysisDomain) -> None:
    """Require that metric_key belongs strictly to the declared analytical domain."""
    expected_domain = require_authorized_domain(
        domain.value if isinstance(domain, AnalysisDomain) else domain
    )
    expected_prefix = AUTHORIZED_DOMAIN_PREFIXES[expected_domain.value]
    if not isinstance(metric_key, str) or not metric_key.startswith(expected_prefix):
        raise DomainMembershipError(
            f"metric key {metric_key!r} does not belong to domain {expected_domain.value!r} "
            f"(must start with {expected_prefix!r})"
        )


def validate_metric_keys_for_domain(
    metric_keys: Collection[str], domain: str | AnalysisDomain
) -> None:
    """Require that every metric key in the collection belongs to the declared domain."""
    for key in metric_keys:
        validate_metric_key_for_domain(key, domain)


def validate_diagnostic_mode_for_domain(
    mode: DiagnosticMode | str, domain: str | AnalysisDomain
) -> None:
    """Validate compatibility between DiagnosticMode and AnalysisDomain.

    DiagnosticMode.MARKET admits exclusively market or derivatives domains.
    DiagnosticMode.FUNDAMENTAL admits exclusively fundamental domain.
    DiagnosticMode.UNIFIED is unauthorized.
    Valuation and events domains do not have authorized diagnostic modes yet.
    """
    dom = require_authorized_domain(domain.value if isinstance(domain, AnalysisDomain) else domain)
    mode_str = mode.value if hasattr(mode, "value") else str(mode)

    if mode_str == DiagnosticMode.UNIFIED.value:
        raise DomainMembershipError("diagnostic mode UNIFIED is unauthorized")

    if mode_str == DiagnosticMode.FUNDAMENTAL.value:
        if dom is not AnalysisDomain.FUNDAMENTAL:
            raise DomainMembershipError(
                f"diagnostic with FUNDAMENTAL mode cannot belong to domain {dom.value!r}"
            )
    elif mode_str == DiagnosticMode.MARKET.value:
        if dom not in (AnalysisDomain.MARKET, AnalysisDomain.DERIVATIVES):
            raise DomainMembershipError(
                "diagnostic with MARKET mode admits only market or derivatives "
                f"domains, not {dom.value!r}"
            )
    else:
        raise DomainMembershipError(f"unsupported diagnostic mode: {mode_str!r}")


def validate_diagnostic_internal_consistency(
    diagnostic: DiagnosticResult,
    metric_keys_by_id: Mapping[UUID, str],
) -> AnalysisDomain:
    """Require that all cited metrics in a diagnostic belong to the same authorized domain.

    Returns the unified domain.
    Raises DomainMembershipError on empty cited metrics, missing keys, or mixed domains.
    """
    cited_metric_ids: set[UUID] = set()
    for comp in diagnostic.components:
        cited_metric_ids.update(comp.metric_result_ids)
    for ev in diagnostic.evidence:
        cited_metric_ids.add(ev.metric_result_id)

    if not cited_metric_ids:
        # If diagnostic cites no metrics, check mode against market/fundamental
        mode_str = (
            diagnostic.mode.value if hasattr(diagnostic.mode, "value") else str(diagnostic.mode)
        )
        if mode_str == DiagnosticMode.UNIFIED.value:
            raise DomainMembershipError("diagnostic mode UNIFIED is unauthorized")
        if mode_str == DiagnosticMode.FUNDAMENTAL.value:
            return AnalysisDomain.FUNDAMENTAL
        if mode_str == DiagnosticMode.MARKET.value:
            return AnalysisDomain.MARKET
        raise DomainMembershipError(f"unsupported diagnostic mode: {mode_str!r}")

    domains: set[AnalysisDomain] = set()
    for mid in cited_metric_ids:
        key = metric_keys_by_id.get(mid)
        if key is None:
            raise DomainMembershipError(f"metric key not provided for cited metric {mid}")
        domains.add(domain_for_metric_key(key))

    if len(domains) > 1:
        domains_str = ", ".join(sorted(d.value for d in domains))
        raise DomainMembershipError(
            f"diagnostic {diagnostic.diagnostic_id} cites mixed metric domains: {domains_str}"
        )

    resolved_domain = next(iter(domains))
    validate_diagnostic_mode_for_domain(diagnostic.mode, resolved_domain)
    return resolved_domain


__all__ = [
    "AUTHORIZED_DOMAIN_PREFIXES",
    "DOMAIN_MEMBERSHIP_POLICY_VERSION",
    "AnalysisDomain",
    "DomainMembershipError",
    "domain_for_metric_key",
    "is_authorized_domain",
    "metric_prefix_for_domain",
    "require_authorized_domain",
    "validate_diagnostic_internal_consistency",
    "validate_diagnostic_mode_for_domain",
    "validate_metric_key_for_domain",
    "validate_metric_keys_for_domain",
]
