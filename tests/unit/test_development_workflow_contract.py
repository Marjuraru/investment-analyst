from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
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
        "ui": ".agents/skills/ui/SKILL.md",
    }
    skills = {name: _read(path) for name, path in skill_paths.items()}
    core_rule = _read(".agents/rules/investment-analyst-core.md")
    ui_agent = _read(".agents/skills/ui/agents/openai.yaml")
    template = _read(".github/ISSUE_TEMPLATE/work_block.yml")

    assert protocol.count("## Algoritmo canónico de transición operativa") == 1
    assert protocol.count("| Condición viva | Decisión | Siguiente acción obligatoria |") == 1
    assert "PENDING|PASS|FAIL" in protocol
    assert protocol.count("<!-- development-workflow:build-v1") == 1
    assert protocol.count("<!-- development-workflow:audit-v1") == 1
    assert "development-workflow:superseded-v1" in protocol
    assert "snapshot batched de guards vivos" in protocol
    assert "segundo snapshot/revalidación crítica" in protocol
    assert "squash merge con --match-head-commit" in protocol
    assert "FINALIZE es estrictamente live-only" in protocol
    assert "las páginas de `reviewThreads` resueltas" in protocol
    assert "Después de ready se descarta el primer snapshot" in protocol
    assert "threads persistentes" in protocol
    assert "sesión fresca e independiente" in protocol
    assert "chat Gemini nuevo" not in protocol
    assert "Terra High" not in protocol
    assert "modelo o cliente" in protocol
    assert "Python 3.12 quality" in protocol
    assert "fallo material de BUILD/FIX" in protocol
    assert "otro comando mutante están prohibidos" in protocol
    assert "transición mecánica explícita" in protocol
    assert "/ui" in protocol
    assert (
        "salvo la publicación/reconciliación estrecha de su propio marker machine-owned" in protocol
    )
    assert "AWAITING HUMAN APPROVAL" in protocol

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
    assert "scripts/check_workflow_guards.py" in skills["audit"]
    assert "scripts/check_workflow_guards.py" in skills["build"]
    assert "scripts/check_workflow_guards.py" in skills["investment-block-flow"]
    assert "--live --phase finalize" in skills["build"]
    assert "--live --phase finalize" in skills["audit"]
    assert "--live --phase finalize" in skills["investment-block-flow"]
    assert "intenta refutar" in skills["audit"]
    assert "comandos mutantes" in skills["audit"]
    assert (
        "La única mutación autorizada es la estrecha y machine-owned del propio comentario AUDIT"
        in skills["audit"]
    )
    assert "publica o reconcilia primero su único marker machine-owned exact-SHA" in skills["audit"]
    assert "un resultado FAIL registra `status=FAIL`" in skills["audit"]
    assert "Después de esa mutación estrecha relee GitHub y ejecuta nuevamente" in skills["audit"]
    assert (
        "Con PASS y policy HUMAN, el handoff termina `AWAITING HUMAN APPROVAL`" in skills["audit"]
    )
    assert "Con FAIL o policy HUMAN, terminar sin mutar PR." not in skills["audit"]
    for forbidden_action in (
        "gh pr ready",
        "merge",
        "cleanup",
        "cierre del Issue",
        "cambios de label",
    ):
        assert forbidden_action in skills["audit"]
    assert "writer role válido" in skills["plan"]
    assert "exactamente BUILD" in skills["ui"]
    assert "único writer" in skills["ui"]
    assert "local_web.py" in skills["ui"]
    assert "scratch externo" in skills["ui"]
    assert "Gemini persistente" not in skills["ui"]
    assert "allow_implicit_invocation: false" in ui_agent
    assert "read-only" in core_rule
    assert "Formatter, fixer" in core_rule
    assert "snapshot antes y" in core_rule
    assert "--live --phase finalize" in core_rule
    assert "control-plane-first" in skills["plan"]
    assert "exploración dirigida" in skills["plan"]
    assert "id: writer_role" in template
    assert "options: [BUILD, UI_WORKER]" in template
    assert "id: owner" not in template


def test_canonical_skill_basenames_and_ui_frontier_are_unique() -> None:
    skill_paths = sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md"))
    basenames = [path.parent.name for path in skill_paths]

    assert basenames.count("audit") == 1
    assert basenames.count("ui") == 1
    assert not (ROOT / ".agents" / "workflows" / "audit.md").exists()
    assert not (ROOT / ".agents" / "workflows" / "ui.md").exists()

    ui = _read(".agents/skills/ui/SKILL.md")
    assert "src/investment_analyst/frontend/static/**" in ui
    assert "no tocar `local_web.py` sin allowlist explícita" in ui
    assert "No usar subagentes writers, worktrees paralelos ni handoff" in ui
    assert "BUILD READY`, `BUILD BLOCKED` o `BUILD GUARD FAILURE`" in ui


@dataclass(frozen=True, slots=True)
class UiWorkerPreflight:
    target_valid: bool = True
    writer_is_exclusive: bool = True
    scope_is_ui: bool = True


def _ui_worker_preflight(
    snapshot: UiWorkerPreflight,
    attempted_actions: list[str],
) -> tuple[str, str]:
    if not snapshot.target_valid:
        return "GUARD FAILURE", "stop before branch, file, or PR mutation"
    if not snapshot.writer_is_exclusive or not snapshot.scope_is_ui:
        return "BLOCKED", "return to PLAN before expanding authority or scope"
    attempted_actions.append("read-only target resolution")
    return "CONTINUE", "execute the existing BUILD path"


def test_ui_worker_preflight_rejects_missing_target_without_mutation() -> None:
    attempted_actions: list[str] = []

    status, evidence = _ui_worker_preflight(
        UiWorkerPreflight(target_valid=False),
        attempted_actions,
    )

    assert status == "GUARD FAILURE"
    assert "before branch, file, or PR" in evidence
    assert attempted_actions == []


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
    protected_set_exact: bool = True
    canonical_markers: bool = True
    required_gate_name: str = "Python 3.12 quality"
    required_action_pending: bool = False
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
    if not snapshot.protected_set_exact:
        return "GUARD FAILURE", "declared and observed protected sets differ"
    if not snapshot.canonical_markers:
        return "GUARD FAILURE", "active markers are not canonical"
    if snapshot.required_gate_name != "Python 3.12 quality":
        return "GUARD FAILURE", "required CI gate name is not literal"
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
    if snapshot.ci in {"queued", "in_progress"}:
        return "WAIT/POLL", "poll required CI until terminal"
    if snapshot.ci == "unknown":
        return "GUARD FAILURE", "required CI state is not recognized"
    if snapshot.ci == "correctable-fail":
        return "FIX", "correct own CI failure and publish a new candidate"
    if snapshot.smoke in {"pending", "long-running"}:
        return "CONTINUE", "execute or continue authorized smoke until terminal"
    if snapshot.smoke == "correctable-fail":
        return "FIX", "correct smoke failure within scope and rerun"
    if snapshot.required_action_pending:
        return "CONTINUE", "complete the remaining required action"
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
            BuildSnapshot(protected_set_exact=False),
            ("GUARD FAILURE", "declared and observed protected sets differ"),
        ),
        (
            BuildSnapshot(canonical_markers=False),
            ("GUARD FAILURE", "active markers are not canonical"),
        ),
        (
            BuildSnapshot(required_gate_name="Python quality"),
            ("GUARD FAILURE", "required CI gate name is not literal"),
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
            BuildSnapshot(ci="queued"),
            ("WAIT/POLL", "poll required CI until terminal"),
        ),
        (
            BuildSnapshot(ci="unknown"),
            ("GUARD FAILURE", "required CI state is not recognized"),
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
        (
            BuildSnapshot(required_action_pending=True),
            ("CONTINUE", "complete the remaining required action"),
        ),
    ],
)
def test_build_transition_matrix(
    snapshot: BuildSnapshot,
    expected: tuple[str, str],
) -> None:
    assert _build_transition(snapshot) == expected


@dataclass(frozen=True, slots=True)
class AuditSnapshot:
    build_pass: bool = True
    ci_pass: bool = True
    semantic_bug: bool = False
    scope_creep: bool = False
    critical_acceptance_covered: bool = True
    critical_invariant_covered: bool = True
    critical_negative_covered: bool = True
    base_sha: str = FULL_SHA
    live_sha: str = FULL_SHA
    evidence_sha: str = FULL_SHA
    branch_read_live: bool = True
    contradiction_resolved: bool = True
    tests_preserved_or_equivalent: bool = True
    material_fix_exists: bool = False
    fix_probe_verified: bool = True
    smoke_sufficient: bool = True
    requested_changes: bool = False
    open_threads: int = 0
    changed_files: int = 4
    reviewed_files: int = 4
    excluded_files: int = 0
    exclusions_justified: bool = True


def _audit_decision(snapshot: AuditSnapshot) -> tuple[str, str]:
    if snapshot.base_sha != FULL_SHA or not snapshot.branch_read_live:
        return "FAIL", "base or branch identifier was not read live"
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
    if not snapshot.critical_invariant_covered:
        return "FAIL", "critical invariant lacks evidence"
    if not snapshot.critical_negative_covered:
        return "FAIL", "critical negative case omitted"
    if not snapshot.contradiction_resolved:
        return "FAIL", "contradictory evidence remains unresolved"
    if not snapshot.tests_preserved_or_equivalent:
        return "FAIL", "test coverage was removed or weakened"
    if snapshot.material_fix_exists and not snapshot.fix_probe_verified:
        return "FAIL", "material BUILD/FIX probe was not verified"
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
        (AuditSnapshot(critical_invariant_covered=False), "FAIL", "critical invariant"),
        (AuditSnapshot(critical_negative_covered=False), "FAIL", "negative case omitted"),
        (AuditSnapshot(base_sha="b" * 40), "FAIL", "base or branch"),
        (AuditSnapshot(branch_read_live=False), "FAIL", "base or branch"),
        (AuditSnapshot(evidence_sha="b" * 40), "FAIL", "SHA stale"),
        (AuditSnapshot(contradiction_resolved=False), "FAIL", "contradictory evidence"),
        (AuditSnapshot(tests_preserved_or_equivalent=False), "FAIL", "removed or weakened"),
        (
            AuditSnapshot(material_fix_exists=True, fix_probe_verified=False),
            "FAIL",
            "BUILD/FIX probe",
        ),
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


@pytest.mark.parametrize("command", ["ruff format file.py", "ruff check --fix file.py"])
def test_audit_rejects_mutating_validation_commands(command: str) -> None:
    assert _audit_validation_command(command) == "REJECT MUTATING COMMAND"


def test_audit_allows_read_only_focused_validation() -> None:
    assert _audit_validation_command("pytest tests/unit/test_contract.py") == "ALLOW READ-ONLY"


def _audit_validation_command(command: str) -> str:
    if " format" in command or "--fix" in command:
        return "REJECT MUTATING COMMAND"
    return "ALLOW READ-ONLY"


@pytest.mark.parametrize("model_metadata", ["model-a", "model-b", "client-c"])
def test_role_permissions_do_not_depend_on_model_metadata(model_metadata: str) -> None:
    assert _role_permissions(model_metadata) == _role_permissions("other-client")


def _role_permissions(model_metadata: str) -> dict[str, str]:
    del model_metadata
    return {
        "PLAN": "control-plane",
        "BUILD": "single-writer",
        "UI_WORKER": "ui-only-writer",
        "AUDIT": "read-only",
    }


def test_role_permissions_are_explicit_and_complete() -> None:
    permissions = {
        "PLAN": "control-plane",
        "BUILD": "single-writer",
        "UI_WORKER": "ui-only-writer",
        "AUDIT": "read-only",
    }
    assert _role_permissions("evidence-only") == permissions


@dataclass(frozen=True, slots=True)
class FinalizeSnapshot:
    live_head: str = FULL_SHA
    build_sha: str = FULL_SHA
    audit_sha: str = FULL_SHA
    ci_sha: str = FULL_SHA
    build_pass: bool = True
    audit_pass: bool = True
    ci_pass: bool = True
    required_gate_name: str = "Python 3.12 quality"
    smoke_pass: bool = True
    phase_transitioned: bool = True
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
    if snapshot.required_gate_name != "Python 3.12 quality":
        return "required CI gate name is not literal"
    if not snapshot.phase_transitioned:
        return "mechanical phase transition is absent"
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
        (
            FinalizeSnapshot(phase_transitioned=False),
            FinalizeSnapshot(),
            "mechanical phase transition",
        ),
        (
            FinalizeSnapshot(required_gate_name="Python quality"),
            FinalizeSnapshot(),
            "gate name is not literal",
        ),
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
