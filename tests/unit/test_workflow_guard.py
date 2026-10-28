from __future__ import annotations

import base64
import hashlib
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
    AuthoritySnapshot,
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
    StandingAuthorizationEvidence,
    _validate_audit_independence,
    _validate_scope,
    evaluate,
    parse_acceptance_manifest,
    parse_declared_scope,
    parse_goal_review,
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


def _manifest(route_effect: str = "NONE") -> str:
    requirements = ["live_probe:unit"]
    if route_effect != "NONE":
        requirements.append(f"route_transition:DEV-7:{route_effect}")
    manifest = {
        "schema_version": "workflow-acceptance-manifest-v1",
        "route_effect": route_effect,
        "items": [{"id": "A1", "kind": "acceptance", "requirements": requirements}],
    }
    return f"\n```json\n{json.dumps(manifest, separators=(',', ':'))}\n```\n"


def _body(
    profile: str = "CRITICAL",
    policy: str = "HUMAN",
    writer_role: str = "BUILD_PRODUCT",
    risk: str = "R3",
    route_effect: str = "NONE",
    authorization: str | None = None,
    finalization_authorization: str | None = None,
) -> str:
    authorization_line = (
        f"- **Critical AUTO authorization:** `{authorization}`.\n" if authorization else ""
    )
    finalization_line = (
        f"- **Finalization authorization:** `{finalization_authorization}`.\n"
        if finalization_authorization
        else ""
    )
    return (
        f"""<!-- development-workflow:work-block-v1 -->

- **Work Block ID:** `DEV-7`.
- **Risk:** `{risk}`.
- **Profile:** `{profile}`.
- **finalize_policy:** `{policy}`.
- **route_effect:** `{route_effect}`.
- **Base remota exacta:** `origin/main@{BASE_SHA}`.
- **Expected branch:** `codex/dev-7-finalize-policy-guard`.
- **Writer role:** `{writer_role}`.
{authorization_line}{finalization_line}
"""
        + _manifest(route_effect)
        + _scope_body()
    )


def _governance_writer_body(
    paths: tuple[str, ...],
    *,
    profile: str = "CRITICAL",
    policy: str = "HUMAN",
    risk: str = "R3",
    route_effect: str = "NONE",
    writer_role: str = "BUILD_GOVERNANCE",
) -> str:
    body = _body(
        profile=profile,
        policy=policy,
        writer_role=writer_role,
        risk=risk,
        route_effect=route_effect,
    )
    previous_allowlist = (
        f"- `scripts/check_workflow_guards.py` — `{DECLARED_DIGEST}`.\n"
        "- `.github/CODEOWNERS` — `nuevo`.\n"
    )
    entries = "".join(f"- `{path}` — `{DECLARED_DIGEST}`.\n" for path in paths)
    assert previous_allowlist in body
    return body.replace(previous_allowlist, entries, 1)


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
    block: str = "DEV-7",
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
        f"block={block}\nsha={sha}\nstatus={status}\n"
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


def _goal_review_data(
    *,
    statuses: tuple[str, ...] = (
        "NOT_MET",
        "NOT_MET",
        "UNKNOWN",
        "UNKNOWN",
        "UNKNOWN",
        "UNKNOWN",
        "UNKNOWN",
    ),
    code_sha: str = BASE_SHA,
) -> dict[str, object]:
    goals: list[dict[str, object]] = []
    for index, status in enumerate(statuses, start=1):
        goals.append(
            {
                "id": f"DC-Q{index}",
                "status": status,
                "evidence_level": "INTEGRATED" if status == "UNKNOWN" else "OPERATIONAL",
                "baseline": "baseline value",
                "current": "current value",
                "target": "target value",
                "method": "comparable method",
                "references": ["docs/data_chassis.md"],
            }
        )
    return {
        "schema_version": "workflow-goal-review-v1",
        "phase": "DATA-CHASSIS",
        "as_of": "2026-10-05T00:15:44+00:00",
        "code_sha": code_sha,
        "runtime_sha": "e" * 40,
        "goals": goals,
        "counts": {
            "met": sum(status == "MET" for status in statuses),
            "not_met": sum(status == "NOT_MET" for status in statuses),
            "unknown": sum(status == "UNKNOWN" for status in statuses),
        },
    }


def _body_with_goal_review(review: dict[str, object]) -> str:
    return _body() + f"\n```json\n{json.dumps(review, separators=(',', ':'))}\n```\n"


def _structured_marker(
    role: str,
    body: str,
    *,
    status: str = "PASS",
    comment_id: int | None = None,
    sha: str = FULL_SHA,
    block: str = "DEV-7",
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
        f"<!-- development-workflow:{role}-v2\nblock={block}\nsha={sha}\nstatus={status}\n"
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
    authority_snapshot: AuthoritySnapshot | None = None,
    repository: str | None = "Marjuraru/investment-analyst",
    present_paths: frozenset[str] = frozenset(),
    route_document: str | None = None,
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
        scope_evidence=ScopeEvidence(changed_paths, {}, {}, present_paths, route_document),
        authority_snapshot=authority_snapshot,
        repository=repository,
    )


def _critical_auto_policy(
    *,
    block: str = "DEV-7",
    expected_branch: str = "codex/dev-7-finalize-policy-guard",
    route_effect: str = "NONE",
    route_item: str = "DEV",
    authorization_id: str = "human-dev-7",
) -> bytes:
    return json.dumps(
        {
            "schema_version": "critical-auto-policy-v1",
            "authorizations": [
                {
                    "authorization_id": authorization_id,
                    "granted_at": "2026-10-07T05:45:23.081522+00:00",
                    "human_instruction": "Explicit human authorization for this bounded finalize.",
                    "purpose": "finalize_only",
                    "writer_role": "BUILD_PRODUCT",
                    "profile": "CRITICAL",
                    "finalize_policy": "AUTO",
                    "route_item": route_item,
                    "blocks": [
                        {
                            "id": block,
                            "expected_branch": expected_branch,
                            "route_effect": route_effect,
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")


STANDING_ID = "human-standing-auto-finalize-20261028"
STANDING_REPOSITORY = "Marjuraru/investment-analyst"
LIVE_MAIN_SHA = "e" * 40


def _standing_policy(
    *,
    authorization_id: str = STANDING_ID,
    status: str = "ACTIVE",
    writer_roles: tuple[str, ...] = ("BUILD_PRODUCT", "BUILD_GOVERNANCE", "UI_WORKER"),
    profiles: tuple[str, ...] = ("FAST", "STANDARD", "CRITICAL"),
    repository: str = STANDING_REPOSITORY,
    extra_standing: tuple[dict[str, object], ...] = (),
) -> bytes:
    legacy = json.loads(_critical_auto_policy())["authorizations"]
    standing = {
        "authorization_id": authorization_id,
        "granted_at": "2026-10-28T03:42:14Z",
        "human_instruction": "Standing human authorization for future finalization.",
        "repository": repository,
        "purpose": "finalize_only",
        "writer_roles": list(writer_roles),
        "profiles": list(profiles),
        "finalize_policy": "AUTO",
        "status": status,
    }
    return json.dumps(
        {
            "schema_version": "critical-auto-policy-v2",
            "authorizations": legacy,
            "standing_authorizations": [standing, *extra_standing],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _standing_body(
    *,
    profile: str = "CRITICAL",
    writer_role: str = "BUILD_PRODUCT",
    risk: str = "R3",
    route_effect: str = "NONE",
    authorization_id: str = STANDING_ID,
) -> str:
    return _body(
        profile=profile,
        policy="AUTO",
        writer_role=writer_role,
        risk=risk,
        route_effect=route_effect,
        finalization_authorization=authorization_id,
    )


def _patch_standing(
    monkeypatch: pytest.MonkeyPatch,
    base_policy: bytes,
    *,
    live_policy: bytes | None = None,
    main_sha: str = LIVE_MAIN_SHA,
    repository: str = STANDING_REPOSITORY,
    reads: list[str] | None = None,
) -> None:
    def git_bytes(arguments: tuple[str, ...], label: str) -> bytes:
        assert arguments == (
            "show",
            f"{BASE_SHA}:{_MODULE._CRITICAL_AUTO_POLICY_PATH}",
        ), arguments
        assert label == "standing finalization policy base file"
        if reads is not None:
            reads.append("base")
        return base_policy

    monkeypatch.setattr(_MODULE, "_git_bytes", git_bytes)
    if live_policy is None:
        return

    def gh_json(arguments: list[str], label: str) -> object:
        if arguments == ["api", f"repos/{repository}/commits/main"]:
            if reads is not None:
                reads.append("commit")
            return {"sha": main_sha}
        expected = (
            "api",
            f"repos/{repository}/contents/{_MODULE._CRITICAL_AUTO_POLICY_PATH}?ref={main_sha}",
        )
        assert tuple(arguments) == expected, arguments
        assert label == "standing finalization policy live main"
        if reads is not None:
            reads.append("policy")
        return {
            "content": base64.b64encode(live_policy).decode("ascii"),
            "encoding": "base64",
        }

    monkeypatch.setattr(_MODULE, "_gh_json", gh_json)


def _standing_evidence(
    *,
    repository: str = STANDING_REPOSITORY,
    status: str = "ACTIVE",
    live_main_sha: str | None = None,
) -> StandingAuthorizationEvidence:
    return StandingAuthorizationEvidence(
        STANDING_ID,
        repository,
        BASE_SHA,
        DECLARED_DIGEST,
        status,
        "verified_base_standing",
        live_main_sha,
    )
    assert parse_work_block(_body()).policy == "HUMAN"
    with pytest.raises(ValueError, match="duplicate Work Block field"):
        parse_work_block(_body() + "- **Profile:** `STANDARD`.\n")
    with pytest.raises(ValueError, match="critical AUTO authorization is absent"):
        parse_work_block(_body(policy="AUTO"))
    with pytest.raises(ValueError, match="requires BUILD_PRODUCT"):
        parse_work_block(_body(policy="AUTO", writer_role="UI_WORKER", authorization="human-dev-7"))
    with pytest.raises(ValueError, match="requires BUILD_PRODUCT"):
        parse_work_block(
            _body(policy="AUTO", writer_role="BUILD_GOVERNANCE", authorization="human-dev-7")
        )
    with pytest.raises(ValueError, match="only valid for CRITICAL/AUTO"):
        parse_work_block(_body(authorization="human-dev-7"))
    with pytest.raises(ValueError, match="missing Work Block fields"):
        parse_work_block(_body().replace("- **Writer role:** `BUILD_PRODUCT`.\n", ""))


def test_critical_auto_requires_a_verified_base_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body(policy="AUTO", authorization="human-dev-7")
    policy = _critical_auto_policy()

    def git_bytes(arguments: tuple[str, ...], label: str) -> bytes:
        assert label == "critical AUTO policy base file"
        assert arguments == (
            "show",
            f"{BASE_SHA}:{_MODULE._CRITICAL_AUTO_POLICY_PATH}",
        )
        return policy

    monkeypatch.setattr(_MODULE, "_git_bytes", git_bytes)
    result = evaluate(_snapshot(body=body), phase="build")

    assert result.decision == "READY"
    assert result.critical_auto is not None
    evidence = result.as_json()["critical_auto_authorization"]
    assert evidence == {
        "authorization_id": "human-dev-7",
        "base_sha": BASE_SHA,
        "policy_sha256": hashlib.sha256(policy).hexdigest(),
        "source": "verified_base",
    }
    assert "Explicit human authorization" not in json.dumps(result.as_json())


def test_committed_critical_auto_policy_is_bounded_to_declared_blocks() -> None:
    policy = (ROOT / ".agents" / "rules" / "critical_auto_policy.json").read_bytes()

    authorizations = _MODULE._parse_critical_auto_policy(policy)

    assert [authorization.authorization_id for authorization in authorizations] == [
        "human-data-chassis-finalize-20261007",
        "human-data-chassis-continuity-finalize-20261022",
        "human-v2-readiness-finalize-20261026",
    ]
    legacy, continuity, readiness = authorizations
    assert legacy.purpose == "finalize_only"
    assert legacy.writer_role == "BUILD_PRODUCT"
    assert [
        (block.block, block.expected_branch, block.route_effect) for block in legacy.blocks
    ] == [
        ("DATA-CHASSIS-43", "codex/data-chassis-43", "ADVANCES"),
        ("DATA-CHASSIS-44", "codex/data-chassis-44", "ADVANCES"),
        ("DATA-CHASSIS-45", "codex/data-chassis-45", "COMPLETES"),
    ]
    assert continuity.purpose == "finalize_only"
    assert continuity.writer_role == "BUILD_PRODUCT"
    assert continuity.profile == "CRITICAL"
    assert continuity.policy == "AUTO"
    assert continuity.route_item == "DATA-CHASSIS"
    assert [
        (block.block, block.expected_branch, block.route_effect) for block in continuity.blocks
    ] == [
        ("DATA-CHASSIS-READ-1", "codex/data-chassis-read-1", "ADVANCES"),
        ("DATA-CHASSIS-47", "codex/data-chassis-47", "ADVANCES"),
        ("DATA-CHASSIS-48", "codex/data-chassis-48", "COMPLETES"),
        ("DATA-CHASSIS-49", "codex/data-chassis-49", "ADVANCES"),
        ("DATA-CHASSIS-CLOSE", "codex/data-chassis-close", "COMPLETES"),
    ]
    assert continuity.blocks[2].route_effect == "COMPLETES"
    assert readiness.purpose == "finalize_only"
    assert readiness.writer_role == "BUILD_PRODUCT"
    assert readiness.profile == "CRITICAL"
    assert readiness.policy == "AUTO"
    assert readiness.route_item == "FUNDAMENTALS-COVERAGE"
    assert [
        (block.block, block.expected_branch, block.route_effect) for block in readiness.blocks
    ] == [
        (
            "FUNDAMENTALS-COVERAGE-READINESS",
            "codex/fundamentals-coverage-readiness",
            "ADVANCES",
        )
    ]


def _continuity_body(route_effect: str, block: str, branch: str) -> str:
    return (
        _body(policy="AUTO", authorization="human-dev-7", route_effect=route_effect)
        .replace("DEV-7", block)
        .replace("codex/dev-7-finalize-policy-guard", branch)
    )


def test_critical_auto_binds_data_chassis_48_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _critical_auto_policy(
        block="DATA-CHASSIS-48",
        expected_branch="codex/data-chassis-48",
        route_effect="COMPLETES",
        route_item="DATA-CHASSIS",
    )
    monkeypatch.setattr(_MODULE, "_git_bytes", lambda _arguments, _label: policy)
    metadata = parse_work_block(
        _continuity_body("COMPLETES", "DATA-CHASSIS-48", "codex/data-chassis-48")
    )

    evidence = _MODULE._critical_auto_evidence(metadata)

    assert evidence is not None
    assert evidence.authorization_id == "human-dev-7"
    assert evidence.source == "verified_base"


@pytest.mark.parametrize(
    ("block", "branch", "route_effect"),
    [
        ("DATA-CHASSIS-48", "codex/data-chassis-48", "ADVANCES"),
        ("DATA-CHASSIS-48", "codex/data-chassis-49", "COMPLETES"),
        ("DATA-CHASSIS-49", "codex/data-chassis-49", "COMPLETES"),
    ],
)
def test_critical_auto_rejects_wrong_data_chassis_48_binding(
    monkeypatch: pytest.MonkeyPatch,
    block: str,
    branch: str,
    route_effect: str,
) -> None:
    policy = _critical_auto_policy(
        block="DATA-CHASSIS-48",
        expected_branch="codex/data-chassis-48",
        route_effect="COMPLETES",
        route_item="DATA-CHASSIS",
    )
    monkeypatch.setattr(_MODULE, "_git_bytes", lambda _arguments, _label: policy)
    metadata = parse_work_block(_continuity_body(route_effect, block, branch))

    with pytest.raises(_MODULE.GuardFailure, match="does not bind the Work Block"):
        _MODULE._critical_auto_evidence(metadata)


def test_continuity_authorization_is_absent_from_candidate_or_unbound_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body(
        policy="AUTO",
        authorization="human-data-chassis-continuity-finalize-20261022",
    )
    monkeypatch.setattr(_MODULE, "_git_bytes", lambda _arguments, _label: _critical_auto_policy())

    result = evaluate(_snapshot(body=body), phase="build")

    assert result.decision == "GUARD FAILURE"
    assert "absent from the verified base policy" in result.reasons[0]


def _v2_readiness_body(
    *,
    block: str = "FUNDAMENTALS-COVERAGE-READINESS",
    branch: str = "codex/fundamentals-coverage-readiness",
    route_effect: str = "ADVANCES",
) -> str:
    return (
        _body(
            policy="AUTO",
            authorization="human-v2-readiness-finalize-20261026",
            route_effect=route_effect,
        )
        .replace("DEV-7", block)
        .replace("codex/dev-7-finalize-policy-guard", branch)
    )


def _v2_readiness_policy() -> bytes:
    return _critical_auto_policy(
        block="FUNDAMENTALS-COVERAGE-READINESS",
        expected_branch="codex/fundamentals-coverage-readiness",
        route_effect="ADVANCES",
        route_item="FUNDAMENTALS-COVERAGE",
        authorization_id="human-v2-readiness-finalize-20261026",
    )


def test_critical_auto_binds_fundamentals_coverage_readiness_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_MODULE, "_git_bytes", lambda _arguments, _label: _v2_readiness_policy())
    metadata = parse_work_block(_v2_readiness_body())

    evidence = _MODULE._critical_auto_evidence(metadata)

    assert evidence is not None
    assert evidence.authorization_id == "human-v2-readiness-finalize-20261026"
    assert evidence.source == "verified_base"


@pytest.mark.parametrize(
    ("block", "branch", "route_effect"),
    [
        ("FUNDAMENTALS-COVERAGE-READINESS", "codex/fundamentals-coverage-readiness", "COMPLETES"),
        ("FUNDAMENTALS-COVERAGE-READINESS", "codex/fundamentals-coverage-other", "ADVANCES"),
        ("FUNDAMENTALS-COVERAGE-OTHER", "codex/fundamentals-coverage-readiness", "ADVANCES"),
    ],
)
def test_critical_auto_rejects_wrong_readiness_binding(
    monkeypatch: pytest.MonkeyPatch,
    block: str,
    branch: str,
    route_effect: str,
) -> None:
    monkeypatch.setattr(_MODULE, "_git_bytes", lambda _arguments, _label: _v2_readiness_policy())
    metadata = parse_work_block(
        _v2_readiness_body(block=block, branch=branch, route_effect=route_effect)
    )

    with pytest.raises(_MODULE.GuardFailure, match="does not bind the Work Block"):
        _MODULE._critical_auto_evidence(metadata)


def test_readiness_authorization_rejects_governance_auto() -> None:
    with pytest.raises(ValueError, match="requires BUILD_PRODUCT"):
        parse_work_block(_v2_readiness_body().replace("BUILD_PRODUCT", "BUILD_GOVERNANCE"))


def test_readiness_authorization_is_not_authority_from_candidate_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_MODULE, "_git_bytes", lambda _arguments, _label: _critical_auto_policy())
    metadata = parse_work_block(_v2_readiness_body())

    with pytest.raises(_MODULE.GuardFailure, match="absent from the verified base policy"):
        _MODULE._critical_auto_evidence(metadata)


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (_critical_auto_policy(block="DEV-8"), "does not bind the Work Block"),
        (
            b'{"schema_version":"critical-auto-policy-v1","schema_version":"critical-auto-policy-v1","authorizations":[]}',
            "not valid strict JSON",
        ),
        (
            json.dumps({"schema_version": "critical-auto-policy-v1", "authorizations": []}).encode(
                "utf-8"
            ),
            "authorizations are absent",
        ),
    ],
)
def test_critical_auto_policy_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    policy: bytes,
    reason: str,
) -> None:
    monkeypatch.setattr(_MODULE, "_git_bytes", lambda _arguments, _label: policy)

    result = evaluate(
        _snapshot(body=_body(policy="AUTO", authorization="human-dev-7")), phase="build"
    )

    assert result.decision == "GUARD FAILURE"
    assert reason in result.reasons[0]


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
    auto_body = _governance_writer_body(
        ("scripts/check_workflow_guards.py",), profile="FAST", policy="HUMAN"
    )
    auto_metadata = replace(parse_work_block(auto_body), policy="AUTO")
    with pytest.raises(ValueError) as governance_failure:
        _validate_scope(
            auto_metadata,
            auto_body,
            ScopeEvidence((ChangedPath("scripts/check_workflow_guards.py", "modified"),), {}, {}),
        )
    assert str(governance_failure.value) == (
        "governance path requires HUMAN policy: scripts/check_workflow_guards.py"
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
    body = _governance_writer_body((path,), profile="FAST", policy="HUMAN")
    metadata = replace(parse_work_block(body), policy="AUTO")

    with pytest.raises(ValueError) as failure:
        _validate_scope(
            metadata,
            body,
            ScopeEvidence((ChangedPath(path, "modified"),), {}, {}),
        )

    assert str(failure.value) == f"governance path requires HUMAN policy: {path}"


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


def test_goal_review_validates_counts_and_preserves_legacy_and_json_behavior() -> None:
    review = _goal_review_data()
    body = _body_with_goal_review(review)
    parsed = parse_goal_review(body)

    assert parsed is not None
    assert parsed.phase == "DATA-CHASSIS"
    assert parsed.as_of.isoformat() == "2026-10-05T00:15:44+00:00"
    assert (parsed.counts.met, parsed.counts.not_met, parsed.counts.unknown) == (0, 2, 5)
    assert len(parsed.goals) == 7

    with_operational_met = parse_goal_review(
        _body_with_goal_review(_goal_review_data(statuses=("MET", "NOT_MET", "UNKNOWN")))
    )
    assert with_operational_met is not None
    assert with_operational_met.counts.met == 1

    legacy_body = _body()
    assert parse_goal_review(legacy_body) is None
    unrelated_json = legacy_body + '\n```json\n{"example":"workflow-goal-review-v1"}\n```\n'
    assert parse_goal_review(unrelated_json) is None
    legacy_result = evaluate(_snapshot(body=legacy_body), phase="build")
    reviewed_result = evaluate(_snapshot(body=body), phase="build")
    assert reviewed_result.decision == legacy_result.decision
    assert reviewed_result.as_json() == legacy_result.as_json()

    json_snapshot = replace(_snapshot(body=body), source="json")
    assert evaluate(json_snapshot, phase="build").decision == "NON_AUTHORITATIVE"


@pytest.mark.parametrize(
    "mutation",
    (
        "unknown_top_field",
        "unknown_goal_field",
        "duplicate_goal_id",
        "invalid_status",
        "invalid_evidence_level",
        "inflated_count",
        "boolean_count",
        "met_scratch",
        "met_integrated",
        "met_unknown_current",
        "naive_timestamp",
        "non_utc_timestamp",
        "truncated_code_sha",
        "truncated_runtime_sha",
    ),
)
def test_goal_review_rejects_invalid_structure_and_unearned_met(mutation: str) -> None:
    review = _goal_review_data()
    goals_value = review["goals"]
    counts_value = review["counts"]
    assert isinstance(goals_value, list) and isinstance(goals_value[0], dict)
    assert isinstance(counts_value, dict)
    first_goal = goals_value[0]

    if mutation == "unknown_top_field":
        review["unexpected"] = "private review payload"
    elif mutation == "unknown_goal_field":
        first_goal["unexpected"] = "private review payload"
    elif mutation == "duplicate_goal_id":
        assert isinstance(goals_value[1], dict)
        goals_value[1]["id"] = first_goal["id"]
    elif mutation == "invalid_status":
        first_goal["status"] = "IN_PROGRESS"
    elif mutation == "invalid_evidence_level":
        first_goal["evidence_level"] = "VALIDATED"
    elif mutation == "inflated_count":
        counts_value["met"] = 1
    elif mutation == "boolean_count":
        counts_value["unknown"] = True
    elif mutation.startswith("met_"):
        met_review = _goal_review_data(statuses=("MET",))
        met_goals = met_review["goals"]
        assert isinstance(met_goals, list) and isinstance(met_goals[0], dict)
        first_goal = met_goals[0]
        review = met_review
        if mutation == "met_scratch":
            first_goal["evidence_level"] = "SCRATCH"
        elif mutation == "met_integrated":
            first_goal["evidence_level"] = "INTEGRATED"
        else:
            first_goal["current"] = "UNKNOWN"
    elif mutation == "naive_timestamp":
        review["as_of"] = "2026-10-05T00:15:44"
    elif mutation == "non_utc_timestamp":
        review["as_of"] = "2026-10-05T05:15:44+05:00"
    elif mutation == "truncated_code_sha":
        review["code_sha"] = BASE_SHA[:-1]
    else:
        review["runtime_sha"] = "e" * 39

    with pytest.raises(ValueError):
        parse_goal_review(_body_with_goal_review(review))


def test_goal_review_rejects_duplicate_malformed_and_duplicate_key_json() -> None:
    review = _goal_review_data()
    block = f"\n```json\n{json.dumps(review, separators=(',', ':'))}\n```\n"
    with pytest.raises(ValueError, match="at most once"):
        parse_goal_review(_body() + block + block)

    malformed = '{"schema_version":"workflow-goal-review-v1",}'
    with pytest.raises(ValueError, match="strict JSON"):
        parse_goal_review(_body() + f"\n```json\n{malformed}\n```\n")

    duplicated_key = json.dumps(review, separators=(",", ":")).replace(
        '"phase":"DATA-CHASSIS"', '"phase":"DATA-CHASSIS","phase":"OTHER"', 1
    )
    error_body = _body() + f"\n```json\n{duplicated_key}\n```\n"
    with pytest.raises(ValueError) as captured:
        parse_goal_review(error_body)
    assert "strict JSON" in str(captured.value)
    assert "private review payload" not in str(captured.value)


def test_goal_review_code_sha_must_match_declared_base_without_leaking_body() -> None:
    secret = "SENSITIVE_REVIEW_PAYLOAD_39861"
    review = _goal_review_data(code_sha="f" * 40)
    goals = review["goals"]
    assert isinstance(goals, list) and isinstance(goals[0], dict)
    goals[0]["method"] = secret
    result = evaluate(_snapshot(body=_body_with_goal_review(review)), phase="build")

    assert result.decision == "GUARD FAILURE"
    assert result.reasons == ("goal review code_sha differs from Work Block base",)
    assert secret not in " ".join(result.reasons)


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


def test_governance_writer_accepts_declared_governance_surfaces() -> None:
    representative_paths = tuple(f"{prefix}frontier_probe.yml" for prefix in GOVERNANCE_PREFIXES)
    test_paths = (
        "tests/unit/test_workflow_guard.py",
        "tests/unit/test_development_workflow_contract.py",
    )
    paths = tuple(sorted(set(GOVERNANCE_PATHS).union(representative_paths, test_paths)))
    body = _governance_writer_body(paths)
    scope = _validate_scope(
        parse_work_block(body),
        body,
        ScopeEvidence(tuple(ChangedPath(path, "modified") for path in paths), {}, {}),
    )

    assert scope.changed_path_count == len(paths)
    assert scope.consumed_allowlist_paths == paths
    assert scope.governance_paths == tuple(
        sorted(set(GOVERNANCE_PATHS).union(representative_paths))
    )


def test_governance_frontier_rejects_invalid_role_policy_scope_and_paths() -> None:
    def validate(body: str, path: str, status: str = "modified") -> None:
        _validate_scope(
            parse_work_block(body),
            body,
            ScopeEvidence((ChangedPath(path, status),), {}, {}),
        )

    workflow_path = ".github/workflows/ci.yml"
    for writer_role in ("BUILD_PRODUCT", "UI_WORKER"):
        body = _governance_writer_body((workflow_path,), writer_role=writer_role)
        with pytest.raises(ValueError, match="product writer cannot modify governance path"):
            validate(body, workflow_path)

    invalid_metadata = (
        {"risk": "R0"},
        {"risk": "R1"},
        {"risk": "R2"},
        {"profile": "FAST"},
        {"profile": "STANDARD"},
        {"profile": "FAST", "policy": "AUTO"},
        {"profile": "STANDARD", "policy": "AUTO"},
        {"route_effect": "ADVANCES"},
        {"route_effect": "COMPLETES"},
    )
    for metadata in invalid_metadata:
        body = _governance_writer_body(("scripts/check_workflow_guards.py",), **metadata)
        with pytest.raises(ValueError):
            validate(body, "scripts/check_workflow_guards.py")

    unknown_role = _governance_writer_body(
        ("scripts/check_workflow_guards.py",), writer_role="BUILD"
    )
    with pytest.raises(ValueError, match="writer role is unknown"):
        validate(unknown_role, "scripts/check_workflow_guards.py")

    with pytest.raises(ValueError, match="changed path is absent from strict allowlist"):
        validate(
            _governance_writer_body(("scripts/check_workflow_guards.py",)),
            "docs/product_roadmap.md",
        )

    product_path = "docs/unrelated_product.md"
    with pytest.raises(ValueError, match="governance writer cannot modify product path"):
        validate(_governance_writer_body((product_path,)), product_path)

    for near_miss in (
        ".agents/skills-extra/build/SKILL.md",
        ".github/workflows-extra/ci.yml",
        ".github/ISSUE_TEMPLATE-extra/template.yml",
    ):
        body = _governance_writer_body((near_miss,))
        with pytest.raises(ValueError, match="governance writer cannot modify product path"):
            validate(body, near_miss)

    for status, reason in (
        ("added", "existing allowlist path is added"),
        ("renamed", "existing allowlist path is renamed"),
    ):
        body = _governance_writer_body(("scripts/check_workflow_guards.py",))
        with pytest.raises(ValueError, match=reason):
            validate(body, "scripts/check_workflow_guards.py", status)

    for status in ("modified", "renamed"):
        body = _governance_writer_body((".github/CODEOWNERS",)).replace(
            f"- `.github/CODEOWNERS` — `{DECLARED_DIGEST}`.",
            "- `.github/CODEOWNERS` — `nuevo`.",
        )
        with pytest.raises(ValueError, match="new allowlist path is not added"):
            validate(body, ".github/CODEOWNERS", status)

    denied_body = _governance_writer_body((workflow_path,)).replace(
        "- `src/**` — `deny`.",
        "- `src/**` — `deny`.\n- `.github/workflows/**` — `deny`.",
    )
    with pytest.raises(ValueError, match="allowlist overlaps prohibited surface"):
        validate(denied_body, workflow_path)

    immutable_body = _governance_writer_body((workflow_path,)).replace(
        "### Superficies del repositorio no modificables — deny por ruta cambiada\n",
        "### Superficies del repositorio no modificables — deny por ruta cambiada\n\n"
        f"- `{workflow_path}` — `{DECLARED_DIGEST}`.\n",
    )
    with pytest.raises(ValueError, match="allowlist overlaps immutable surface"):
        validate(immutable_body, workflow_path)


def test_governance_frontier_requires_exact_base_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_contents = {path: f"base bytes for {path}".encode() for path in _MODULE._AUTHORITY_PATHS}
    queries: list[tuple[str, str]] = []

    def git_bytes(arguments: tuple[str, ...], label: str) -> bytes:
        assert label == "authority base file"
        assert len(arguments) == 2 and arguments[0] == "show"
        base_sha, path = arguments[1].split(":", maxsplit=1)
        queries.append((base_sha, path))
        assert base_sha == BASE_SHA
        return base_contents[path]

    monkeypatch.setattr(_MODULE, "_git_bytes", git_bytes)
    body = _governance_writer_body(("scripts/check_workflow_guards.py",))
    base_digests = {
        path: hashlib.sha256(content).hexdigest() for path, content in base_contents.items()
    }
    valid_authority = AuthoritySnapshot(BASE_SHA, base_digests)
    valid_snapshot = _snapshot(body=body, authority_snapshot=valid_authority)

    accepted = evaluate(valid_snapshot, phase="build")

    assert accepted.decision == "READY"
    assert queries == [(BASE_SHA, path) for path in _MODULE._AUTHORITY_PATHS]

    candidate_digests = dict(base_digests)
    candidate_digests["scripts/check_workflow_guards.py"] = hashlib.sha256(
        b"candidate bytes"
    ).hexdigest()
    incomplete_digests = dict(base_digests)
    incomplete_digests.pop("scripts/check_workflow_guards.py")
    expanded_digests = {**base_digests, "unlisted/path": DECLARED_DIGEST}
    altered_digests = dict(base_digests)
    altered_digests["scripts/check_workflow_guards.py"] = DECLARED_DIGEST
    invalid_authorities = (
        (None, "requires a base authority snapshot"),
        (AuthoritySnapshot(HISTORICAL_SHA, base_digests), "base SHA differs"),
        (AuthoritySnapshot(BASE_SHA, incomplete_digests), "paths are incomplete or expanded"),
        (AuthoritySnapshot(BASE_SHA, expanded_digests), "paths are incomplete or expanded"),
        (AuthoritySnapshot(BASE_SHA, candidate_digests), "digest differs from base"),
        (AuthoritySnapshot(BASE_SHA, altered_digests), "digest differs from base"),
    )
    for authority, reason in invalid_authorities:
        result = evaluate(
            replace(valid_snapshot, authority_snapshot=authority),
            phase="build",
        )
        assert result.decision == "GUARD FAILURE"
        assert reason in result.reasons[0]


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
    assert "SEC-CORPUS" in plan
    assert "/api/v1/sec-document-timeline" in plan
    assert "/api/v1/cazatiburones/declared-activity" in plan
    assert "/api/v1/cazatiburones/institutional-observations" in plan

    local_web = (ROOT / "src" / "investment_analyst" / "frontend" / "local_web.py").read_text(
        encoding="utf-8"
    )
    assert "/api/v1/cazatiburones/declared-activity" in local_web
    assert "/api/v1/cazatiburones/institutional-observations" in local_web
    assert "/api/v1/sec-document-timeline" in local_web
    assert "def sec_document_timeline(" in local_web
    assert "def cazatiburones_declared_activity(" in local_web
    assert "def cazatiburones_institutional_observations(" in local_web

    test_web = (ROOT / "tests" / "integration" / "frontend" / "test_local_web.py").read_text(
        encoding="utf-8"
    )
    assert "/api/v1/sec-document-timeline" in test_web
    assert "/api/v1/cazatiburones/declared-activity" in test_web
    assert "/api/v1/cazatiburones/institutional-observations" in test_web

    status_proc = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "src/investment_analyst",
            "tests/integration/frontend/test_local_web.py",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status_proc.stdout.strip() == "", f"SEC-CORPUS files modified: {status_proc.stdout}"

    revert_check = subprocess.run(
        ["git", "log", "--grep=[Rr]evert.*SEC-CORPUS", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert revert_check.stdout.strip() == "", f"Unexpected revert found: {revert_check.stdout}"

    sec_audit_marker = _marker("audit", block="SEC-CORPUS-21")
    body = _body()
    snap_sec = _snapshot(body=body, comments=(sec_audit_marker,))
    res_sec = evaluate(snap_sec, phase="audit")
    assert res_sec.decision == "GUARD FAILURE"


def test_metadata_accepts_standing_finalization_authorization() -> None:
    for writer_role in ("BUILD_PRODUCT", "BUILD_GOVERNANCE", "UI_WORKER"):
        for profile in ("FAST", "STANDARD", "CRITICAL"):
            metadata = parse_work_block(_standing_body(profile=profile, writer_role=writer_role))
            assert metadata.finalization_authorization == STANDING_ID
            assert metadata.critical_auto_authorization is None
            assert metadata.policy == "AUTO"

    with pytest.raises(ValueError, match="mutually exclusive"):
        parse_work_block(
            _body(
                policy="AUTO",
                authorization="human-dev-7",
                finalization_authorization=STANDING_ID,
            )
        )
    with pytest.raises(ValueError, match="only valid for AUTO"):
        parse_work_block(_body(finalization_authorization=STANDING_ID))
    with pytest.raises(ValueError, match="finalization authorization is absent for AUTO"):
        parse_work_block(_body(profile="FAST", policy="AUTO"))
    with pytest.raises(ValueError, match="only valid for CRITICAL/AUTO"):
        parse_work_block(_body(profile="FAST", policy="AUTO", authorization="human-dev-7"))
    with pytest.raises(ValueError, match="finalization authorization ID is invalid"):
        parse_work_block(_standing_body(authorization_id="bad id"))


def test_committed_policy_v2_preserves_v1_and_adds_the_standing_grant() -> None:
    parsed = _MODULE._parse_finalization_policy(
        (ROOT / ".agents" / "rules" / "critical_auto_policy.json").read_bytes()
    )

    assert parsed.schema_version == "critical-auto-policy-v2"
    assert [authorization.authorization_id for authorization in parsed.authorizations] == [
        "human-data-chassis-finalize-20261007",
        "human-data-chassis-continuity-finalize-20261022",
        "human-v2-readiness-finalize-20261026",
    ]
    assert [authorization.authorization_id for authorization in parsed.standing_authorizations] == [
        STANDING_ID
    ]
    grant = parsed.standing_authorizations[0]
    assert grant.repository == STANDING_REPOSITORY
    assert grant.purpose == "finalize_only"
    assert grant.writer_roles == ("BUILD_PRODUCT", "BUILD_GOVERNANCE", "UI_WORKER")
    assert grant.profiles == ("FAST", "STANDARD", "CRITICAL")
    assert grant.policy == "AUTO"
    assert grant.status == "ACTIVE"


def test_finalization_policy_v2_fails_closed() -> None:
    def rejects(policy: bytes, reason: str) -> None:
        with pytest.raises(_MODULE.GuardFailure, match=reason):
            _MODULE._parse_finalization_policy(policy)

    def encoded(**changes: object) -> bytes:
        root = json.loads(_standing_policy())
        standing = root["standing_authorizations"][0]
        assert isinstance(standing, dict)
        standing.update(changes)
        return json.dumps(root).encode()

    root = json.loads(_standing_policy())
    root["extra"] = 1
    rejects(json.dumps(root).encode(), "unknown or missing fields")
    root = json.loads(_standing_policy())
    root.pop("standing_authorizations")
    rejects(json.dumps(root).encode(), "unknown or missing fields")
    root = json.loads(_standing_policy())
    root["authorizations"] = []
    rejects(json.dumps(root).encode(), "authorizations are absent")
    rejects(encoded(authorization_id="human-dev-7"), "ID is invalid or duplicate")
    rejects(encoded(writer_roles=["BUILD"]), "writer_roles contain unknown or duplicate")
    rejects(encoded(profiles=[]), "profiles are absent")
    rejects(encoded(repository="invalid"), "repository is invalid")
    rejects(encoded(granted_at="2026-10-28T03:42:14"), "must be UTC")
    rejects(encoded(finalize_policy="HUMAN"), "policy is invalid")
    rejects(encoded(purpose="merge"), "purpose is invalid")
    rejects(encoded(status="PENDING"), "status is invalid")
    rejects(encoded(extra="field"), "unknown or missing fields")
    rejects(
        b'{"schema_version":"critical-auto-policy-v2",'
        b'"schema_version":"critical-auto-policy-v2",'
        b'"authorizations":[],"standing_authorizations":[]}',
        "not valid strict JSON",
    )


def test_standing_auto_uses_only_verified_base_bytes_and_reports_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _standing_policy()
    _patch_standing(monkeypatch, base)

    result = evaluate(_snapshot(body=_standing_body()), phase="build")

    assert result.decision == "READY"
    assert result.critical_auto is None
    assert result.standing_authorization is not None
    assert result.as_json()["finalization_authorization"] == {
        "authorization_id": STANDING_ID,
        "repository": STANDING_REPOSITORY,
        "base_sha": BASE_SHA,
        "policy_sha256": hashlib.sha256(base).hexdigest(),
        "status": "ACTIVE",
        "source": "verified_base_standing",
        "live_main_sha": None,
        "binding": {
            "block": "DEV-7",
            "expected_branch": "codex/dev-7-finalize-policy-guard",
            "writer_role": "BUILD_PRODUCT",
            "head_sha": FULL_SHA,
            "manifest_sha256": parse_acceptance_manifest(_standing_body()).digest,
        },
    }
    assert "Standing human authorization" not in json.dumps(result.as_json())


@pytest.mark.parametrize(
    ("metadata", "repository", "policy", "reason"),
    [
        (
            _standing_body(profile="FAST"),
            STANDING_REPOSITORY,
            _standing_policy(profiles=("CRITICAL",)),
            "does not bind the profile",
        ),
        (
            _standing_body(writer_role="UI_WORKER"),
            STANDING_REPOSITORY,
            _standing_policy(writer_roles=("BUILD_PRODUCT",)),
            "does not bind the writer role",
        ),
        (
            _standing_body(),
            "Other/repo",
            _standing_policy(),
            "repository differs",
        ),
        (
            _standing_body(),
            STANDING_REPOSITORY,
            _standing_policy(status="REVOKED"),
            "not ACTIVE",
        ),
        (
            _standing_body(),
            STANDING_REPOSITORY,
            _standing_policy(authorization_id="other-standing"),
            "absent from the verified base policy",
        ),
        (
            _standing_body(),
            STANDING_REPOSITORY,
            _critical_auto_policy(),
            "absent from the verified base policy",
        ),
    ],
)
def test_standing_auto_rejects_invalid_bindings(
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
    repository: str,
    policy: bytes,
    reason: str,
) -> None:
    monkeypatch.setattr(_MODULE, "_git_bytes", lambda _arguments, _label: policy)

    with pytest.raises(_MODULE.GuardFailure, match=reason):
        _MODULE._standing_authorization_evidence(parse_work_block(metadata), repository, "build")


def test_standing_auto_rechecks_the_same_grant_in_two_independent_finalize_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _standing_policy()
    reads: list[str] = []
    _patch_standing(monkeypatch, base, live_policy=base, reads=reads)
    body = _standing_body()

    first = evaluate(_snapshot(body=body), phase="finalize")
    second = evaluate(_snapshot(body=body), phase="finalize")

    assert first.decision == "AUTO_FINALIZE_AUTHORIZED"
    assert second.decision == "AUTO_FINALIZE_AUTHORIZED"
    assert reads == [
        "base",
        "commit",
        "policy",
        "base",
        "commit",
        "policy",
    ]
    assert first.as_json()["finalization_authorization"]["live_main_sha"] == LIVE_MAIN_SHA


def test_standing_auto_stops_when_live_main_revokes_or_changes_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _standing_policy()
    body = _standing_body()

    _patch_standing(monkeypatch, base, live_policy=_standing_policy(status="REVOKED"))
    revoked = evaluate(_snapshot(body=body), phase="audit")
    assert revoked.decision == "GUARD FAILURE"
    assert "not ACTIVE in live main" in revoked.reasons[0]

    _patch_standing(
        monkeypatch,
        base,
        live_policy=_standing_policy(writer_roles=("BUILD_PRODUCT",)),
    )
    changed = evaluate(_snapshot(body=body), phase="audit")
    assert changed.decision == "GUARD FAILURE"
    assert "changed in live main" in changed.reasons[0]

    _patch_standing(monkeypatch, base, live_policy=_standing_policy(profiles=("FAST",)))
    reduced = evaluate(_snapshot(body=body), phase="audit")
    assert reduced.decision == "GUARD FAILURE"
    assert "does not bind the profile in live main" in reduced.reasons[0]

    _patch_standing(
        monkeypatch,
        base,
        live_policy=_standing_policy(authorization_id="other-standing"),
    )
    absent = evaluate(_snapshot(body=body), phase="audit")
    assert absent.decision == "GUARD FAILURE"
    assert "absent from live main" in absent.reasons[0]

    extra = json.loads(base)["standing_authorizations"][0]
    extra["authorization_id"] = "other-standing"
    _patch_standing(monkeypatch, base, live_policy=_standing_policy(extra_standing=(extra,)))
    unaffected = evaluate(_snapshot(body=body), phase="audit")
    assert unaffected.decision == "AUDIT GUARD PASS"


def test_standing_grant_is_never_read_from_candidate_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_MODULE, "_git_bytes", lambda _arguments, _label: _critical_auto_policy())

    result = evaluate(_snapshot(body=_standing_body()), phase="build")

    assert result.decision == "GUARD FAILURE"
    assert "absent from the verified base policy" in result.reasons[0]
    assert result.standing_authorization is None


def test_governance_writer_accepts_auto_only_with_a_verified_standing_authorization() -> None:
    body = _governance_writer_body(("scripts/check_workflow_guards.py",), policy="HUMAN").replace(
        "- **finalize_policy:** `HUMAN`.",
        f"- **finalize_policy:** `AUTO`.\n- **Finalization authorization:** `{STANDING_ID}`.",
    )
    metadata = parse_work_block(body)
    changed = (ChangedPath("scripts/check_workflow_guards.py", "modified"),)

    scope = _validate_scope(
        metadata,
        body,
        ScopeEvidence(changed, {}, {}),
        standing_authorization=_standing_evidence(),
    )
    assert scope.consumed_allowlist_paths == ("scripts/check_workflow_guards.py",)

    with pytest.raises(ValueError, match="governance path requires HUMAN policy"):
        _validate_scope(metadata, body, ScopeEvidence(changed, {}, {}))

    product_body = _governance_writer_body(
        ("scripts/check_workflow_guards.py", "docs/unrelated_product.md"),
        policy="HUMAN",
    ).replace(
        "- **finalize_policy:** `HUMAN`.",
        f"- **finalize_policy:** `AUTO`.\n- **Finalization authorization:** `{STANDING_ID}`.",
    )
    product_metadata = parse_work_block(product_body)
    with pytest.raises(ValueError, match="governance writer cannot modify product path"):
        _validate_scope(
            product_metadata,
            product_body,
            ScopeEvidence((ChangedPath("docs/unrelated_product.md", "modified"),), {}, {}),
            standing_authorization=_standing_evidence(),
        )

    product_writer_body = _governance_writer_body(
        ("scripts/check_workflow_guards.py",),
        policy="HUMAN",
        writer_role="BUILD_PRODUCT",
    ).replace(
        "- **finalize_policy:** `HUMAN`.",
        f"- **finalize_policy:** `AUTO`.\n- **Finalization authorization:** `{STANDING_ID}`.",
    )
    with pytest.raises(ValueError, match="product writer cannot modify governance path"):
        _validate_scope(
            parse_work_block(product_writer_body),
            product_writer_body,
            ScopeEvidence(changed, {}, {}),
            standing_authorization=_standing_evidence(),
        )


ADR_BLOCK = "FUNDAMENTALS-ADR-INTEGRATION"
ADR_BRANCH = "codex/fundamentals-adr-integration"
ADR_HEAD_SHA = "1" * 40
ADR_PATHS = (
    "docs/basic_functional_release_plan.md",
    "src/investment_analyst/catalog/models.py",
    "src/investment_analyst/analytics/valuation/service.py",
    "tests/unit/test_valuation.py",
)


def _adr_manifest() -> str:
    manifest = {
        "schema_version": "workflow-acceptance-manifest-v1",
        "route_effect": "ADVANCES",
        "items": [
            {
                "id": "A1",
                "kind": "acceptance",
                "requirements": [
                    "changed_path:docs/basic_functional_release_plan.md",
                    "changed_path:src/investment_analyst/catalog/models.py",
                    "focused_test:tests/unit/test_valuation.py",
                    "ci:Python 3.12 quality",
                    "smoke:governance-to-adr-product-transition-scratch",
                ],
            },
            {
                "id": "A2",
                "kind": "acceptance",
                "requirements": [
                    "route_transition:FUNDAMENTALS-COVERAGE:ADVANCES",
                    "live_probe:standing-grant-reused-without-ledger-edit",
                ],
            },
            {
                "id": "N1",
                "kind": "negative",
                "requirements": [
                    "changed_path:src/investment_analyst/analytics/valuation/service.py",
                ],
            },
        ],
    }
    return f"\n```json\n{json.dumps(manifest, separators=(',', ':'))}\n```\n"


def _adr_body() -> str:
    allowlist = "\n".join(f"- `{path}` — `{DECLARED_DIGEST}`." for path in ADR_PATHS)
    return f"""<!-- development-workflow:work-block-v1 -->

- **Work Block ID:** `{ADR_BLOCK}`.
- **Risk:** `R3`.
- **Profile:** `CRITICAL`.
- **finalize_policy:** `AUTO`.
- **route_effect:** `ADVANCES`.
- **Base remota exacta:** `origin/main@{BASE_SHA}`.
- **Expected branch:** `{ADR_BRANCH}`.
- **Writer role:** `BUILD_PRODUCT`.
- **Finalization authorization:** `{STANDING_ID}`.

## Strict delta allowlist

{allowlist}

## Superficies protegidas

### Checkpoint dormante — verificación de hash en el primary worktree

### Superficies del repositorio no modificables — deny por ruta cambiada

## Superficies prohibidas

- `docs/archive/**` — `deny`.
- `docs/incidents/**` — `deny`.
""" + _adr_manifest()


def _adr_route_document() -> str:
    return (
        "| Item | State | Note |\n"
        "| --- | --- | --- |\n"
        "| `FUNDAMENTALS-COVERAGE` | `NEXT` | ADR integration |\n"
        "| `OTHER` | `BLOCKED` | pending |\n"
    )


def _adr_comments(body: str | None = None) -> tuple[CommentSnapshot, ...]:
    resolved = body or _adr_body()
    return (
        _structured_marker(
            "build",
            resolved,
            sha=ADR_HEAD_SHA,
            block=ADR_BLOCK,
            comment_id=200,
        ),
        _structured_marker(
            "audit",
            resolved,
            sha=ADR_HEAD_SHA,
            block=ADR_BLOCK,
            comment_id=201,
        ),
    )


def _adr_snapshot(
    *,
    body: str | None = None,
    comments: tuple[CommentSnapshot, ...],
    branch: str = ADR_BRANCH,
    head_sha: str = ADR_HEAD_SHA,
    base_sha: str = BASE_SHA,
    changed_paths: tuple[ChangedPath, ...] | None = None,
    route_document: str | None = None,
    repository: str = STANDING_REPOSITORY,
    requested_changes: bool = False,
    open_threads: int = 0,
    smoke: SmokeSnapshot | None = None,
    mergeable: bool = True,
    mergeability_state: str = "clean",
    viewer_permission: str = "WRITE",
    base_protected: bool = True,
) -> GuardSnapshot:
    resolved_body = body or _adr_body()
    if changed_paths is None:
        changed_paths = tuple(ChangedPath(path, "modified") for path in ADR_PATHS)
    if route_document is None:
        route_document = _adr_route_document()
    if smoke is None:
        smoke = SmokeSnapshot("PASS", "scratch transition evidence")
    return GuardSnapshot(
        issue=_issue(resolved_body),
        pull_request=PullRequestSnapshot(371, "OPEN", branch, head_sha, "main", base_sha, True),
        comments=comments,
        checks=(CheckSnapshot("Python 3.12 quality", "completed", "success"),),
        smoke=smoke,
        requested_changes=requested_changes,
        open_threads=open_threads,
        source="live",
        mergeable=mergeable,
        mergeability_state=mergeability_state,
        viewer_permission=viewer_permission,
        base_protected=base_protected,
        scope_evidence=ScopeEvidence(changed_paths, {}, {}, frozenset(), route_document),
        repository=repository,
    )


def test_adr_product_transition_reaches_auto_finalize_without_human_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _standing_policy()
    reads: list[str] = []
    _patch_standing(monkeypatch, base, live_policy=base, reads=reads)
    body = _adr_body()
    comments = _adr_comments(body)

    build = evaluate(_adr_snapshot(body=body, comments=(comments[0],)), phase="build")
    assert build.decision == "READY"
    assert build.standing_authorization is not None
    assert build.standing_authorization.authorization_id == STANDING_ID

    audit = evaluate(_adr_snapshot(body=body, comments=comments), phase="audit")
    assert audit.decision == "AUDIT GUARD PASS"

    first = evaluate(_adr_snapshot(body=body, comments=comments), phase="finalize")
    second = evaluate(_adr_snapshot(body=body, comments=comments), phase="finalize")

    assert first.decision == "AUTO_FINALIZE_AUTHORIZED"
    assert second.decision == "AUTO_FINALIZE_AUTHORIZED"
    assert first.as_json()["finalization_authorization"]["status"] == "ACTIVE"
    assert reads == [
        "base",
        "base",
        "commit",
        "policy",
        "base",
        "commit",
        "policy",
        "base",
        "commit",
        "policy",
    ]


def test_adr_transition_fails_closed_on_scope_evidence_and_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _standing_policy()
    body = _adr_body()
    comments = _adr_comments(body)

    _patch_standing(monkeypatch, base, live_policy=base)
    first = evaluate(_adr_snapshot(body=body, comments=comments), phase="finalize")
    assert first.decision == "AUTO_FINALIZE_AUTHORIZED"

    _patch_standing(monkeypatch, base, live_policy=_standing_policy(status="REVOKED"))
    revoked = evaluate(_adr_snapshot(body=body, comments=comments), phase="finalize")
    assert revoked.decision == "GUARD FAILURE"
    assert "not ACTIVE in live main" in revoked.reasons[0]

    _patch_standing(monkeypatch, base, live_policy=base)
    outside = evaluate(
        _adr_snapshot(
            body=body,
            comments=comments,
            changed_paths=tuple(
                ChangedPath(path, "modified")
                for path in (*ADR_PATHS, "src/investment_analyst/analytics/valuation/unsafe.py")
            ),
        ),
        phase="build",
    )
    assert outside.decision == "GUARD FAILURE"
    assert "absent from strict allowlist" in outside.reasons[0]

    wrong_base_body = body.replace(f"origin/main@{BASE_SHA}", f"origin/main@{HISTORICAL_SHA}")
    monkeypatch.setattr(
        _MODULE,
        "_git_bytes",
        lambda _arguments, _label: _critical_auto_policy(),
    )
    wrong_base = evaluate(
        _adr_snapshot(
            body=wrong_base_body,
            comments=(comments[0],),
            base_sha=HISTORICAL_SHA,
        ),
        phase="build",
    )
    assert wrong_base.decision == "GUARD FAILURE"
    assert "absent from the verified base policy" in wrong_base.reasons[0]

    copied = _structured_marker("audit", body, sha=ADR_HEAD_SHA, block=ADR_BLOCK, comment_id=201)
    build_marker = _structured_marker(
        "build", body, sha=ADR_HEAD_SHA, block=ADR_BLOCK, comment_id=200
    )
    copied = replace(
        copied,
        body=copied.body.partition("-->\n")[0] + "-->\n" + build_marker.body.partition("-->\n")[2],
    )
    _patch_standing(monkeypatch, base, live_policy=base)
    copied_result = evaluate(
        _adr_snapshot(body=body, comments=(build_marker, copied)), phase="audit"
    )
    assert copied_result.decision == "GUARD FAILURE"
    assert "copied audit evidence payload" in copied_result.reasons[0]

    pending = _structured_marker("audit", body, sha=ADR_HEAD_SHA, block=ADR_BLOCK, comment_id=201)
    payload = json.loads(pending.body.partition("-->\n")[2])
    payload["items"][0]["verdict"] = "PENDING"
    pending = replace(
        pending,
        body=pending.body.partition("-->\n")[0]
        + "-->\n"
        + json.dumps(payload, separators=(",", ":"))
        + "\n",
    )
    pending_result = evaluate(
        _adr_snapshot(body=body, comments=(build_marker, pending)), phase="finalize"
    )
    assert pending_result.decision == "GUARD FAILURE"
    assert "not PASS" in pending_result.reasons[0]


def test_next_block_reuses_the_standing_grant_without_inheriting_prior_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_standing(monkeypatch, _standing_policy())
    next_block = "FUNDAMENTALS-IFRS-NEXT"
    next_branch = "codex/fundamentals-ifrs-next"
    next_sha = "3" * 40
    next_body = _adr_body().replace(ADR_BLOCK, next_block).replace(ADR_BRANCH, next_branch)

    metadata = parse_work_block(next_body)
    assert metadata.block == next_block
    assert metadata.expected_branch == next_branch
    assert metadata.finalization_authorization == STANDING_ID

    inherited = evaluate(
        _adr_snapshot(
            body=next_body,
            comments=_adr_comments(_adr_body()),
            branch=next_branch,
            head_sha=next_sha,
        ),
        phase="build",
    )
    assert inherited.decision == "GUARD FAILURE"

    fresh = _structured_marker("build", next_body, sha=next_sha, block=next_block, comment_id=300)
    rebuilt = evaluate(
        _adr_snapshot(
            body=next_body,
            comments=(fresh,),
            branch=next_branch,
            head_sha=next_sha,
        ),
        phase="build",
    )
    assert rebuilt.decision == "READY"
    assert rebuilt.standing_authorization is not None
    assert rebuilt.standing_authorization.authorization_id == STANDING_ID
