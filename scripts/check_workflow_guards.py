#!/usr/bin/env python3
"""Read-only, fail-closed guards for Development Workflow v1.

The CLI accepts a temporary JSON snapshot or obtains the same snapshot with ``gh``.
It never edits GitHub, the worktree, or a supplied snapshot.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

FULL_SHA = re.compile(r"[0-9a-f]{40}")
RESERVED_TOKEN = re.compile(r"development-workflow:(?:build-v1|audit-v1|superseded-v1)")
MARKER_BLOCK = re.compile(
    r"\A<!-- development-workflow:(?P<kind>build-v1|audit-v1|superseded-v1)\n"
    r"(?P<fields>.*?)\n-->(?P<payload>.*)\Z",
    flags=re.DOTALL,
)
METADATA_FIELD = re.compile(
    r"^\s*-\s+\*\*(?P<key>[^*]+):\*\*\s*(?P<value>.*?)\s*$",
    flags=re.MULTILINE,
)


class GuardFailure(ValueError):
    """A contradictory, missing, or malformed live guard input."""


@dataclass(frozen=True, slots=True)
class WorkBlockMetadata:
    block: str
    profile: str
    policy: str
    base_ref: str
    base_sha: str
    expected_branch: str
    writer_role: str


@dataclass(frozen=True, slots=True)
class IssueSnapshot:
    number: int
    state: str
    labels: frozenset[str]
    body: str


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    number: int
    state: str
    head_branch: str
    head_sha: str
    base_branch: str
    base_sha: str
    is_draft: bool


@dataclass(frozen=True, slots=True)
class CommentSnapshot:
    comment_id: int
    body: str


@dataclass(frozen=True, slots=True)
class CheckSnapshot:
    name: str
    status: str
    conclusion: str


@dataclass(frozen=True, slots=True)
class SmokeSnapshot:
    status: str | None
    evidence: str


@dataclass(frozen=True, slots=True)
class GuardSnapshot:
    issue: IssueSnapshot
    pull_request: PullRequestSnapshot
    comments: tuple[CommentSnapshot, ...]
    checks: tuple[CheckSnapshot, ...]
    smoke: SmokeSnapshot
    requested_changes: bool | None
    open_threads: int | None
    active_issue_count: int | None = None
    open_pr_count: int | None = None
    source: str = "unknown"
    mergeable: bool | None = None
    mergeability_state: str | None = None
    viewer_permission: str | None = None
    base_protected: bool | None = None


@dataclass(frozen=True, slots=True)
class MarkerRecord:
    comment_id: int
    role: str | None
    kind: str
    block: str
    sha: str
    status: str
    reviewer: str | None
    payload: str
    fields: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class MarkerResolution:
    marker: MarkerRecord | None
    duplicates: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class GuardResult:
    decision: str
    reasons: tuple[str, ...]
    metadata: WorkBlockMetadata | None
    build: MarkerResolution
    audit: MarkerResolution
    mutation_plan: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        metadata: dict[str, object] | None = None
        if self.metadata is not None:
            metadata = {
                "block": self.metadata.block,
                "profile": self.metadata.profile,
                "policy": self.metadata.policy,
                "base_ref": self.metadata.base_ref,
                "base_sha": self.metadata.base_sha,
                "expected_branch": self.metadata.expected_branch,
                "writer_role": self.metadata.writer_role,
            }
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "metadata": metadata,
            "markers": {
                "build": _resolution_json(self.build),
                "audit": _resolution_json(self.audit),
            },
            "mutation_plan": list(self.mutation_plan),
        }


def _resolution_json(resolution: MarkerResolution) -> dict[str, object]:
    return {
        "canonical_comment_id": (
            resolution.marker.comment_id if resolution.marker is not None else None
        ),
        "duplicate_comment_ids": list(resolution.duplicates),
        "status": resolution.marker.status if resolution.marker is not None else None,
    }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GuardFailure(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise GuardFailure(f"{label} must be an array")
    return tuple(value)


def _required_str(mapping: Mapping[str, object], keys: tuple[str, ...], label: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    raise GuardFailure(f"{label} is missing")


def _required_int(mapping: Mapping[str, object], keys: tuple[str, ...], label: str) -> int:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    raise GuardFailure(f"{label} is missing")


def _required_bool(mapping: Mapping[str, object], keys: tuple[str, ...], label: str) -> bool:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            return value
    raise GuardFailure(f"{label} is missing")


def _optional_bool(mapping: Mapping[str, object], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            return value
    return None


def _optional_str(mapping: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _clean_value(value: str) -> str:
    code_value = re.search(r"`([^`]+)`", value)
    if code_value is not None:
        return code_value.group(1).strip()
    return value.strip().rstrip(".;")


def parse_work_block(body: str) -> WorkBlockMetadata:
    aliases = {
        "work block id": "block",
        "profile": "profile",
        "finalize_policy": "policy",
        "base remota exacta": "base",
        "expected branch": "expected_branch",
        "writer role": "writer_role",
    }
    values: dict[str, str] = {}
    for match in METADATA_FIELD.finditer(body):
        key = match.group("key").strip().lower()
        target = aliases.get(key)
        if target is None:
            continue
        if target in values:
            raise GuardFailure(f"duplicate Work Block field: {target}")
        values[target] = _clean_value(match.group("value"))

    required = ("block", "profile", "policy", "base", "expected_branch", "writer_role")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise GuardFailure(f"missing Work Block fields: {','.join(missing)}")

    base_match = re.fullmatch(r"(?P<ref>[^@\s]+)@(?P<sha>[0-9a-f]{40})", values["base"])
    if base_match is None:
        raise GuardFailure("base must be a remote ref and full SHA")
    profile = values["profile"].upper()
    policy = values["policy"].upper()
    writer_role = values["writer_role"].upper().replace("-", "_")
    if profile not in {"FAST", "STANDARD", "CRITICAL"}:
        raise GuardFailure("profile is unknown")
    if policy not in {"AUTO", "HUMAN"}:
        raise GuardFailure("finalize_policy is unknown")
    if profile == "CRITICAL" and policy != "HUMAN":
        raise GuardFailure("CRITICAL requires finalize_policy HUMAN")
    if writer_role not in {"PLAN", "BUILD", "UI_WORKER", "AUDIT"}:
        raise GuardFailure("writer role is unknown")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", values["block"]):
        raise GuardFailure("Work Block ID is invalid")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", values["expected_branch"]):
        raise GuardFailure("expected branch is invalid")
    return WorkBlockMetadata(
        block=values["block"],
        profile=profile,
        policy=policy,
        base_ref=base_match.group("ref"),
        base_sha=base_match.group("sha"),
        expected_branch=values["expected_branch"],
        writer_role=writer_role,
    )


def _parse_labels(value: object) -> frozenset[str]:
    labels: set[str] = set()
    for item in _sequence(value, "issue.labels"):
        if isinstance(item, str):
            labels.add(item)
            continue
        item_map = _mapping(item, "issue.label")
        name = item_map.get("name")
        if isinstance(name, str):
            labels.add(name)
    return frozenset(labels)


def _parse_issue(value: object) -> IssueSnapshot:
    item = _mapping(value, "issue")
    return IssueSnapshot(
        number=_required_int(item, ("number",), "issue.number"),
        state=_required_str(item, ("state",), "issue.state").upper(),
        labels=_parse_labels(item.get("labels", ())),
        body=_required_str(item, ("body",), "issue.body"),
    )


def _parse_pull_request(value: object) -> PullRequestSnapshot:
    item = _mapping(value, "pull_request")
    return PullRequestSnapshot(
        number=_required_int(item, ("number",), "pull_request.number"),
        state=_required_str(item, ("state",), "pull_request.state").upper(),
        head_branch=_required_str(item, ("headRefName", "head_branch"), "head branch"),
        head_sha=_required_str(item, ("headRefOid", "head_sha"), "head SHA"),
        base_branch=_required_str(item, ("baseRefName", "base_branch"), "base branch"),
        base_sha=_required_str(item, ("baseRefOid", "base_sha"), "base SHA"),
        is_draft=_required_bool(item, ("isDraft", "is_draft"), "pull_request.isDraft"),
    )


def _parse_comments(value: object) -> tuple[CommentSnapshot, ...]:
    comments: list[CommentSnapshot] = []
    for index, item in enumerate(_sequence(value, "comments")):
        mapping = _mapping(item, f"comments[{index}]")
        comments.append(
            CommentSnapshot(
                comment_id=_required_int(mapping, ("id", "comment_id"), "comment.id"),
                body=_required_str(mapping, ("body",), "comment.body"),
            )
        )
    return tuple(comments)


def _parse_checks(value: object) -> tuple[CheckSnapshot, ...]:
    checks: list[CheckSnapshot] = []
    for index, item in enumerate(_sequence(value, "checks")):
        mapping = _mapping(item, f"checks[{index}]")
        conclusion = mapping.get("conclusion", "")
        if not isinstance(conclusion, str):
            conclusion = ""
        checks.append(
            CheckSnapshot(
                name=_required_str(mapping, ("name",), f"checks[{index}].name"),
                status=_required_str(mapping, ("status",), f"checks[{index}].status").lower(),
                conclusion=conclusion.lower(),
            )
        )
    return tuple(checks)


def _parse_smoke(value: object) -> SmokeSnapshot:
    if value is None:
        return SmokeSnapshot(status=None, evidence="")
    mapping = _mapping(value, "smoke")
    status = mapping.get("status")
    evidence = mapping.get("evidence", "")
    if status is not None and not isinstance(status, str):
        raise GuardFailure("smoke.status must be a string")
    if not isinstance(evidence, str):
        raise GuardFailure("smoke.evidence must be a string")
    return SmokeSnapshot(status=status.upper() if status is not None else None, evidence=evidence)


def snapshot_from_json(value: object) -> GuardSnapshot:
    root = _mapping(value, "snapshot")
    requested_changes = root.get("requested_changes")
    if requested_changes is not None and not isinstance(requested_changes, bool):
        raise GuardFailure("requested_changes must be boolean")
    open_threads = root.get("open_threads")
    if open_threads is not None and (
        not isinstance(open_threads, int) or isinstance(open_threads, bool) or open_threads < 0
    ):
        raise GuardFailure("open_threads must be a non-negative integer")
    active_issue_count = root.get("active_issue_count")
    open_pr_count = root.get("open_pr_count")
    for count, label in (
        (active_issue_count, "active_issue_count"),
        (open_pr_count, "open_pr_count"),
    ):
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or count < 0
        ):
            raise GuardFailure(f"{label} must be a non-negative integer")
    return GuardSnapshot(
        issue=_parse_issue(root.get("issue")),
        pull_request=_parse_pull_request(root.get("pull_request")),
        comments=_parse_comments(root.get("comments", ())),
        checks=_parse_checks(root.get("checks", ())),
        smoke=_parse_smoke(root.get("smoke")),
        requested_changes=requested_changes,
        open_threads=cast(int | None, open_threads),
        active_issue_count=cast(int | None, active_issue_count),
        open_pr_count=cast(int | None, open_pr_count),
        source="json",
        mergeable=_optional_bool(root, ("mergeable",)),
        mergeability_state=_optional_str(root, ("mergeability_state",)),
        viewer_permission=_optional_str(root, ("viewer_permission",)),
        base_protected=_optional_bool(root, ("base_protected",)),
    )


def _normalize_lines(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.splitlines()).rstrip()


def parse_marker(comment: CommentSnapshot) -> MarkerRecord | None:
    body = comment.body.replace("\r\n", "\n").replace("\r", "\n")
    tokens = list(RESERVED_TOKEN.finditer(body))
    if not tokens:
        return None
    if len(tokens) != 1:
        raise GuardFailure(f"comment {comment.comment_id} has repeated marker tokens")
    match = MARKER_BLOCK.fullmatch(body)
    if match is None:
        raise GuardFailure(f"comment {comment.comment_id} has malformed marker structure")
    kind = match.group("kind")
    fields: dict[str, str] = {}
    for line in match.group("fields").split("\n"):
        if not line or "=" not in line:
            raise GuardFailure(f"comment {comment.comment_id} has malformed marker fields")
        key, value = line.split("=", maxsplit=1)
        if not re.fullmatch(r"[a-z_]+", key) or not value or key in fields:
            raise GuardFailure(f"comment {comment.comment_id} has invalid marker fields")
        fields[key] = value

    if kind == "build-v1":
        expected_keys = {"block", "sha", "status"}
        role = "build"
        reviewer = None
        allowed_statuses = {"PENDING", "PASS", "FAIL"}
    elif kind == "audit-v1":
        expected_keys = {"block", "sha", "status", "reviewer"}
        role = "audit"
        reviewer = fields.get("reviewer")
        allowed_statuses = {"PASS", "FAIL"}
    else:
        role = fields.get("role", "")
        if role not in {"build", "audit"}:
            raise GuardFailure(f"comment {comment.comment_id} has invalid superseded role")
        expected_keys = {"block", "sha", "status", "role", "canonical_comment_id", "reason"}
        if role == "audit":
            expected_keys.add("reviewer")
        reviewer = fields.get("reviewer")
        allowed_statuses = {"PENDING", "PASS", "FAIL"} if role == "build" else {"PASS", "FAIL"}
        if fields.get("reason") != "equivalent-duplicate":
            raise GuardFailure(f"comment {comment.comment_id} has invalid superseded reason")
        if not fields.get("canonical_comment_id", "").isdigit():
            raise GuardFailure(f"comment {comment.comment_id} has invalid canonical ID")

    if set(fields) != expected_keys:
        raise GuardFailure(f"comment {comment.comment_id} has unknown or missing marker keys")
    block = fields.get("block", "")
    sha = fields.get("sha", "")
    status = fields.get("status", "")
    if not block or FULL_SHA.fullmatch(sha) is None or status not in allowed_statuses:
        raise GuardFailure(f"comment {comment.comment_id} has invalid marker values")
    if role == "audit" and not reviewer:
        raise GuardFailure(f"comment {comment.comment_id} has missing reviewer")
    return MarkerRecord(
        comment_id=comment.comment_id,
        role=None if kind == "superseded-v1" else role,
        kind=kind,
        block=block,
        sha=sha,
        status=status,
        reviewer=reviewer,
        payload=_normalize_lines(match.group("payload")),
        fields=tuple(sorted(fields.items())),
    )


def _parse_all_markers(comments: tuple[CommentSnapshot, ...]) -> tuple[MarkerRecord, ...]:
    markers: list[MarkerRecord] = []
    for comment in comments:
        marker = parse_marker(comment)
        if marker is not None:
            markers.append(marker)
    return tuple(markers)


def _resolve_markers(
    markers: tuple[MarkerRecord, ...],
    role: str,
    metadata: WorkBlockMetadata,
    head_sha: str,
) -> MarkerResolution:
    active = tuple(marker for marker in markers if marker.role == role)
    for marker in active:
        if marker.block != metadata.block:
            raise GuardFailure(f"{role} marker block differs from live Work Block")
        if marker.sha != head_sha:
            raise GuardFailure(f"{role} marker SHA differs from live PR head")
    if not active:
        return MarkerResolution(marker=None, duplicates=())
    signatures = {
        (marker.kind, marker.block, marker.sha, marker.status, marker.reviewer, marker.payload)
        for marker in active
    }
    if len(signatures) != 1:
        raise GuardFailure(f"non-equivalent active {role} markers")
    canonical = min(active, key=lambda marker: marker.comment_id)
    duplicates = tuple(
        marker.comment_id for marker in active if marker.comment_id != canonical.comment_id
    )
    return MarkerResolution(marker=canonical, duplicates=duplicates)


def _validate_target(snapshot: GuardSnapshot) -> WorkBlockMetadata:
    issue = snapshot.issue
    pr = snapshot.pull_request
    if snapshot.active_issue_count is not None and snapshot.active_issue_count != 1:
        raise GuardFailure("active Issue count is not exactly one")
    if snapshot.open_pr_count is not None and snapshot.open_pr_count != 1:
        raise GuardFailure("open PR count for expected branch is not exactly one")
    if issue.state != "OPEN" or "workflow:active" not in issue.labels:
        raise GuardFailure("active Issue is not open with workflow:active")
    if pr.state != "OPEN":
        raise GuardFailure("target PR is not open")
    metadata = parse_work_block(issue.body)
    if pr.head_branch != metadata.expected_branch:
        raise GuardFailure("PR head branch differs from expected branch")
    if pr.base_branch != metadata.base_ref.removeprefix("origin/"):
        raise GuardFailure("PR base branch differs from declared base")
    if pr.base_sha != metadata.base_sha:
        raise GuardFailure("PR base SHA differs from declared base")
    if FULL_SHA.fullmatch(pr.head_sha) is None:
        raise GuardFailure("PR head SHA is not complete")
    return metadata


def _validate_checks(snapshot: GuardSnapshot) -> None:
    required = tuple(check for check in snapshot.checks if check.name == "Python 3.12 quality")
    if not required:
        raise GuardFailure("required gate Python 3.12 quality is absent")
    if any(
        check.status not in {"completed", "success", "failure", "cancelled"} for check in required
    ):
        raise GuardFailure("required gate Python 3.12 quality is not terminal")
    if any(check.status != "completed" or check.conclusion != "success" for check in required):
        raise GuardFailure("required gate Python 3.12 quality is not PASS")


def _validate_finalize_live_evidence(snapshot: GuardSnapshot) -> None:
    if snapshot.source != "live":
        raise GuardFailure("finalize requires live acquisition")
    if snapshot.mergeable is not True:
        raise GuardFailure("PR mergeability is absent, unknown, or not mergeable")
    if snapshot.mergeability_state != "clean":
        raise GuardFailure("PR mergeability is not terminal clean")
    if snapshot.viewer_permission not in {"ADMIN", "MAINTAIN", "WRITE"}:
        raise GuardFailure("repository-scoped merge permission is absent or insufficient")
    if snapshot.base_protected is not True:
        raise GuardFailure("declared base branch protection is absent or unreadable")


def evaluate(snapshot: GuardSnapshot, phase: str = "finalize") -> GuardResult:
    empty = MarkerResolution(marker=None, duplicates=())
    metadata: WorkBlockMetadata | None = None
    build = empty
    audit = empty
    mutation_plan: tuple[str, ...] = ()
    try:
        if phase not in {"build", "audit", "finalize"}:
            raise GuardFailure("phase is unknown")
        metadata = _validate_target(snapshot)
        markers = _parse_all_markers(snapshot.comments)
        build = _resolve_markers(markers, "build", metadata, snapshot.pull_request.head_sha)
        audit = _resolve_markers(markers, "audit", metadata, snapshot.pull_request.head_sha)
        mutation_plan = tuple(
            f"supersede {role} comment {comment_id}"
            for role, resolution in (("build", build), ("audit", audit))
            for comment_id in resolution.duplicates
        )
        if phase == "build":
            return GuardResult("BUILD GUARD PASS", (), metadata, build, audit, mutation_plan)
        if build.marker is None or build.marker.status != "PASS":
            raise GuardFailure("BUILD PASS marker is absent or not PASS")
        if phase == "audit":
            _validate_checks(snapshot)
            if snapshot.smoke.status not in {"PASS"}:
                raise GuardFailure("smoke evidence is absent or not PASS")
            return GuardResult("AUDIT GUARD PASS", (), metadata, build, audit, mutation_plan)
        if audit.marker is None or audit.marker.status != "PASS":
            raise GuardFailure("AUDIT PASS marker is absent or not PASS")
        if build.duplicates or audit.duplicates:
            raise GuardFailure("marker reconciliation is pending")
        _validate_checks(snapshot)
        if snapshot.smoke.status != "PASS":
            raise GuardFailure("smoke evidence is absent or not PASS")
        if snapshot.requested_changes is None:
            raise GuardFailure("requested_changes evidence is absent")
        if snapshot.requested_changes:
            raise GuardFailure("requested changes remain")
        if snapshot.open_threads is None:
            raise GuardFailure("open thread evidence is absent")
        if snapshot.open_threads != 0:
            raise GuardFailure("open review threads remain")
        _validate_finalize_live_evidence(snapshot)
        decision = (
            "AWAITING HUMAN APPROVAL" if metadata.policy == "HUMAN" else "AUTO_FINALIZE_AUTHORIZED"
        )
        return GuardResult(decision, (), metadata, build, audit, mutation_plan)
    except GuardFailure as error:
        return GuardResult(
            "GUARD FAILURE",
            (str(error),),
            metadata,
            build,
            audit,
            mutation_plan,
        )


def _json_value(text: str, label: str) -> object:
    try:
        return cast(object, json.loads(text))
    except json.JSONDecodeError as error:
        raise GuardFailure(f"{label} is not valid JSON") from error


def _gh_json(arguments: Sequence[str], label: str) -> object:
    try:
        result = subprocess.run(
            ["gh", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GuardFailure(f"{label} could not be read") from error
    return _json_value(result.stdout, label)


def _gh_json_lines(arguments: Sequence[str], label: str) -> tuple[object, ...]:
    try:
        result = subprocess.run(
            ["gh", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GuardFailure(f"{label} could not be read") from error
    values: list[object] = []
    for line in result.stdout.splitlines():
        if line.strip():
            values.append(_json_value(line, label))
    return tuple(values)


def _repo_parts(repo: str) -> tuple[str, str]:
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise GuardFailure("repository must be owner/name")
    return owner, name


def _gh_graphql(
    query: str,
    variables: Mapping[str, str | int],
    label: str,
) -> object:
    arguments: list[str] = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        arguments.extend(["-F" if isinstance(value, int) else "-f", f"{key}={value}"])
    return _gh_json(arguments, label)


def _graphql_connection(
    repo: str,
    pr_number: int,
    connection_name: str,
    node_fields: str,
) -> tuple[Mapping[str, object], ...]:
    owner, name = _repo_parts(repo)
    query = (
        "query($owner: String!, $name: String!, $number: Int!, $cursor: String) {"
        " repository(owner: $owner, name: $name) {"
        " pullRequest(number: $number) {"
        f" {connection_name}(first: 100, after: $cursor) {{"
        f" nodes {{ {node_fields} }} pageInfo {{ hasNextPage endCursor }}"
        " } } } }"
    )
    cursor: str | None = None
    seen_cursors: set[str] = set()
    nodes: list[Mapping[str, object]] = []
    while True:
        variables: dict[str, str | int] = {
            "owner": owner,
            "name": name,
            "number": pr_number,
        }
        if cursor is not None:
            variables["cursor"] = cursor
        response = _mapping(_gh_graphql(query, variables, connection_name), connection_name)
        data = _mapping(response.get("data"), f"{connection_name}.data")
        repository = _mapping(data.get("repository"), f"{connection_name}.repository")
        pull_request = _mapping(repository.get("pullRequest"), f"{connection_name}.pull_request")
        connection = _mapping(pull_request.get(connection_name), connection_name)
        for node in _sequence(connection.get("nodes", ()), f"{connection_name}.nodes"):
            nodes.append(_mapping(node, f"{connection_name}.node"))
        page_info = _mapping(connection.get("pageInfo"), f"{connection_name}.page_info")
        has_next = _required_bool(page_info, ("hasNextPage",), f"{connection_name}.has_next")
        if not has_next:
            return tuple(nodes)
        next_cursor = _required_str(page_info, ("endCursor",), f"{connection_name}.end_cursor")
        if next_cursor in seen_cursors:
            raise GuardFailure(f"{connection_name} pagination cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _latest_reviews_requested_changes(reviews: Sequence[Mapping[str, object]]) -> bool:
    allowed_states = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}
    latest: dict[str, tuple[str, str, str]] = {}
    for review in reviews:
        author = _mapping(review.get("author"), "review author")
        reviewer = _required_str(author, ("login",), "review author login")
        state = _required_str(review, ("state",), "review state").upper()
        submitted_at = _required_str(review, ("submittedAt",), "review submitted_at")
        review_id = _required_str(review, ("id",), "review id")
        if state not in allowed_states:
            raise GuardFailure("review state is unknown")
        candidate = (submitted_at, review_id, state)
        current = latest.get(reviewer)
        if current is None or candidate[:2] > current[:2]:
            latest[reviewer] = candidate
    if any(state == "PENDING" for _, _, state in latest.values()):
        raise GuardFailure("latest review state is not terminal")
    return any(state == "CHANGES_REQUESTED" for _, _, state in latest.values())


def _open_review_threads(threads: Sequence[Mapping[str, object]]) -> int:
    open_threads = 0
    for thread in threads:
        if not _required_bool(thread, ("isResolved",), "review thread resolution"):
            open_threads += 1
    return open_threads


def _viewer_permission(repo: str) -> str:
    owner, name = _repo_parts(repo)
    query = (
        "query($owner: String!, $name: String!) {"
        " repository(owner: $owner, name: $name) { viewerPermission } }"
    )
    response = _mapping(
        _gh_graphql(query, {"owner": owner, "name": name}, "repository permission"),
        "repository permission",
    )
    data = _mapping(response.get("data"), "repository permission.data")
    repository = _mapping(data.get("repository"), "repository permission.repository")
    return _required_str(repository, ("viewerPermission",), "viewer permission").upper()


def _finalize_live_evidence(
    repo: str,
    pr_number: int,
    base_branch: str,
    pull_request: Mapping[str, object],
) -> tuple[bool, int, bool, str, str]:
    reviews = _graphql_connection(
        repo, pr_number, "reviews", "id state submittedAt author { login }"
    )
    threads = _graphql_connection(repo, pr_number, "reviewThreads", "isResolved")
    _mapping(
        _gh_json(["api", f"repos/{repo}/branches/{base_branch}/protection"], "branch protection"),
        "branch protection",
    )
    return (
        _latest_reviews_requested_changes(reviews),
        _open_review_threads(threads),
        _required_bool(pull_request, ("mergeable",), "pull_request.mergeable"),
        _required_str(pull_request, ("mergeable_state",), "pull_request.mergeable_state").lower(),
        _viewer_permission(repo),
    )


def snapshot_from_live(
    repo: str,
    issue_number: int,
    pr_number: int,
    smoke_status: str | None,
    phase: str,
) -> GuardSnapshot:
    issue = _gh_json(
        ["issue", "view", str(issue_number), "--repo", repo, "--json", "number,state,labels,body"],
        "issue",
    )
    issue_map = _mapping(issue, "issue")
    issue_candidates = _sequence(
        _gh_json(
            [
                "issue",
                "list",
                "--state",
                "open",
                "--label",
                "workflow:active",
                "--limit",
                "2",
                "--repo",
                repo,
                "--json",
                "number",
            ],
            "active Issues",
        ),
        "active Issues",
    )
    if len(issue_candidates) != 1 or (
        _required_int(issue_map, ("number",), "issue.number") != issue_number
    ):
        raise GuardFailure("active Issue target is not unique")
    expected_branch = parse_work_block(
        _required_str(issue_map, ("body",), "issue.body")
    ).expected_branch
    pr_candidates = _sequence(
        _gh_json(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--head",
                expected_branch,
                "--limit",
                "2",
                "--repo",
                repo,
                "--json",
                "number",
            ],
            "target PRs",
        ),
        "target PRs",
    )
    if len(pr_candidates) != 1:
        raise GuardFailure("target PR is not unique")
    candidate_map = _mapping(pr_candidates[0], "target PR")
    if _required_int(candidate_map, ("number",), "target PR number") != pr_number:
        raise GuardFailure("requested PR is not the expected branch target")
    pull_request_raw = _mapping(
        _gh_json(
            ["api", f"repos/{repo}/pulls/{pr_number}"],
            "pull request",
        ),
        "pull request",
    )
    head_raw = _mapping(pull_request_raw.get("head"), "pull request head")
    base_raw = _mapping(pull_request_raw.get("base"), "pull request base")
    pull_request: object = {
        "number": pull_request_raw.get("number"),
        "state": pull_request_raw.get("state"),
        "isDraft": pull_request_raw.get("draft"),
        "headRefName": head_raw.get("ref"),
        "headRefOid": head_raw.get("sha"),
        "baseRefName": base_raw.get("ref"),
        "baseRefOid": base_raw.get("sha"),
    }
    pull_request_map = _mapping(pull_request, "pull_request")
    issue_comments = _gh_json_lines(
        [
            "api",
            "--paginate",
            "--jq",
            ".[]",
            f"repos/{repo}/issues/{pr_number}/comments",
        ],
        "issue comments",
    )
    review_comments = _gh_json_lines(
        [
            "api",
            "--paginate",
            "--jq",
            ".[]",
            f"repos/{repo}/pulls/{pr_number}/comments",
        ],
        "review comments",
    )
    reviews = _gh_json_lines(
        [
            "api",
            "--paginate",
            "--jq",
            ".[]",
            f"repos/{repo}/pulls/{pr_number}/reviews",
        ],
        "reviews",
    )
    comments: list[object] = list(issue_comments) + list(review_comments)
    for review in reviews:
        review_map = _mapping(review, "review")
        body = review_map.get("body")
        review_id = review_map.get("id")
        if isinstance(body, str) and isinstance(review_id, int):
            comments.append({"id": review_id, "body": body})
    checks_root = _mapping(
        _gh_json(
            [
                "api",
                (
                    f"repos/{repo}/commits/"
                    f"{_required_str(pull_request_map, ('headRefOid',), 'head SHA')}/check-runs"
                ),
            ],
            "check runs",
        ),
        "check runs",
    )
    requested_changes: bool | None = None
    open_threads: int | None = None
    mergeable: bool | None = None
    mergeability_state: str | None = None
    viewer_permission: str | None = None
    base_protected: bool | None = None
    if phase == "finalize":
        (
            requested_changes,
            open_threads,
            mergeable,
            mergeability_state,
            viewer_permission,
        ) = _finalize_live_evidence(
            repo,
            pr_number,
            _required_str(base_raw, ("ref",), "base"),
            pull_request_raw,
        )
        base_protected = True
    snapshot = snapshot_from_json(
        {
            "issue": issue,
            "pull_request": pull_request,
            "comments": comments,
            "checks": checks_root.get("check_runs", ()),
            "smoke": {"status": smoke_status} if smoke_status is not None else None,
            "active_issue_count": len(issue_candidates),
            "open_pr_count": len(pr_candidates),
            "requested_changes": requested_changes,
            "open_threads": open_threads,
            "mergeable": mergeable,
            "mergeability_state": mergeability_state,
            "viewer_permission": viewer_permission,
            "base_protected": base_protected,
        }
    )
    return replace(snapshot, source="live")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Development Workflow guard")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--json", dest="json_path", type=Path, help="read a temporary JSON snapshot"
    )
    source.add_argument(
        "--live", action="store_true", help="read Issue, PR, comments and CI with gh"
    )
    parser.add_argument("--repo", help="GitHub repository owner/name for --live")
    parser.add_argument("--issue", type=int, help="active Work Block Issue number for --live")
    parser.add_argument("--pr", type=int, help="target PR number for --live")
    parser.add_argument("--phase", choices=("build", "audit", "finalize"), default="finalize")
    parser.add_argument("--smoke-status", choices=("PASS", "FAIL", "PENDING"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.json_path is not None:
            if args.phase == "finalize":
                raise GuardFailure("finalize phase is live-only")
            snapshot = snapshot_from_json(
                _json_value(args.json_path.read_text(encoding="utf-8"), "snapshot")
            )
        else:
            if not args.repo or args.issue is None or args.pr is None:
                raise GuardFailure("--live requires --repo, --issue and --pr")
            snapshot = snapshot_from_live(
                args.repo, args.issue, args.pr, args.smoke_status, args.phase
            )
        result = evaluate(snapshot, args.phase)
    except (GuardFailure, OSError) as error:
        result = GuardResult(
            "GUARD FAILURE",
            (str(error),),
            None,
            MarkerResolution(None, ()),
            MarkerResolution(None, ()),
            (),
        )
    print(json.dumps(result.as_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result.decision != "GUARD FAILURE" else 1


if __name__ == "__main__":
    sys.exit(main())
