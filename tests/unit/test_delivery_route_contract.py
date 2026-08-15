"""Contract checks for the compact, non-authorizing delivery route."""

from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {"DONE", "NEXT", "PLANNED", "BLOCKED", "DEFERRED"}


@dataclass(frozen=True, slots=True)
class RouteItem:
    identifier: str
    status: str
    evidence: str
    work_blocks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteTransition:
    effect: str
    item: str
    proposed_status: str
    next_item: str | None
    issue_closed: bool | None
    pr_merged: bool | None
    merge_in_main: bool | None


def _validate(items: tuple[RouteItem, ...]) -> None:
    identifiers = [item.identifier for item in items]
    assert len(identifiers) == len(set(identifiers))
    assert all(item.status in ALLOWED for item in items)
    assert sum(item.status == "NEXT" for item in items) == 1
    for item in items:
        if item.status == "DONE":
            assert item.evidence
        assert item.work_blocks


def _route_items(release_plan: str) -> tuple[RouteItem, ...]:
    rows = [line for line in release_plan.splitlines() if line.startswith("| `") and " | `" in line]
    return tuple(
        RouteItem(
            identifier=columns[1].strip("` "),
            status=columns[2].strip("` "),
            evidence=columns[4].strip(),
            work_blocks=("route",),
        )
        for line in rows
        for columns in [line.split("|")]
    )


def _route_status(items: tuple[RouteItem, ...], identifier: str) -> str:
    return next(item.status for item in items if item.identifier == identifier)


def _validate_candidate_transition(
    main_items: tuple[RouteItem, ...],
    candidate_items: tuple[RouteItem, ...],
    transition: RouteTransition,
) -> None:
    assert transition.effect in {"NONE", "ADVANCES", "COMPLETES"}
    main_status = _route_status(main_items, transition.item)
    candidate_status = _route_status(candidate_items, transition.item)
    if transition.effect == "NONE":
        assert candidate_items == main_items
        return
    if transition.effect == "ADVANCES":
        assert main_status != "DONE"
        assert candidate_status != "DONE"
        if candidate_status != main_status:
            assert main_status == "NEXT"
            assert candidate_status == "BLOCKED"
            assert transition.proposed_status == "BLOCKED"
            assert transition.next_item is not None
            assert _route_status(candidate_items, transition.next_item) == "NEXT"
        return
    assert main_status != "DONE"
    assert transition.proposed_status == "DONE"
    assert candidate_status == "DONE"
    assert transition.next_item is not None
    assert _route_status(candidate_items, transition.next_item) == "NEXT"


def _next_is_stale(
    main_items: tuple[RouteItem, ...],
    transition: RouteTransition,
) -> bool:
    if transition.effect != "COMPLETES":
        return False
    if None in (transition.issue_closed, transition.pr_merged, transition.merge_in_main):
        raise ValueError("route lifecycle evidence is indeterminate")
    if not all((transition.issue_closed, transition.pr_merged, transition.merge_in_main)):
        return False
    return _route_status(main_items, transition.item) == "NEXT"


def test_route_is_compact_non_authorizing_and_reconciles_mkt3() -> None:
    release_plan = (ROOT / "docs/basic_functional_release_plan.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs/product_roadmap.md").read_text(encoding="utf-8")

    assert release_plan.count("## Ruta táctica vigente") == 1
    assert "ACTIVE` se deriva exclusivamente" in release_plan
    assert "`MARKET-COMPARISON` | `DONE`" in release_plan
    assert "MKT-3/#56/#57" in release_plan
    assert "benchmarks sectoriales" in release_plan
    assert "PREDICTIVE-RESEARCH` | `DEFERRED`" in release_plan
    assert "Sólo main expresa el estado integrado" in release_plan
    assert "`DELIVERY-GOVERNANCE` | `DONE`" in release_plan
    assert "`ANALYST-READINESS` | `BLOCKED`" in release_plan
    assert "OPS-2/#66" in release_plan
    assert "insufficient_local_dates" in release_plan
    assert "`VALUATION-HISTORY` | `NEXT`" in release_plan
    assert "comparación normalizada de varios activos" not in roadmap
    assert "beta y correlación frente a un benchmark identificado" not in roadmap
    _validate(_route_items(release_plan))


def test_plan_contract_names_strategy_route_and_live_reconciliation() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    protocol = (ROOT / "docs/development_protocol.md").read_text(encoding="utf-8")
    plan_skill = (ROOT / ".agents/skills/plan/SKILL.md").read_text(encoding="utf-8")

    for text in (agents, protocol, plan_skill):
        assert "docs/product_roadmap.md" in text
        assert "docs/basic_functional_release_plan.md" in text
        assert "main/GitHub" in text
    assert "control plane → estrategia" in agents
    assert "razón y evidencia viva" in plan_skill
    assert "route_effect" in protocol
    assert "route_effect" in plan_skill


def test_candidate_route_lifecycle_is_conditional_and_requires_one_next() -> None:
    main_items = (
        RouteItem("DELIVERY-GOVERNANCE", "NEXT", "live", ("DEV-8", "DEV-9")),
        RouteItem("ANALYST-READINESS", "PLANNED", "pending", ("BASE-15", "OPS-1")),
    )
    candidate_items = (
        RouteItem("DELIVERY-GOVERNANCE", "DONE", "DEV-10", ("DEV-8", "DEV-9", "DEV-10")),
        RouteItem("ANALYST-READINESS", "NEXT", "pending", ("BASE-15", "OPS-1")),
    )
    transition = RouteTransition(
        "COMPLETES",
        "DELIVERY-GOVERNANCE",
        "DONE",
        "ANALYST-READINESS",
        False,
        False,
        False,
    )

    _validate_candidate_transition(main_items, candidate_items, transition)
    assert not _next_is_stale(main_items, transition)


@pytest.mark.parametrize(
    "transition",
    [
        RouteTransition("NONE", "A", "NEXT", None, False, False, False),
        RouteTransition("ADVANCES", "A", "NEXT", None, False, False, False),
    ],
)
def test_none_and_advances_do_not_close_or_churn_route(
    transition: RouteTransition,
) -> None:
    main_items = (RouteItem("A", "NEXT", "live", ("DEV-1",)),)
    _validate_candidate_transition(main_items, main_items, transition)


def test_advances_can_block_a_noncompleted_item_and_choose_one_next() -> None:
    main_items = (
        RouteItem("ANALYST-READINESS", "NEXT", "pending", ("OPS-2",)),
        RouteItem("VALUATION-HISTORY", "PLANNED", "pending", ("route",)),
    )
    candidate_items = (
        RouteItem("ANALYST-READINESS", "BLOCKED", "#66 insufficient dates", ("OPS-2",)),
        RouteItem("VALUATION-HISTORY", "NEXT", "pending", ("route",)),
    )
    transition = RouteTransition(
        "ADVANCES",
        "ANALYST-READINESS",
        "BLOCKED",
        "VALUATION-HISTORY",
        None,
        None,
        None,
    )

    _validate_candidate_transition(main_items, candidate_items, transition)
    assert _route_status(candidate_items, "ANALYST-READINESS") == "BLOCKED"
    assert _route_status(candidate_items, "VALUATION-HISTORY") == "NEXT"


def test_plan_rejects_integrated_completion_left_as_next_without_name_inference() -> None:
    main_items = (
        RouteItem("DELIVERY-GOVERNANCE", "NEXT", "live", ("one", "two")),
        RouteItem("ANALYST-READINESS", "PLANNED", "pending", ("three",)),
    )
    integrated = RouteTransition(
        "COMPLETES",
        "DELIVERY-GOVERNANCE",
        "DONE",
        "ANALYST-READINESS",
        True,
        True,
        True,
    )
    assert _next_is_stale(main_items, integrated)


@pytest.mark.parametrize(
    "transition",
    [
        RouteTransition("COMPLETES", "A", "DONE", "B", False, False, False),
        RouteTransition("COMPLETES", "A", "DONE", "B", True, False, False),
        RouteTransition("COMPLETES", "A", "DONE", "B", True, True, False),
    ],
)
def test_open_unmerged_or_not_integrated_completion_does_not_change_main(
    transition: RouteTransition,
) -> None:
    main_items = (
        RouteItem("A", "NEXT", "live", ("one",)),
        RouteItem("B", "PLANNED", "pending", ("two",)),
    )

    assert not _next_is_stale(main_items, transition)


def test_plan_fails_closed_for_indeterminate_lifecycle_evidence() -> None:
    main_items = (RouteItem("A", "NEXT", "live", ("one",)),)
    indeterminate = RouteTransition("COMPLETES", "A", "DONE", None, None, True, True)

    with pytest.raises(ValueError, match="indeterminate"):
        _next_is_stale(main_items, indeterminate)


def test_route_allows_many_to_many_work_block_relationships() -> None:
    _validate(
        (
            RouteItem("A", "DONE", "#1", ("DEV-1", "DEV-2")),
            RouteItem("B", "NEXT", "hypothesis", ("DEV-2",)),
        )
    )


@pytest.mark.parametrize(
    "items",
    [
        (
            RouteItem("A", "NEXT", "x", ("DEV-1",)),
            RouteItem("A", "PLANNED", "x", ("DEV-2",)),
        ),
        (RouteItem("A", "UNKNOWN", "x", ("DEV-1",)),),
        (
            RouteItem("A", "NEXT", "x", ("DEV-1",)),
            RouteItem("B", "NEXT", "x", ("DEV-2",)),
        ),
        (RouteItem("A", "DONE", "", ("DEV-1",)),),
        (RouteItem("A", "NEXT", "x", ()),),
    ],
)
def test_route_rejects_invalid_status_identity_evidence_or_relationship(
    items: tuple[RouteItem, ...],
) -> None:
    with pytest.raises(AssertionError):
        _validate(items)
