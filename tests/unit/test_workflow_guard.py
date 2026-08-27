from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
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
HISTORICAL_SHA = "c" * 40


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


def _marker(
    role: str,
    status: str = "PASS",
    *,
    payload: str = "evidence=pass",
    sha: str = FULL_SHA,
    comment_id: int | None = None,
) -> CommentSnapshot:
    reviewer = "reviewer=gemini-fresh\n" if role == "audit" else ""
    return CommentSnapshot(
        comment_id if comment_id is not None else (10 if role == "build" else 11),
        f"<!-- development-workflow:{role}-v1\n"
        f"block=DEV-7\nsha={sha}\nstatus={status}\n{reviewer}-->\n{payload}\n",
    )


def _head_advanced_archive(
    *,
    sha: str = HISTORICAL_SHA,
    superseded_by_sha: str = FULL_SHA,
    role: str = "audit",
    payload: str = "historical-secret-payload",
) -> CommentSnapshot:
    return CommentSnapshot(
        20,
        "<!-- development-workflow:superseded-v1\n"
        f"block=DEV-7\nsha={sha}\nstatus=PASS\nrole={role}\n"
        "reviewer=gemini-fresh\nreason=head-advanced\n"
        f"superseded_by_sha={superseded_by_sha}\n-->\n{payload}\n",
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
        source="live",
        mergeable=True,
        mergeability_state="clean",
        viewer_permission="WRITE",
        base_protected=True,
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


def test_build_accepts_one_stale_audit_without_transferring_pass() -> None:
    result = evaluate(
        _snapshot(
            comments=(
                _marker("build", status="PENDING"),
                _marker("audit", sha=HISTORICAL_SHA),
            )
        ),
        phase="build",
    )

    assert result.decision == "BUILD GUARD PASS"
    assert result.build.marker is not None
    assert result.build.marker.status == "PENDING"
    assert result.audit.marker is None
    assert [marker.sha for marker in result.audit.historical_stale] == [HISTORICAL_SHA]
    assert result.mutation_plan == ("audit-owner archive audit comment 11 as head-advanced",)


def test_build_recovers_one_stale_pending_build_marker_without_payload_transfer() -> None:
    result = evaluate(
        _snapshot(
            comments=(
                _marker(
                    "build",
                    status="PENDING",
                    sha=HISTORICAL_SHA,
                    payload="historical-secret-payload",
                ),
            )
        ),
        phase="build",
    )

    assert result.decision == "BUILD GUARD PASS"
    assert result.build.marker is None
    assert [marker.sha for marker in result.build.historical_stale] == [HISTORICAL_SHA]
    assert result.mutation_plan == (
        "build-owner retarget build comment 10 to current head as PENDING",
    )
    assert "historical-secret-payload" not in json.dumps(result.as_json(), sort_keys=True)


@pytest.mark.parametrize("phase", ("audit", "finalize"))
def test_only_build_can_recover_a_stale_pending_build_marker(phase: str) -> None:
    result = evaluate(
        _snapshot(comments=(_marker("build", status="PENDING", sha=HISTORICAL_SHA),)),
        phase=phase,
    )

    assert result.decision == "GUARD FAILURE"
    assert result.reasons == ("build marker SHA differs from live PR head",)


@pytest.mark.parametrize("status", ("PASS", "FAIL"))
def test_build_rejects_stale_terminal_build_markers(status: str) -> None:
    result = evaluate(
        _snapshot(comments=(_marker("build", status=status, sha=HISTORICAL_SHA),)),
        phase="build",
    )

    assert result.decision == "GUARD FAILURE"
    assert result.reasons == ("build marker SHA differs from live PR head",)


def test_build_rejects_multiple_or_current_and_stale_build_markers() -> None:
    stale = _marker("build", status="PENDING", sha=HISTORICAL_SHA, comment_id=12)
    multiple = evaluate(
        _snapshot(
            comments=(
                stale,
                _marker("build", status="PENDING", sha=HISTORICAL_SHA, comment_id=13),
            )
        ),
        phase="build",
    )
    current_and_stale = evaluate(
        _snapshot(comments=(_marker("build"), stale)),
        phase="build",
    )

    assert multiple.decision == "GUARD FAILURE"
    assert current_and_stale.decision == "GUARD FAILURE"
    assert multiple.reasons == ("build marker SHA differs from live PR head",)
    assert current_and_stale.reasons == ("build marker SHA differs from live PR head",)


def test_audit_preflight_accepts_stale_history_after_build_pass() -> None:
    result = evaluate(
        _snapshot(
            comments=(
                _marker("build"),
                _marker("audit", sha=HISTORICAL_SHA),
            )
        ),
        phase="audit",
    )

    assert result.decision == "AUDIT GUARD PASS"
    assert result.build.marker is not None
    assert result.build.marker.status == "PASS"
    assert result.audit.marker is None
    assert result.audit.historical_stale[0].sha == HISTORICAL_SHA


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_current_audit_result_is_the_only_current_result(status: str) -> None:
    result = evaluate(
        _snapshot(comments=(_marker("build"), _marker("audit", status=status))),
        phase="audit",
    )

    assert result.decision == "AUDIT GUARD PASS"
    assert result.audit.marker is not None
    assert result.audit.marker.status == status
    assert result.audit.historical_stale == ()


def test_finalize_rejects_stale_audit_and_never_transfers_pass() -> None:
    result = evaluate(
        _snapshot(comments=(_marker("build"), _marker("audit", sha=HISTORICAL_SHA))),
        phase="finalize",
    )

    assert result.decision == "GUARD FAILURE"
    assert result.reasons == ("audit marker SHA differs from live PR head",)


def test_current_and_stale_audit_markers_are_contradictory() -> None:
    comments = (
        _marker("build"),
        _marker("audit"),
        _marker("audit", sha=HISTORICAL_SHA, comment_id=12),
    )

    for phase in ("build", "audit"):
        result = evaluate(_snapshot(comments=comments), phase=phase)
        assert result.decision == "GUARD FAILURE"
        assert result.reasons == ("current and historical stale audit markers coexist",)


def test_non_equivalent_stale_audit_markers_fail_closed() -> None:
    comments = (
        _marker("build"),
        _marker("audit", sha=HISTORICAL_SHA, comment_id=11),
        _marker("audit", sha=HISTORICAL_SHA, status="FAIL", comment_id=12),
    )
    result = evaluate(_snapshot(comments=comments), phase="audit")

    assert result.decision == "GUARD FAILURE"
    assert result.reasons == ("non-equivalent stale audit markers",)


@pytest.mark.parametrize(
    "comment",
    (
        _head_advanced_archive(role="build"),
        _head_advanced_archive(superseded_by_sha=HISTORICAL_SHA),
        CommentSnapshot(
            21,
            "<!-- development-workflow:superseded-v1\n"
            f"block=DEV-7\nsha={HISTORICAL_SHA}\nstatus=PASS\nrole=audit\n"
            "reviewer=gemini-fresh\nreason=head-advanced\n-->\nhistory\n",
        ),
    ),
)
def test_invalid_head_advanced_archives_fail_closed(comment: CommentSnapshot) -> None:
    with pytest.raises(ValueError):
        parse_marker(comment)


def test_equivalent_audit_duplicates_keep_existing_reconciliation() -> None:
    duplicate = CommentSnapshot(12, _marker("audit").body)
    result = evaluate(
        _snapshot(comments=(_marker("build"), _marker("audit"), duplicate)),
        phase="audit",
    )

    assert result.decision == "AUDIT GUARD PASS"
    assert result.mutation_plan == ("supersede audit comment 12",)


def test_archive_first_without_current_audit_never_authorizes_finalize() -> None:
    comments = (_marker("build"), _head_advanced_archive())
    audit_preflight = evaluate(_snapshot(comments=comments), phase="audit")
    finalize = evaluate(_snapshot(comments=comments), phase="finalize")

    assert audit_preflight.decision == "AUDIT GUARD PASS"
    assert audit_preflight.audit.marker is None
    assert finalize.decision == "GUARD FAILURE"
    assert finalize.reasons == ("AUDIT PASS marker is absent or not PASS",)


def test_stale_json_and_action_plan_never_expose_historical_payload() -> None:
    result = evaluate(
        _snapshot(
            comments=(
                _marker("build"),
                _marker(
                    "audit",
                    sha=HISTORICAL_SHA,
                    payload="historical-secret-payload",
                ),
            )
        ),
        phase="build",
    )
    rendered = json.dumps(result.as_json(), sort_keys=True)

    assert "historical-secret-payload" not in rendered
    assert "audit-owner archive audit comment 11 as head-advanced" in rendered
    assert "historical_stale" in rendered


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


def test_finalization_rejects_manual_snapshot_and_non_terminal_mergeability() -> None:
    manual = replace(_snapshot(), source="json")
    assert evaluate(manual).decision == "GUARD FAILURE"
    assert "live acquisition" in evaluate(manual).reasons[0]

    mergeability_unknown = replace(_snapshot(), mergeable=None)
    assert evaluate(mergeability_unknown).decision == "GUARD FAILURE"
    assert "mergeability" in evaluate(mergeability_unknown).reasons[0]


def test_latest_review_uses_only_the_current_reviewer_state() -> None:
    changes_requested = {
        "id": "review-1",
        "state": "CHANGES_REQUESTED",
        "submittedAt": "2026-08-14T12:00:00Z",
        "author": {"login": "reviewer"},
    }
    approved = {
        "id": "review-2",
        "state": "APPROVED",
        "submittedAt": "2026-08-14T12:01:00Z",
        "author": {"login": "reviewer"},
    }
    dismissed = {
        "id": "review-3",
        "state": "DISMISSED",
        "submittedAt": "2026-08-14T12:02:00Z",
        "author": {"login": "reviewer"},
    }

    assert _MODULE._latest_reviews_requested_changes((changes_requested,))
    assert not _MODULE._latest_reviews_requested_changes((changes_requested, approved))
    assert not _MODULE._latest_reviews_requested_changes((changes_requested, dismissed))
    with pytest.raises(ValueError, match="unknown"):
        _MODULE._latest_reviews_requested_changes(({**changes_requested, "state": "UNRECOGNIZED"},))


def test_review_thread_count_requires_explicit_resolution() -> None:
    assert _MODULE._open_review_threads(({"isResolved": True}, {"isResolved": False})) == 1
    with pytest.raises(ValueError, match="resolution"):
        _MODULE._open_review_threads(({},))


def test_graphql_connection_reads_all_pages_and_rejects_truncated_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = iter(
        (
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [{"isResolved": True}],
                                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                            }
                        }
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [{"isResolved": False}],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            },
        )
    )
    monkeypatch.setattr(_MODULE, "_gh_graphql", lambda *_: next(pages))

    threads = _MODULE._graphql_connection("owner/repository", 12, "reviewThreads", "isResolved")
    assert threads == ({"isResolved": True}, {"isResolved": False})

    monkeypatch.setattr(
        _MODULE,
        "_gh_graphql",
        lambda *_: {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True, "endCursor": None},
                        }
                    }
                }
            }
        },
    )
    with pytest.raises(ValueError, match="end_cursor"):
        _MODULE._graphql_connection("owner/repository", 12, "reviewThreads", "isResolved")


def test_build_and_audit_phases_share_target_and_marker_guard() -> None:
    build_result = evaluate(_snapshot(comments=()), phase="build")
    assert build_result.decision == "BUILD GUARD PASS"
    audit_result = evaluate(_snapshot(comments=(_marker("build"),)), phase="audit")
    assert audit_result.decision == "AUDIT GUARD PASS"


def test_build_bootstrap_allows_zero_prs_but_other_phases_fail_closed() -> None:
    bootstrap = replace(
        _snapshot(comments=()),
        pull_request=None,
        active_issue_count=1,
        open_pr_count=0,
    )

    result = evaluate(bootstrap, phase="build")

    assert result.decision == "BUILD BOOTSTRAP"
    assert result.metadata is not None
    assert result.metadata.expected_branch == "codex/dev-7-finalize-policy-guard"
    for phase in ("audit", "finalize"):
        failed = evaluate(bootstrap, phase=phase)
        assert failed.decision == "GUARD FAILURE"
        assert failed.reasons == ("target PR is absent outside BUILD bootstrap",)


def test_json_cli_is_read_only_and_cannot_authorize_finalize(tmp_path: Path) -> None:
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
        [
            sys.executable,
            "scripts/check_workflow_guards.py",
            "--json",
            str(snapshot_path),
            "--phase",
            "build",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["decision"] == "BUILD GUARD PASS"
    assert snapshot_path.read_bytes() == before

    finalized = subprocess.run(
        [
            sys.executable,
            "scripts/check_workflow_guards.py",
            "--json",
            str(snapshot_path),
            "--phase",
            "finalize",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert finalized.returncode == 1
    assert json.loads(finalized.stdout)["reasons"] == ["finalize phase is live-only"]


def test_json_cli_reports_non_terminal_build_bootstrap(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "bootstrap.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "issue": {
                    "number": 58,
                    "state": "OPEN",
                    "labels": ["workflow:active"],
                    "body": _body(),
                },
                "active_issue_count": 1,
                "open_pr_count": 0,
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/check_workflow_guards.py",
            "--json",
            str(snapshot_path),
            "--phase",
            "build",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout)["decision"] == "BUILD BOOTSTRAP"
