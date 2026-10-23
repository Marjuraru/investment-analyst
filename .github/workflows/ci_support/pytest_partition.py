"""Pytest plugin that collects, verifies, and filters exact CI shard selections.

The workflow runs this plugin in three modes:

``collect``
    Collect the complete universe and write it as strict JSON.
``shard``
    Verify the collected universe against the partition manifest, then execute
    exactly the selected node ids and write a terminal receipt.
``serial``
    Execute the complete universe, write the same receipt, and serve as the
    optional serial reference for the CI-T1 comparison.

Node ids are never passed through a shell or matched by substring. The plugin
reads them from a validated JSON selection file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import shards


class _State:
    def __init__(self, mode: str, shard: int) -> None:
        self.mode = mode
        self.shard = shard
        self.started = time.monotonic()
        self.universe: tuple[str, ...] = ()
        self.universe_digest = ""
        self.selected: tuple[str, ...] = ()
        self.reports: dict[str, list[pytest.TestReport]] = {}
        self.failure: str | None = None


_STATE: _State | None = None


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise pytest.UsageError(f"{name} is required")
    return value


def pytest_configure(config: pytest.Config) -> None:
    del config
    global _STATE
    mode = os.environ.get("CI_PARTITION_MODE", "").strip()
    if not mode:
        _STATE = None
        return
    if mode not in {"collect", "shard", "serial"}:
        raise pytest.UsageError("CI_PARTITION_MODE is invalid")
    shard = 0
    if mode == "shard":
        raw_shard = _required_environment("CI_SHARD")
        if not raw_shard.isdigit() or int(raw_shard) < 1:
            raise pytest.UsageError("CI_SHARD must be a positive integer")
        shard = int(raw_shard)
    _STATE = _State(mode, shard)


def _write_collection(state: _State) -> None:
    target = Path(_required_environment("CI_COLLECTION_OUT"))
    payload = {
        "schema_version": shards.COLLECTION_SCHEMA,
        "universe": list(state.universe),
        "count": len(state.universe),
        "universe_sha256": state.universe_digest,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _outcome(reports: list[pytest.TestReport]) -> str:
    if not reports:
        return "missing"
    if any(report.when == "call" and report.failed for report in reports):
        return "failed"
    if any(report.failed for report in reports):
        return "error"
    call = next((report for report in reports if report.when == "call"), None)
    if call is not None:
        if call.passed and hasattr(call, "wasxfail"):
            return "xpassed"
        if call.skipped and hasattr(call, "wasxfail"):
            return "xfailed"
        if call.passed:
            return "passed"
        if call.skipped:
            return "skipped"
    setup = next((report for report in reports if report.when == "setup"), None)
    if setup is not None and setup.skipped:
        return "skipped"
    return "missing"


def _terminal(state: _State, exitstatus: int) -> bool:
    if state.failure is not None or exitstatus not in {0, 1}:
        return False
    return all(
        _outcome(state.reports.get(nodeid, [])) in shards.TERMINAL_OUTCOMES
        for nodeid in state.selected
    )


def _write_receipt(state: _State, exitstatus: int, terminal: bool) -> None:
    target_value = os.environ.get("CI_RECEIPT_OUT", "")
    if not target_value:
        return
    outcomes = {nodeid: _outcome(state.reports.get(nodeid, [])) for nodeid in state.selected}
    durations = {
        nodeid: round(sum(report.duration for report in state.reports.get(nodeid, [])), 3)
        for nodeid in state.selected
    }
    payload = {
        "schema_version": shards.RECEIPT_SCHEMA,
        "mode": state.mode,
        "shard": state.shard,
        "source_sha": os.environ.get("GITHUB_SHA", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "universe_count": len(state.universe),
        "universe_sha256": state.universe_digest,
        "selected_count": len(state.selected),
        "selected_sha256": shards.universe_sha256(state.selected),
        "outcomes": outcomes,
        "durations": durations,
        "terminal": terminal,
        "exitstatus": exitstatus,
        "duration_seconds": round(time.monotonic() - state.started, 3),
    }
    target = Path(target_value)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _fail(state: _State, message: str) -> None:
    state.failure = message
    if state.mode != "collect":
        _write_receipt(state, 4, terminal=False)
    raise pytest.UsageError(f"CI partition failure: {message}")


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    del session
    state = _STATE
    if state is None:
        return
    nodeids = tuple(sorted(item.nodeid for item in items))
    if len(nodeids) != len(set(nodeids)):
        _fail(state, "pytest collected duplicate node ids")
    if not nodeids:
        _fail(state, "pytest collected an empty universe")
    digest = shards.universe_sha256(nodeids)
    state.universe = nodeids
    state.universe_digest = digest
    if state.mode == "collect":
        _write_collection(state)
        state.selected = nodeids
        return
    try:
        selection_path = Path(_required_environment("CI_SELECTION_FILE"))
    except pytest.UsageError as error:
        _fail(state, str(error))
        return
    try:
        selected = shards.load_selection(
            selection_path,
            mode=state.mode,
            shard=state.shard,
            source_sha=os.environ.get("GITHUB_SHA", ""),
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            expected_universe_sha256=digest,
            expected_universe_count=len(nodeids),
        )
    except shards.ContractError as error:
        _fail(state, str(error))
        return
    if state.mode == "shard":
        selected_set = set(selected)
        missing = selected_set - set(nodeids)
        if missing:
            _fail(state, f"selected node id is absent from collection: {sorted(missing)[0]}")
        selected_items = sorted(
            (item for item in items if item.nodeid in selected_set),
            key=lambda item: item.nodeid,
        )
        if not selected_items:
            _fail(state, "shard selection is empty")
        deselected = [item for item in items if item.nodeid not in selected_set]
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected_items
    else:
        items[:] = sorted(items, key=lambda item: item.nodeid)
    state.selected = tuple(item.nodeid for item in items)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    state = _STATE
    if state is None or state.mode == "collect":
        return
    state.reports.setdefault(report.nodeid, []).append(report)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session
    state = _STATE
    if state is None or state.mode == "collect":
        return
    if state.failure is not None:
        return
    _write_receipt(state, exitstatus, _terminal(state, exitstatus))
