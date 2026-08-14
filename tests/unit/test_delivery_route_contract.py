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


def test_route_is_compact_non_authorizing_and_reconciles_mkt3() -> None:
    release_plan = (ROOT / "docs/basic_functional_release_plan.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs/product_roadmap.md").read_text(encoding="utf-8")

    assert release_plan.count("## Ruta táctica vigente") == 1
    assert "ACTIVE` se deriva exclusivamente" in release_plan
    assert "`MARKET-COMPARISON` | `DONE`" in release_plan
    assert "MKT-3/#56/#57" in release_plan
    assert "benchmarks sectoriales" in release_plan
    assert "PREDICTIVE-RESEARCH` | `DEFERRED`" in release_plan
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
