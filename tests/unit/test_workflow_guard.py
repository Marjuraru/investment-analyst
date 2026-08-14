from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_workflow_guards",
    Path(__file__).resolve().parents[2] / "scripts" / "check_workflow_guards.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["check_workflow_guards"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

from check_workflow_guards import (  # noqa: E402
    CheckSnapshot,
    CommentSnapshot,
    GuardSnapshot,
    IssueSnapshot,
    PullRequestSnapshot,
    SmokeSnapshot,
    evaluate,
    parse_marker,
    parse_work_block,
)

ROOT = Path(__file__).resolve().parents[2]
FULL_SHA = "a" * 40
BASE_SHA = "b" * 40


def _issue(body: str) -> IssueSnapshot:
    return IssueSnapshot(58, "OPEN", frozenset({"workflow:active"}), body)


def _body(profile: str = "CRITICAL", policy: str = "HUMAN") -> str:
    return f"""<!-- development-workflow:work-block-v1 -->

- **Work Block ID:** `DEV-7`.
- **Profile:** `{profile}`.
- **finalize_policy:** `{policy}`.
- **Base remota exacta:** `origin/main@{BASE_SHA}`.
- **Expected branch:** `codex/dev-7-finalize-policy-guard`.
- **Writer role:** `BUILD`.
"""


def _pr() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        59,
        "OPEN",
        "codex/dev-7-finalize-policy-guard",
        FULL_SHA,
        "main",
        BASE_SHA,
        True,
    )


def _marker(role: str, status: str = "PASS", *, payload: str = "evidence=pass") -> CommentSnapshot:
    reviewer = "reviewer=gemini-fresh\n" if role == "audit" else ""
    return CommentSnapshot(
        10 if role == "build" else 11,
        f"<!-- development-workflow:{role}-v1\n"
        f"block=DEV-7\nsha={FULL_SHA}\nstatus={status}\n{reviewer}-->\n{payload}\n",
    )


def _snapshot(
    *,
    body: str | None = None,
    comments: tuple[CommentSnapshot, ...] = (_marker("build"), _marker("audit")),
    smoke: str | None = "PASS",
    requested_changes: bool | None = False,
    open_threads: int | None = 0,
) -> GuardSnapshot:
    return GuardSnapshot(
        issue=_issue(body or _body()),
        pull_request=_pr(),
        comments=comments,
        checks=(CheckSnapshot("Python 3.12 quality", "completed", "success"),),
        smoke=SmokeSnapshot(smoke, "smoke evidence"),
        requested_changes=requested_changes,
        open_threads=open_threads,
    )


def test_metadata_requires_unique_structural_fields_and_human_critical() -> None:
    assert parse_work_block(_body()).policy == "HUMAN"
    with pytest.raises(ValueError, match="duplicate Work Block field"):
        parse_work_block(_body() + "- **Profile:** `STANDARD`.\n")
    with pytest.raises(ValueError, match="CRITICAL requires"):
        parse_work_block(_body(policy="AUTO"))
    with pytest.raises(ValueError, match="missing Work Block fields"):
        parse_work_block(_body().replace("- **Writer role:** `BUILD`.\n", ""))


@pytest.mark.parametrize(
    "body",
    [
        "development-workflow:audit-v1 in prose",
        "<!-- development-workflow:audit-v1\nblock=DEV-7\n",
        "<!-- development-workflow:audit-v1\nblock=DEV-7\nsha="
        + FULL_SHA
        + "\nstatus=PASS\nreviewer=x\nextra=y\n-->\n",
        "<!-- development-workflow:audit-v1\nblock=DEV-7\nsha="
        + FULL_SHA
        + "\nstatus=PASS\nreviewer=x\n-->\nsecond development-workflow:build-v1",
    ],
)
def test_reserved_marker_tokens_are_never_ignored(body: str) -> None:
    with pytest.raises(ValueError):
        parse_marker(CommentSnapshot(1, body))


def test_valid_marker_requires_structural_start_and_exact_schema() -> None:
    marker = parse_marker(_marker("audit"))
    assert marker is not None
    assert marker.role == "audit"
    assert marker.status == "PASS"
    with pytest.raises(ValueError, match="malformed marker structure"):
        parse_marker(CommentSnapshot(1, " \n" + _marker("audit").body))
    with pytest.raises(ValueError, match="unknown or missing"):
        parse_marker(
            CommentSnapshot(
                1,
                _marker("audit").body.replace(
                    "reviewer=gemini-fresh", "reviewer=gemini-fresh\nfoo=bar"
                ),
            )
        )


def test_equivalent_duplicates_return_plan_but_finalize_fails_closed() -> None:
    duplicate = CommentSnapshot(12, _marker("build").body)
    result = evaluate(_snapshot(comments=(_marker("build"), duplicate, _marker("audit"))))
    assert result.decision == "GUARD FAILURE"
    assert "reconciliation" in result.reasons[0]
    assert result.mutation_plan == ("supersede build comment 12",)


def test_non_equivalent_duplicates_fail_without_mutation_plan() -> None:
    duplicate = CommentSnapshot(12, _marker("build", payload="evidence=different").body)
    result = evaluate(_snapshot(comments=(_marker("build"), duplicate, _marker("audit"))))
    assert result.decision == "GUARD FAILURE"
    assert "non-equivalent" in result.reasons[0]
    assert result.mutation_plan == ()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (_body(profile="FAST", policy="HUMAN"), "AWAITING HUMAN APPROVAL"),
        (_body(profile="STANDARD", policy="HUMAN"), "AWAITING HUMAN APPROVAL"),
        (_body(profile="CRITICAL", policy="HUMAN"), "AWAITING HUMAN APPROVAL"),
    ],
)
def test_human_policy_never_authorizes_ready_or_merge(body: str, expected: str) -> None:
    result = evaluate(_snapshot(body=body))
    assert result.decision == expected


def test_finalization_requires_literal_gate_and_live_evidence() -> None:
    missing_smoke = evaluate(_snapshot(smoke=None))
    assert missing_smoke.decision == "GUARD FAILURE"
    assert "smoke" in missing_smoke.reasons[0]
    missing_gate = _snapshot()
    missing_gate = GuardSnapshot(
        missing_gate.issue,
        missing_gate.pull_request,
        missing_gate.comments,
        (),
        missing_gate.smoke,
        missing_gate.requested_changes,
        missing_gate.open_threads,
    )
    result = evaluate(missing_gate)
    assert result.decision == "GUARD FAILURE"
    assert "Python 3.12 quality" in result.reasons[0]


def test_build_and_audit_phases_share_target_and_marker_guard() -> None:
    build_result = evaluate(_snapshot(comments=()), phase="build")
    assert build_result.decision == "BUILD GUARD PASS"
    audit_result = evaluate(_snapshot(comments=(_marker("build"),)), phase="audit")
    assert audit_result.decision == "AUDIT GUARD PASS"


def test_json_cli_is_read_only_and_emits_compact_decision(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "issue": {
                    "number": 58,
                    "state": "OPEN",
                    "labels": ["workflow:active"],
                    "body": _body(),
                },
                "pull_request": {
                    "number": 59,
                    "state": "OPEN",
                    "isDraft": True,
                    "headRefName": _pr().head_branch,
                    "headRefOid": FULL_SHA,
                    "baseRefName": "main",
                    "baseRefOid": BASE_SHA,
                },
                "comments": [{"id": c.comment_id, "body": c.body} for c in _snapshot().comments],
                "checks": [
                    {"name": "Python 3.12 quality", "status": "completed", "conclusion": "success"}
                ],
                "smoke": {"status": "PASS", "evidence": "temporary smoke"},
                "requested_changes": False,
                "open_threads": 0,
            }
        ),
        encoding="utf-8",
    )
    before = snapshot_path.read_bytes()
    completed = subprocess.run(
        [sys.executable, "scripts/check_workflow_guards.py", "--json", str(snapshot_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["decision"] == "AWAITING HUMAN APPROVAL"
    assert snapshot_path.read_bytes() == before
