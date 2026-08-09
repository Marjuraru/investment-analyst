from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FULL_SHA = "a" * 40


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _frontmatter_value(text: str, key: str) -> str:
    frontmatter = text.split("---", maxsplit=2)[1]
    for line in frontmatter.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", maxsplit=1)[1].strip()
    raise AssertionError(f"missing frontmatter key: {key}")


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_static_contract_cross_references_skills_permissions_markers_and_alias() -> None:
    protocol = _read("docs/development_protocol.md")
    skill_paths = {
        "plan": ".agents/skills/plan/SKILL.md",
        "build": ".agents/skills/build/SKILL.md",
        "audit": ".agents/skills/audit/SKILL.md",
        "investment-block-flow": ".agents/skills/investment-block-flow/SKILL.md",
    }
    skills = {name: _read(path) for name, path in skill_paths.items()}
    core_rule = _read(".agents/rules/investment-analyst-core.md")
    audit_alias = _read(".agents/workflows/audit.md")

    assert protocol.count("## Algoritmo canónico de transición operativa") == 1
    assert protocol.count("| Condición viva | Decisión | Siguiente acción obligatoria |") == 1
    assert "PENDING|PASS|FAIL" in protocol
    assert protocol.count("<!-- development-workflow:build-v1") == 1
    assert protocol.count("<!-- development-workflow:audit-v1") == 1
    assert "development-workflow:superseded-v1" in protocol
    assert "snapshot batched de guards vivos" in protocol
    assert "segundo snapshot/revalidación crítica" in protocol
    assert "squash merge con --match-head-commit" in protocol
    assert "Terra High" in protocol

    for name, text in skills.items():
        assert _frontmatter_value(text, "name") == name
        assert "docs/development_protocol.md" in text
        assert len(text.splitlines()) < 40
        assert "| Condición viva |" not in text

    assert "No crear rama" in skills["plan"]
    assert "Stagear sólo" in skills["build"]
    assert "commit, push" in skills["build"]
    assert "PR draft" in skills["build"]
    assert "read-only" in skills["audit"]
    assert "diff completo" in skills["audit"]
    assert "policy HUMAN" in skills["audit"]
    assert "read-only" in core_rule
    assert "snapshot antes y" in core_rule
    assert "`.agents/skills/audit/SKILL.md`" in audit_alias
    assert "policy HUMAN" in audit_alias


def test_remote_only_base_and_absent_branch_progress_from_exact_remote_sha(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    client = tmp_path / "client"
    remote.mkdir()
    seed.mkdir()
    client.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _git(seed, "init", "--initial-branch=main")
    (seed / "contract.txt").write_text("verified base\n", encoding="utf-8")
    _git(seed, "add", "contract.txt")
    _git(
        seed,
        "-c",
        "user.name=workflow-test",
        "-c",
        "user.email=workflow-test@example.invalid",
        "commit",
        "-m",
        "base",
    )
    declared_sha = _git(seed, "rev-parse", "HEAD")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "origin", "main")

    _git(client, "init", "--initial-branch=unrelated")
    _git(client, "remote", "add", "origin", str(remote))
    assert _git(client, "branch", "--list", "work-block") == ""

    _git(
        client,
        "fetch",
        "origin",
        "refs/heads/main:refs/remotes/origin/work-block-base",
    )
    acquired_sha = _git(client, "rev-parse", "refs/remotes/origin/work-block-base")
    assert acquired_sha == declared_sha
    _git(client, "switch", "-c", "work-block", "refs/remotes/origin/work-block-base")

    assert _git(client, "branch", "--show-current") == "work-block"
    assert _git(client, "rev-parse", "HEAD") == declared_sha
    assert (client / "contract.txt").read_text(encoding="utf-8") == "verified base\n"


def _protected_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _protected_hash_decision(path: Path, expected: str) -> tuple[str, str]:
    if _protected_hash(path) != expected:
        return "GUARD FAILURE", "stop without restoring or modifying protected file"
    return "CONTINUE", "preserve protected bytes and continue"


def test_protected_hash_uses_worktree_bytes_and_never_repairs_a_mismatch(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected.md"
    original_bytes = b"local work\r\nwith exact bytes\n"
    protected.write_bytes(original_bytes)
    exact_hash = hashlib.sha256(original_bytes).hexdigest()

    assert _protected_hash_decision(protected, exact_hash) == (
        "CONTINUE",
        "preserve protected bytes and continue",
    )
    assert _protected_hash_decision(protected, "0" * 64) == (
        "GUARD FAILURE",
        "stop without restoring or modifying protected file",
    )
    assert protected.read_bytes() == original_bytes


@dataclass(frozen=True, slots=True)
class BuildSnapshot:
    target_valid: bool = True
    protected_hashes_match: bool = True
    scope_expansion: bool = False
    external_resource_available: bool = True
    base_acquired: bool = True
    branch_exists: bool = True
    focused_tests: str = "pass"
    ci: str = "pass"
    smoke: str = "pass"


def _build_transition(snapshot: BuildSnapshot) -> tuple[str, str]:
    if not snapshot.target_valid:
        return "GUARD FAILURE", "stop on contradictory target metadata"
    if not snapshot.protected_hashes_match:
        return "GUARD FAILURE", "stop without modifying protected work"
    if snapshot.scope_expansion:
        return "BLOCKED", "return to PLAN for material scope expansion"
    if not snapshot.external_resource_available:
        return "BLOCKED", "request the specific missing external resource"
    if not snapshot.base_acquired:
        return "CONTINUE", "fetch and verify the declared remote base SHA"
    if not snapshot.branch_exists:
        return "CONTINUE", "create expected branch from verified remote base"
    if snapshot.focused_tests == "correctable-fail":
        return "FIX", "correct focused test failure within scope"
    if snapshot.ci == "pending":
        return "WAIT/POLL", "poll required CI until terminal"
    if snapshot.ci == "correctable-fail":
        return "FIX", "correct own CI failure and publish a new candidate"
    if snapshot.smoke in {"pending", "long-running"}:
        return "CONTINUE", "execute or continue authorized smoke until terminal"
    if snapshot.smoke == "correctable-fail":
        return "FIX", "correct smoke failure within scope and rerun"
    return "READY", "publish BUILD PASS for the live SHA"


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (
            BuildSnapshot(base_acquired=False),
            ("CONTINUE", "fetch and verify the declared remote base SHA"),
        ),
        (
            BuildSnapshot(branch_exists=False),
            ("CONTINUE", "create expected branch from verified remote base"),
        ),
        (
            BuildSnapshot(protected_hashes_match=False),
            ("GUARD FAILURE", "stop without modifying protected work"),
        ),
        (
            BuildSnapshot(focused_tests="correctable-fail"),
            ("FIX", "correct focused test failure within scope"),
        ),
        (
            BuildSnapshot(ci="pending"),
            ("WAIT/POLL", "poll required CI until terminal"),
        ),
        (
            BuildSnapshot(ci="correctable-fail"),
            ("FIX", "correct own CI failure and publish a new candidate"),
        ),
        (
            BuildSnapshot(),
            ("READY", "publish BUILD PASS for the live SHA"),
        ),
        (
            BuildSnapshot(smoke="pending"),
            ("CONTINUE", "execute or continue authorized smoke until terminal"),
        ),
        (
            BuildSnapshot(smoke="long-running"),
            ("CONTINUE", "execute or continue authorized smoke until terminal"),
        ),
        (
            BuildSnapshot(smoke="correctable-fail"),
            ("FIX", "correct smoke failure within scope and rerun"),
        ),
        (
            BuildSnapshot(external_resource_available=False),
            ("BLOCKED", "request the specific missing external resource"),
        ),
        (
            BuildSnapshot(scope_expansion=True),
            ("BLOCKED", "return to PLAN for material scope expansion"),
        ),
    ],
)
def test_build_transition_matrix(
    snapshot: BuildSnapshot,
    expected: tuple[str, str],
) -> None:
    assert _build_transition(snapshot) == expected


@dataclass(frozen=True, slots=True)
class Comment:
    comment_id: int
    body: str


@dataclass(frozen=True, slots=True)
class Marker:
    role: str
    block: str
    sha: str
    status: str
    payload: str


ACTIVE_MARKER = re.compile(
    r"<!-- development-workflow:(?P<role>build|audit)-v1\n"
    r"(?P<fields>.*?)\n-->",
    flags=re.DOTALL,
)


def _normalize_payload(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.splitlines()).rstrip()


def _parse_marker(comment: Comment, role: str) -> Marker | None:
    matches = list(ACTIVE_MARKER.finditer(comment.body))
    role_openers = comment.body.count(f"<!-- development-workflow:{role}-v1")
    role_matches = [match for match in matches if match.group("role") == role]
    if role_openers != len(role_matches) or len(role_matches) > 1:
        raise ValueError("malformed or repeated active marker")
    if not role_matches:
        return None

    match = role_matches[0]
    fields: dict[str, str] = {}
    for line in match.group("fields").splitlines():
        if "=" not in line:
            raise ValueError("malformed marker field")
        key, value = line.split("=", maxsplit=1)
        if not key or not value or key in fields:
            raise ValueError("malformed marker field")
        fields[key] = value
    if fields.get("role", role) != role:
        raise ValueError("marker role mismatch")
    if not fields.get("block") or not re.fullmatch(r"[0-9a-f]{40}", fields.get("sha", "")):
        raise ValueError("missing block or full SHA")
    allowed_statuses = {"PENDING", "PASS", "FAIL"} if role == "build" else {"PASS", "FAIL"}
    if fields.get("status") not in allowed_statuses:
        raise ValueError("invalid marker status")
    if role == "audit" and not fields.get("reviewer"):
        raise ValueError("missing audit reviewer")
    return Marker(
        role=role,
        block=fields["block"],
        sha=fields["sha"],
        status=fields["status"],
        payload=_normalize_payload(comment.body),
    )


def _supersede(comment: Comment, role: str, canonical_id: int) -> Comment:
    marker_name = f"<!-- development-workflow:{role}-v1"
    superseded = comment.body.replace(
        marker_name,
        "<!-- development-workflow:superseded-v1",
        1,
    )
    closing_index = superseded.index("-->")
    fields = f"role={role}\ncanonical_comment_id={canonical_id}\nreason=equivalent-duplicate\n"
    superseded = superseded[:closing_index] + fields + superseded[closing_index:]
    return replace(comment, body=superseded)


def _reconcile_markers(
    comments: tuple[Comment, ...],
    role: str,
) -> tuple[str, tuple[Comment, ...], int | None]:
    try:
        parsed = [(comment, _parse_marker(comment, role)) for comment in comments]
    except ValueError:
        return "GUARD FAILURE", comments, None
    active = [(comment, marker) for comment, marker in parsed if marker is not None]
    if not active:
        return "CREATE", comments, None
    if len(active) == 1:
        return "UPDATE", comments, active[0][0].comment_id

    first_marker = active[0][1]
    if any(marker != first_marker for _, marker in active[1:]):
        return "GUARD FAILURE", comments, None
    canonical_id = min(comment.comment_id for comment, _ in active)
    reconciled = tuple(
        comment
        if marker is None or comment.comment_id == canonical_id
        else _supersede(comment, role, canonical_id)
        for comment, marker in parsed
    )
    remaining = [
        marker for comment in reconciled if (marker := _parse_marker(comment, role)) is not None
    ]
    if len(remaining) != 1:
        return "GUARD FAILURE", comments, None
    return "RECONCILED", reconciled, canonical_id


def _marker_body(
    role: str,
    *,
    block: str = "DEV-4",
    sha: str = FULL_SHA,
    status: str = "PASS",
    evidence: str = "gate=CI PASS\nnegative=scope protected",
    marker_role: str | None = None,
) -> str:
    reviewer = "reviewer=independent-model\n" if role == "audit" else ""
    explicit_role = f"role={marker_role}\n" if marker_role is not None else ""
    return (
        f"<!-- development-workflow:{role}-v1\n"
        f"block={block}\n"
        f"sha={sha}\n"
        f"status={status}\n"
        f"{reviewer}{explicit_role}-->\n"
        f"{evidence}\n"
    )


@pytest.mark.parametrize("role", ["build", "audit"])
def test_equivalent_markers_reconcile_to_lowest_id_without_losing_evidence(role: str) -> None:
    body = _marker_body(role)
    comments = (Comment(41, body), Comment(7, body), Comment(19, "human comment"))

    result, reconciled, canonical_id = _reconcile_markers(comments, role)

    assert result == "RECONCILED"
    assert canonical_id == 7
    assert sum(f"development-workflow:{role}-v1" in item.body for item in reconciled) == 1
    duplicate = next(item for item in reconciled if item.comment_id == 41)
    assert "development-workflow:superseded-v1" in duplicate.body
    assert f"role={role}" in duplicate.body
    assert "canonical_comment_id=7" in duplicate.body
    assert "reason=equivalent-duplicate" in duplicate.body
    assert "gate=CI PASS" in duplicate.body
    assert "negative=scope protected" in duplicate.body


@pytest.mark.parametrize(
    ("role", "conflicting_body"),
    [
        ("build", _marker_body("build", marker_role="audit")),
        ("build", _marker_body("build", block="DEV-OTHER")),
        ("build", _marker_body("build", sha="b" * 40)),
        ("build", _marker_body("build", status="FAIL")),
        (
            "build",
            _marker_body("build", evidence="gate=CI PASS\nnegative=changed payload"),
        ),
        ("build", "<!-- development-workflow:build-v1\nblock=DEV-4\n"),
        ("audit", _marker_body("audit", marker_role="build")),
        ("audit", _marker_body("audit", block="DEV-OTHER")),
        ("audit", _marker_body("audit", sha="b" * 40)),
        ("audit", _marker_body("audit", status="FAIL")),
        (
            "audit",
            _marker_body("audit", evidence="gate=CI PASS\nnegative=changed payload"),
        ),
        ("audit", "<!-- development-workflow:audit-v1\nblock=DEV-4\n"),
    ],
    ids=[
        "build-role",
        "build-block",
        "build-sha",
        "build-status",
        "build-payload",
        "build-malformed",
        "audit-role",
        "audit-block",
        "audit-sha",
        "audit-status",
        "audit-payload",
        "audit-malformed",
    ],
)
def test_conflicting_or_malformed_markers_fail_without_mutation(
    role: str,
    conflicting_body: str,
) -> None:
    original = (Comment(7, _marker_body(role)), Comment(41, conflicting_body))

    result, reconciled, canonical_id = _reconcile_markers(original, role)

    assert result == "GUARD FAILURE"
    assert reconciled == original
    assert canonical_id is None


@dataclass(frozen=True, slots=True)
class AuditSnapshot:
    build_pass: bool = True
    ci_pass: bool = True
    semantic_bug: bool = False
    scope_creep: bool = False
    critical_acceptance_covered: bool = True
    critical_negative_covered: bool = True
    live_sha: str = FULL_SHA
    evidence_sha: str = FULL_SHA
    smoke_sufficient: bool = True
    requested_changes: bool = False
    open_threads: int = 0
    changed_files: int = 4
    reviewed_files: int = 4
    excluded_files: int = 0
    exclusions_justified: bool = True


def _audit_decision(snapshot: AuditSnapshot) -> tuple[str, str]:
    if snapshot.live_sha != snapshot.evidence_sha:
        return "FAIL", "SHA stale: BUILD evidence does not match live head"
    if not snapshot.build_pass or not snapshot.ci_pass:
        return "FAIL", "critical BUILD or CI evidence missing"
    if snapshot.semantic_bug:
        return "FAIL", "semantic bug in reviewed contract despite green gates"
    if snapshot.scope_creep:
        return "FAIL", "material file outside authorized scope"
    if not snapshot.critical_acceptance_covered:
        return "FAIL", "critical acceptance lacks evidence"
    if not snapshot.critical_negative_covered:
        return "FAIL", "critical negative case omitted"
    if not snapshot.smoke_sufficient:
        return "FAIL", "real smoke does not demonstrate required behavior"
    if snapshot.requested_changes:
        return "FAIL", "requested changes remain unresolved"
    if snapshot.open_threads:
        return "FAIL", "review thread remains unresolved"
    accounted = snapshot.reviewed_files + snapshot.excluded_files
    if accounted != snapshot.changed_files or (
        snapshot.excluded_files and not snapshot.exclusions_justified
    ):
        return "FAIL", "material diff is not fully reviewed and accounted"
    return "PASS", "full material diff reviewed with critical acceptance and negatives covered"


@pytest.mark.parametrize(
    ("snapshot", "expected_status", "evidence_fragment"),
    [
        (AuditSnapshot(semantic_bug=True), "FAIL", "semantic bug"),
        (AuditSnapshot(scope_creep=True), "FAIL", "outside authorized scope"),
        (AuditSnapshot(critical_acceptance_covered=False), "FAIL", "critical acceptance"),
        (AuditSnapshot(critical_negative_covered=False), "FAIL", "negative case omitted"),
        (AuditSnapshot(evidence_sha="b" * 40), "FAIL", "SHA stale"),
        (AuditSnapshot(smoke_sufficient=False), "FAIL", "smoke"),
        (AuditSnapshot(requested_changes=True), "FAIL", "requested changes"),
        (AuditSnapshot(open_threads=1), "FAIL", "review thread"),
        (AuditSnapshot(reviewed_files=3), "FAIL", "diff"),
        (AuditSnapshot(), "PASS", "full material diff reviewed"),
    ],
)
def test_audit_semantic_matrix_requires_concrete_evidence_and_full_diff(
    snapshot: AuditSnapshot,
    expected_status: str,
    evidence_fragment: str,
) -> None:
    status, evidence = _audit_decision(snapshot)
    assert status == expected_status
    assert evidence_fragment in evidence


@dataclass(frozen=True, slots=True)
class FinalizeSnapshot:
    live_head: str = FULL_SHA
    build_sha: str = FULL_SHA
    audit_sha: str = FULL_SHA
    ci_sha: str = FULL_SHA
    build_pass: bool = True
    audit_pass: bool = True
    ci_pass: bool = True
    smoke_pass: bool = True
    requested_changes: bool = False
    open_threads: int = 0
    mergeable: bool = True
    has_permission: bool = True
    branch_protected: bool = True


def _finalize_guard(snapshot: FinalizeSnapshot, expected_sha: str) -> str | None:
    if snapshot.live_head != expected_sha:
        return "live head changed"
    if {snapshot.build_sha, snapshot.audit_sha, snapshot.ci_sha} != {expected_sha}:
        return "BUILD, AUDIT or CI evidence is stale"
    if not all([snapshot.build_pass, snapshot.audit_pass, snapshot.ci_pass, snapshot.smoke_pass]):
        return "required evidence is not PASS"
    if snapshot.requested_changes:
        return "requested changes remain"
    if snapshot.open_threads:
        return "review thread remains"
    if not snapshot.mergeable:
        return "PR is not mergeable"
    if not snapshot.has_permission:
        return "merge permission is absent"
    if not snapshot.branch_protected:
        return "main branch protection is absent"
    return None


def _finalize_decision(
    before_ready: FinalizeSnapshot,
    after_ready: FinalizeSnapshot,
    *,
    policy: str,
) -> tuple[str, str]:
    first_failure = _finalize_guard(before_ready, FULL_SHA)
    if first_failure:
        return "FINALIZATION BLOCKED", f"first snapshot: {first_failure}"
    if policy == "HUMAN":
        return "HUMAN MERGE", "guards pass; do not mark ready or merge automatically"
    second_failure = _finalize_guard(after_ready, FULL_SHA)
    if second_failure:
        return "FINALIZATION BLOCKED", f"second snapshot: {second_failure}"
    return "MERGE", f"squash --match-head-commit {FULL_SHA}"


@pytest.mark.parametrize(
    ("before", "after", "expected_fragment"),
    [
        (FinalizeSnapshot(live_head="b" * 40), FinalizeSnapshot(), "first snapshot: live head"),
        (FinalizeSnapshot(), FinalizeSnapshot(live_head="b" * 40), "second snapshot: live head"),
        (FinalizeSnapshot(ci_sha="b" * 40), FinalizeSnapshot(), "evidence is stale"),
        (FinalizeSnapshot(), FinalizeSnapshot(audit_sha="b" * 40), "evidence is stale"),
        (FinalizeSnapshot(requested_changes=True), FinalizeSnapshot(), "requested changes"),
        (FinalizeSnapshot(open_threads=1), FinalizeSnapshot(), "review thread"),
        (FinalizeSnapshot(mergeable=False), FinalizeSnapshot(), "not mergeable"),
        (FinalizeSnapshot(has_permission=False), FinalizeSnapshot(), "permission"),
        (FinalizeSnapshot(branch_protected=False), FinalizeSnapshot(), "protection"),
    ],
)
def test_finalize_matrix_fails_closed_before_or_after_ready(
    before: FinalizeSnapshot,
    after: FinalizeSnapshot,
    expected_fragment: str,
) -> None:
    status, evidence = _finalize_decision(before, after, policy="AUTO")
    assert status == "FINALIZATION BLOCKED"
    assert expected_fragment in evidence


def test_finalize_happy_paths_preserve_exact_sha_and_human_policy() -> None:
    auto_status, auto_action = _finalize_decision(
        FinalizeSnapshot(),
        FinalizeSnapshot(),
        policy="AUTO",
    )
    human_status, human_action = _finalize_decision(
        FinalizeSnapshot(),
        FinalizeSnapshot(),
        policy="HUMAN",
    )

    assert auto_status == "MERGE"
    assert auto_action == f"squash --match-head-commit {FULL_SHA}"
    assert human_status == "HUMAN MERGE"
    assert human_action == "guards pass; do not mark ready or merge automatically"
