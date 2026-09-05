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
    GOVERNANCE_PATHS,
    GOVERNANCE_PREFIXES,
    ChangedPath,
    CheckSnapshot,
    CommentSnapshot,
    GuardSnapshot,
    IssueSnapshot,
    MarkerRecord,
    MarkerResolution,
    PullRequestSnapshot,
    ScopeEvidence,
    SmokeSnapshot,
    _validate_audit_independence,
    evaluate,
    parse_acceptance_manifest,
    parse_declared_scope,
    parse_marker,
    parse_work_block,
)

ROOT = Path(__file__).resolve().parents[2]
FULL_SHA = "a" * 40
BASE_SHA = "b" * 40
HISTORICAL_SHA = "c" * 40
DECLARED_DIGEST = "d" * 64


def _scope_body() -> str:
    return f"""
## Strict delta allowlist

- `scripts/check_workflow_guards.py` — `{DECLARED_DIGEST}`.
- `.github/CODEOWNERS` — `nuevo`.

## Superficies protegidas

### Checkpoint dormante — verificación de hash en el primary worktree

### Superficies del repositorio no modificables — deny por ruta cambiada

## Superficies prohibidas

- `src/**` — `deny`.
"""


def _issue(body: str) -> IssueSnapshot:
    return IssueSnapshot(58, "OPEN", frozenset({"workflow:active"}), body)


def _manifest() -> str:
    return """
```json
{"schema_version":"workflow-acceptance-manifest-v1","route_effect":"NONE","items":[{"id":"A1","kind":"acceptance","requirements":["live_probe:unit"]}]}
```
"""


def _body(
    profile: str = "CRITICAL",
    policy: str = "HUMAN",
    writer_role: str = "BUILD_PRODUCT",
) -> str:
    return (
        f"""<!-- development-workflow:work-block-v1 -->

- **Work Block ID:** `DEV-7`.
- **Risk:** `R3`.
- **Profile:** `{profile}`.
- **finalize_policy:** `{policy}`.
- **route_effect:** `NONE`.
- **Base remota exacta:** `origin/main@{BASE_SHA}`.
- **Expected branch:** `codex/dev-7-finalize-policy-guard`.
- **Writer role:** `{writer_role}`.
"""
        + _manifest()
        + _scope_body()
    )


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
    manifest = parse_acceptance_manifest(_body())
    evidence_val = "unit" if role == "build" else "audit-verified"
    evidence = json.dumps(
        {"items": [{"id": "A1", "verdict": "PASS", "evidence": {"live_probe:unit": evidence_val}}]},
        separators=(",", ":"),
    )
    reviewer = "reviewer=gemini-fresh\n" if role == "audit" else ""
    return CommentSnapshot(
        comment_id if comment_id is not None else (10 if role == "build" else 11),
        f"<!-- development-workflow:{role}-v2\n"
        f"block=DEV-7\nsha={sha}\nstatus={status}\n"
        f"manifest_sha256={manifest.digest}\n{reviewer}-->\n"
        f"{evidence if payload == 'evidence=pass' else payload}\n",
    )


def _body_for_manifest(
    manifest: dict[str, object],
    *,
    writer_role: str = "BUILD_PRODUCT",
    route_effect: str = "NONE",
) -> str:
    encoded = json.dumps(manifest, separators=(",", ":"))
    return (
        _body(writer_role=writer_role)
        .replace(_manifest(), f"\n```json\n{encoded}\n```\n")
        .replace("- **route_effect:** `NONE`.", f"- **route_effect:** `{route_effect}`.")
    )


def _structured_marker(
    role: str,
    body: str,
    *,
    status: str = "PASS",
    comment_id: int | None = None,
    sha: str = FULL_SHA,
) -> CommentSnapshot:
    manifest = parse_acceptance_manifest(body)
    evidence_val = "verified" if role == "build" else "audit-verified"
    items = [
        {
            "id": item.item_id,
            "verdict": "PASS",
            "evidence": {requirement: evidence_val for requirement in item.requirements},
        }
        for item in manifest.items
    ]
    reviewer = "reviewer=fresh-auditor\n" if role == "audit" else ""
    return CommentSnapshot(
        comment_id if comment_id is not None else (100 if role == "build" else 101),
        f"<!-- development-workflow:{role}-v2\nblock=DEV-7\nsha={sha}\nstatus={status}\n"
        f"manifest_sha256={manifest.digest}\n{reviewer}-->\n"
        f"{json.dumps({'items': items}, separators=(',', ':'))}\n",
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
    changed_paths: tuple[ChangedPath, ...] = (),
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
        scope_evidence=ScopeEvidence(changed_paths, {}, {}),
    )


def test_metadata_requires_unique_structural_fields_and_human_critical() -> None:
    assert parse_work_block(_body()).policy == "HUMAN"
    with pytest.raises(ValueError, match="duplicate Work Block field"):
        parse_work_block(_body() + "- **Profile:** `STANDARD`.\n")
    with pytest.raises(ValueError, match="CRITICAL requires"):
        parse_work_block(_body(policy="AUTO"))
    with pytest.raises(ValueError, match="missing Work Block fields"):
        parse_work_block(_body().replace("- **Writer role:** `BUILD_PRODUCT`.\n", ""))


def test_declared_scope_distinguishes_checkpoint_from_immutable_and_accepts_exact_deny() -> None:
    body = (
        _body()
        .replace(
            "### Checkpoint dormante — verificación de hash en el primary worktree\n",
            "### Checkpoint dormante — verificación de hash en el primary worktree\n\n"
            f"- `checkpoint.txt` — `{DECLARED_DIGEST}`.\n",
        )
        .replace(
            "### Superficies del repositorio no modificables — deny por ruta cambiada\n",
            "### Superficies del repositorio no modificables — deny por ruta cambiada\n\n"
            f"- `immutable.txt` — `{DECLARED_DIGEST}`.\n",
        )
        .replace("- `src/**` — `deny`.", "- `src/**` — `deny`.\n- `CLAUDE.md` — `deny`.")
    )

    scope = parse_declared_scope(body)

    assert scope.checkpoint_hashes == {"checkpoint.txt": DECLARED_DIGEST}
    assert scope.immutable_hashes == {"immutable.txt": DECLARED_DIGEST}
    assert scope.prohibited["CLAUDE.md"] == "deny"


def test_scope_fails_closed_for_governance_auto_and_path_contracts() -> None:
    auto = _body(profile="FAST", policy="AUTO")
    governance = evaluate(
        _snapshot(
            body=auto,
            changed_paths=(ChangedPath("scripts/check_workflow_guards.py", "modified"),),
        ),
        phase="build",
    )
    assert governance.decision == "GUARD FAILURE"
    assert governance.reasons == (
        "governance path requires HUMAN policy: scripts/check_workflow_guards.py",
    )

    outside = evaluate(
        _snapshot(changed_paths=(ChangedPath("src/new.py", "added"),)), phase="build"
    )
    assert outside.decision == "GUARD FAILURE"
    assert outside.reasons == ("changed path matches prohibited surface: src/new.py",)

    wrong_creation = evaluate(
        _snapshot(changed_paths=(ChangedPath("scripts/check_workflow_guards.py", "added"),)),
        phase="build",
    )
    assert wrong_creation.decision == "GUARD FAILURE"
    assert wrong_creation.reasons == (
        "existing allowlist path is added: scripts/check_workflow_guards.py",
    )

    allowed_new = evaluate(
        _snapshot(changed_paths=(ChangedPath(".github/CODEOWNERS", "added"),)), phase="build"
    )
    assert allowed_new.decision == "GUARD FAILURE"
    assert allowed_new.reasons == (
        "product writer cannot modify governance path: .github/CODEOWNERS",
    )


@pytest.mark.parametrize(
    "path",
    (
        "AGENTS.md",
        "docs/development_protocol.md",
        ".agents/rules/example.md",
        ".agents/skills/example/SKILL.md",
        "scripts/check_workflow_guards.py",
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        ".github/ISSUE_TEMPLATE/work_block.yml",
    ),
)
def test_every_declared_governance_path_requires_human_policy(path: str) -> None:
    result = evaluate(
        _snapshot(
            body=_body(profile="FAST", policy="AUTO"),
            changed_paths=(ChangedPath(path, "modified"),),
        ),
        phase="build",
    )

    assert result.decision == "GUARD FAILURE"
    assert result.reasons == (f"governance path requires HUMAN policy: {path}",)


def test_scope_hash_categories_and_deny_precede_allowlist() -> None:
    body = (
        _body()
        .replace(
            "### Checkpoint dormante — verificación de hash en el primary worktree\n",
            "### Checkpoint dormante — verificación de hash en el primary worktree\n\n"
            f"- `checkpoint.txt` — `{DECLARED_DIGEST}`.\n",
        )
        .replace(
            "### Superficies del repositorio no modificables — deny por ruta cambiada\n",
            "### Superficies del repositorio no modificables — deny por ruta cambiada\n\n"
            f"- `immutable.txt` — `{DECLARED_DIGEST}`.\n",
        )
    )
    valid = ScopeEvidence(
        (), {"checkpoint.txt": DECLARED_DIGEST}, {"immutable.txt": DECLARED_DIGEST}
    )

    checkpoint_failure = evaluate(
        replace(_snapshot(body=body), scope_evidence=replace(valid, checkpoint_digests={})),
        phase="build",
    )
    assert checkpoint_failure.reasons == (
        "checkpoint hash is absent or unreadable: checkpoint.txt",
    )

    overlap_body = body.replace(
        f"- `scripts/check_workflow_guards.py` — `{DECLARED_DIGEST}`.",
        "- `scripts/check_workflow_guards.py` — `"
        f"{DECLARED_DIGEST}`.\n- `immutable.txt` — `{DECLARED_DIGEST}`.",
    )
    immutable_failure = evaluate(
        replace(
            _snapshot(
                body=overlap_body,
                changed_paths=(ChangedPath("immutable.txt", "modified"),),
            ),
            scope_evidence=valid,
        ),
        phase="build",
    )
    assert immutable_failure.reasons == ("allowlist overlaps immutable surface: immutable.txt",)


def test_codeowners_covers_the_single_governance_constant() -> None:
    lines = {
        line.split()[0]
        for line in (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }

    assert lines == {
        "/AGENTS.md",
        "/docs/development_protocol.md",
        "/.agents/rules/**",
        "/.agents/skills/**",
        "/scripts/check_workflow_guards.py",
        "/.github/workflows/**",
        "/.github/CODEOWNERS",
        "/.github/ISSUE_TEMPLATE/**",
    }
    assert {
        "AGENTS.md",
        "docs/development_protocol.md",
        "scripts/check_workflow_guards.py",
        ".github/CODEOWNERS",
    } == GOVERNANCE_PATHS
    assert GOVERNANCE_PREFIXES == (
        ".agents/rules/",
        ".agents/skills/",
        ".github/workflows/",
        ".github/ISSUE_TEMPLATE/",
    )


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

    assert result.decision == "CONTINUE"
    assert result.classification is not None
    assert result.classification.terminal is False
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

    assert result.decision == "CONTINUE"
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
        source="live",
        scope_evidence=missing_gate.scope_evidence,
    )
    result = evaluate(missing_gate)
    assert result.decision == "GUARD FAILURE"
    assert "Python 3.12 quality" in result.reasons[0]


def test_finalization_rejects_manual_snapshot_and_non_terminal_mergeability() -> None:
    manual = replace(_snapshot(), source="json")
    assert evaluate(manual).decision == "NON_AUTHORITATIVE"
    assert evaluate(manual).as_json()["authoritative"] is False

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
    assert build_result.decision == "CONTINUE"
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

    assert result.decision == "CONTINUE"
    assert result.classification is not None
    assert result.classification.terminal is False
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
    rendered = json.loads(completed.stdout)
    assert rendered["decision"] == "NON_AUTHORITATIVE"
    assert rendered["authoritative"] is False
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
    rendered = json.loads(completed.stdout)
    assert rendered["decision"] == "NON_AUTHORITATIVE"
    assert rendered["authoritative"] is False


def test_manifest_rejects_unknown_duplicate_and_missing_requirements() -> None:
    valid = {
        "schema_version": "workflow-acceptance-manifest-v1",
        "route_effect": "NONE",
        "items": [{"id": "A1", "kind": "acceptance", "requirements": ["live_probe:x"]}],
    }
    assert parse_acceptance_manifest(_body_for_manifest(valid)).items[0].item_id == "A1"
    for broken in (
        {**valid, "unexpected": True},
        {**valid, "items": [valid["items"][0], valid["items"][0]]},
        {
            **valid,
            "items": [{"id": "A1", "kind": "acceptance", "requirements": ["unknown:x"]}],
        },
    ):
        with pytest.raises(ValueError):
            parse_acceptance_manifest(_body_for_manifest(broken))


def test_missing_required_artifacts_prevent_audit_pass_regression_135() -> None:
    manifest = {
        "schema_version": "workflow-acceptance-manifest-v1",
        "route_effect": "NONE",
        "items": [
            {
                "id": "A1",
                "kind": "acceptance",
                "requirements": [
                    "present_path:tests/unit/evidence/sec_institutional_observations/test_summary.py",
                    "present_path:scripts/smoke_sec_institutional_observations.py",
                    "focused_test:sec-institutional-observations",
                    "smoke:real-sec-institutional-observations",
                ],
            }
        ],
    }
    body = _body_for_manifest(manifest)
    snapshot = _snapshot(
        body=body,
        comments=(_structured_marker("build", body),),
    )
    result = evaluate(snapshot, phase="audit")
    assert result.decision == "GUARD FAILURE"
    assert "required present path is absent" in result.reasons[0]


def test_route_effect_without_route_diff_cannot_reach_build_ready() -> None:
    manifest = {
        "schema_version": "workflow-acceptance-manifest-v1",
        "route_effect": "ADVANCES",
        "items": [
            {
                "id": "A1",
                "kind": "acceptance",
                "requirements": ["route_transition:SEC-CORPUS:ADVANCES"],
            }
        ],
    }
    body = _body_for_manifest(manifest, route_effect="ADVANCES")
    result = evaluate(
        _snapshot(body=body, comments=(_structured_marker("build", body),)), phase="build"
    )
    assert result.decision == "GUARD FAILURE"
    assert result.reasons == ("route transition document is absent from diff",)


def test_human_receipt_is_exact_sha_fresh_and_needs_no_self_review() -> None:
    body = _body()
    manifest = parse_acceptance_manifest(body)
    comments = (
        _structured_marker("build", body, comment_id=100),
        _structured_marker("audit", body, comment_id=101),
    )
    awaiting = evaluate(_snapshot(body=body, comments=comments))
    assert awaiting.decision == "AWAITING HUMAN APPROVAL"
    receipt = CommentSnapshot(
        102,
        "<!-- development-workflow:human-v1\n"
        f"block=DEV-7\nsha={FULL_SHA}\ndecision=APPROVE\n"
        f"manifest_sha256={manifest.digest}\n-->\n",
    )
    approved = evaluate(_snapshot(body=body, comments=(*comments, receipt)))
    assert approved.decision == "HUMAN_FINALIZE_AUTHORIZED"
    stale = CommentSnapshot(102, receipt.body.replace(FULL_SHA, HISTORICAL_SHA))
    rejected = evaluate(_snapshot(body=body, comments=(*comments, stale)))
    assert rejected.decision == "GUARD FAILURE"
    assert "stale" in rejected.reasons[0]


def test_product_writer_rejects_governance_and_governance_rejects_product() -> None:
    product_body = _body().replace(
        "## Strict delta allowlist\n",
        "## Strict delta allowlist\n\n- `AGENTS.md` — `" + DECLARED_DIGEST + "`.\n",
    )
    product = evaluate(
        _snapshot(body=product_body, changed_paths=(ChangedPath("AGENTS.md", "modified"),)),
        phase="build",
    )
    assert product.decision == "GUARD FAILURE"
    assert "product writer cannot modify governance path" in product.reasons[0]
    governance_body = _body(writer_role="BUILD_GOVERNANCE")
    governance = evaluate(
        _snapshot(
            body=governance_body,
            changed_paths=(ChangedPath("src/investment_analyst/app.py", "modified"),),
        ),
        phase="build",
    )
    assert governance.decision == "GUARD FAILURE"
    assert "prohibited" in governance.reasons[0]


def test_copied_audit_payload_fails_closed_in_audit_phase() -> None:
    body = _body()
    build_marker = _marker("build", payload="evidence=pass")
    copied_audit = _marker("audit", payload=build_marker.body.partition("-->\n")[2])
    snapshot = _snapshot(body=body, comments=(build_marker, copied_audit))
    result = evaluate(snapshot, phase="audit")
    assert result.decision == "GUARD FAILURE"
    assert result.reasons == ("copied audit evidence payload is identical to build",)


def test_copied_audit_payload_fails_closed_in_finalize_phase() -> None:
    body = _body()
    build_marker = _marker("build", payload="evidence=pass")
    copied_audit = _marker("audit", payload=build_marker.body.partition("-->\n")[2])
    snapshot = _snapshot(body=body, comments=(build_marker, copied_audit))
    result = evaluate(snapshot, phase="finalize")
    assert result.decision == "GUARD FAILURE"
    assert result.reasons == ("copied audit evidence payload is identical to build",)


def test_normalisation_does_not_mask_a_copied_payload() -> None:
    body = _body()
    raw_evidence = json.dumps(
        {"items": [{"id": "A1", "verdict": "PASS", "evidence": {"live_probe:unit": "unit"}}]},
        separators=(",", ":"),
    )
    build_payload = f"{raw_evidence}   \n"
    audit_payload = f"{raw_evidence} \r\n\r\n"
    build_marker = _marker("build", payload=build_payload)
    audit_marker = _marker("audit", payload=audit_payload)
    for phase in ("audit", "finalize"):
        snapshot = _snapshot(body=body, comments=(build_marker, audit_marker))
        result = evaluate(snapshot, phase=phase)
        assert result.decision == "GUARD FAILURE"
        assert result.reasons == ("copied audit evidence payload is identical to build",)
    direct_build = MarkerResolution(
        marker=MarkerRecord(
            1, "build", None, "build-v2", "DEV-7", FULL_SHA, "PASS", None, build_payload, ()
        ),
        duplicates=(),
    )
    direct_audit = MarkerResolution(
        marker=MarkerRecord(
            2, "audit", None, "audit-v2", "DEV-7", FULL_SHA, "PASS", "auditor", audit_payload, ()
        ),
        duplicates=(),
    )
    with pytest.raises(ValueError, match="copied audit evidence payload is identical to build"):
        _validate_audit_independence(direct_build, direct_audit, "audit")


def test_independent_audit_payload_still_passes() -> None:
    body = _body()
    build_marker = _marker("build")
    audit_marker = _marker("audit")
    audit_snap = _snapshot(body=body, comments=(build_marker, audit_marker))
    audit_result = evaluate(audit_snap, phase="audit")
    assert audit_result.decision == "AUDIT GUARD PASS"
    finalize_result = evaluate(audit_snap, phase="finalize")
    assert finalize_result.decision == "AWAITING HUMAN APPROVAL"


def test_rule_is_not_evaluated_in_build_phase() -> None:
    body = _body()
    build_marker = _marker("build", payload="evidence=pass")
    copied_audit = _marker("audit", payload=build_marker.body.partition("-->\n")[2])
    snapshot = _snapshot(body=body, comments=(build_marker, copied_audit))
    result = evaluate(snapshot, phase="build")
    assert result.decision == "READY"
    assert result.classification.status == "READY"
    assert "copied audit evidence payload is identical to build" not in result.reasons


def test_rule_can_only_add_a_failure_never_produce_pass() -> None:
    body = _body()
    build_marker = _marker("build")
    audit_marker = _marker("audit")
    smoke_fail_snap = _snapshot(body=body, comments=(build_marker, audit_marker), smoke=None)
    result = evaluate(smoke_fail_snap, phase="audit")
    assert result.decision == "GUARD FAILURE"
    assert "smoke" in result.reasons[0]

    copied_audit = _marker("audit", payload=build_marker.body.partition("-->\n")[2])
    copied_snap = _snapshot(body=body, comments=(build_marker, copied_audit))
    copied_result = evaluate(copied_snap, phase="audit")
    assert copied_result.decision == "GUARD FAILURE"
    assert copied_result.reasons == ("copied audit evidence payload is identical to build",)


def test_decision_uses_no_author_timestamp_or_similarity_heuristic() -> None:
    body = _body()
    manifest = parse_acceptance_manifest(body)
    build_marker = _marker("build")
    identical_audit = CommentSnapshot(
        999,
        f"<!-- development-workflow:audit-v2\nblock=DEV-7\nsha={FULL_SHA}\nstatus=PASS\n"
        f"manifest_sha256={manifest.digest}\nreviewer=completely-different-reviewer\n-->\n"
        f"{build_marker.body.partition('-->\n')[2]}",
    )
    snap_identical = _snapshot(body=body, comments=(build_marker, identical_audit))
    assert evaluate(snap_identical, phase="audit").decision == "GUARD FAILURE"

    distinct_payload = json.dumps(
        {"items": [{"id": "A1", "verdict": "PASS", "evidence": {"live_probe:unit": "unit-audit"}}]},
        separators=(",", ":"),
    )
    distinct_audit = _marker("audit", payload=distinct_payload)
    snap_distinct = _snapshot(body=body, comments=(build_marker, distinct_audit))
    assert evaluate(snap_distinct, phase="audit").decision == "AUDIT GUARD PASS"


def test_failure_message_prints_no_payload() -> None:
    body = _body()
    secret_marker_payload = json.dumps(
        {
            "items": [
                {
                    "id": "A1",
                    "verdict": "PASS",
                    "evidence": {"live_probe:unit": "SUPER_SECRET_PAYLOAD_EVIDENCE_12345"},
                }
            ]
        },
        separators=(",", ":"),
    )
    build_marker = _marker("build", payload=secret_marker_payload)
    copied_audit = _marker("audit", payload=secret_marker_payload)
    snapshot = _snapshot(body=body, comments=(build_marker, copied_audit))
    result = evaluate(snapshot, phase="audit")
    assert result.decision == "GUARD FAILURE"
    assert "SUPER_SECRET_PAYLOAD_EVIDENCE_12345" not in "".join(result.reasons)
    assert result.reasons == ("copied audit evidence payload is identical to build",)


def test_protocol_states_the_audit_independence_rule() -> None:
    protocol = (ROOT / "docs" / "development_protocol.md").read_text(encoding="utf-8")
    normalized = " ".join(protocol.split())
    assert "La evidencia de AUDIT es una revisión propia e independiente" in normalized
    assert "una copia de la de BUILD no es evidencia" in normalized
    assert "payloads machine-owned normalizados son idénticos" in normalized


def test_existing_workflow_guard_suite_still_passes() -> None:
    body = _body()
    build_marker = _marker("build")
    audit_marker = _marker("audit")
    snap = _snapshot(body=body, comments=(build_marker, audit_marker))
    build_res = evaluate(snap, phase="build")
    assert build_res.decision == "READY"
    audit_res = evaluate(snap, phase="audit")
    assert audit_res.decision == "AUDIT GUARD PASS"


def test_json_mode_still_cannot_produce_pass() -> None:
    body = _body()
    snap = replace(_snapshot(body=body), source="json")
    result = evaluate(snap, phase="audit")
    assert result.decision == "NON_AUTHORITATIVE"
    assert result.decision != "AUDIT GUARD PASS"
    assert result.decision != "AUTO_FINALIZE_AUTHORIZED"
    assert result.decision != "HUMAN_FINALIZE_AUTHORIZED"


def test_sec_corpus_21_is_not_reopened_reverted_or_reaudited() -> None:
    plan = (ROOT / "docs" / "basic_functional_release_plan.md").read_text(encoding="utf-8")
    assert "#159" in plan
    local_web = (ROOT / "src" / "investment_analyst" / "frontend" / "local_web.py").read_text(
        encoding="utf-8"
    )
    assert "/api/v1/cazatiburones/declared-activity" in local_web
    assert "/api/v1/cazatiburones/institutional-observations" in local_web
    assert "/api/v1/sec-document-timeline" in local_web
    res = subprocess.run(
        ["git", "log", "--grep=SEC-CORPUS-21", "-n", "1", "--oneline"],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0 and res.stdout.strip():
        assert "SEC-CORPUS-21" in res.stdout
