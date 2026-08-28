# External Review Context

Type:
Architecture review / agent feedback

Source:
Claude Code external review

Authority:
Non-authoritative

Purpose:
Provide recommendations and observations for future planning.

Rules:

- Must be validated against repository sources.
- Must not override AGENTS.md, skills, protocol, or GitHub state.

I've completed the review. No files were modified; all work was read-only inspection plus `git show`/`git diff`.

One environment note up front, because it shapes finding G-9: **this host has no `.venv`** — `.venv/bin/python` does not exist. I did not create one (that would be environment mutation), so all findings below are from static reading, not execution.

---

# Independent Architecture & Financial Systems Audit

**Repo:** `investment-analyst` · **HEAD:** `3fe39c7` · **Uncommitted:** DEV-12 candidate (3 files)

---

## Part 1 — Governance & Agent Workflow

### G-1

**Severity:** CRITICAL
**Category:** governance
**Location:** `scripts/check_workflow_guards.py::parse_work_block` / `_validate_target`; absence of `.github/CODEOWNERS`

**Evidence:** The guard binds `finalize_policy=HUMAN` to exactly one condition: `profile == "CRITICAL"`. Nothing anywhere — guard, CI, or repo config — inspects _which files_ a Work Block changes. There is no `CODEOWNERS`, and no path-based classification rule. Consequently a Work Block that edits `AGENTS.md`, `docs/development_protocol.md`, `.agents/skills/*/SKILL.md`, or `scripts/check_workflow_guards.py` itself may be declared `Profile: FAST` / `finalize_policy: AUTO` and reach `FINALIZE → squash merge` with zero human in the loop. The currently uncommitted DEV-12 change is exactly this shape: it widens BUILD's own permission boundary, in the two documents that define BUILD's authority.

**Impact:** The agent system can amend the rules that constrain it, and merge that amendment autonomously. Every downstream guarantee in this audit (single writer, scope containment, read-only AUDIT, HUMAN gates) is conditional on documents the automation is permitted to rewrite without review. This is the root governance risk; most other governance findings are reachable through it.

**Recommendation:** Make the governance surface a mechanically-recognized protected path set. Two independent controls, not one: (a) a repo-side control that cannot be edited by the same PR it governs — `CODEOWNERS` plus branch protection requiring owner review on those paths; (b) a guard-side rule that derives the _required_ policy from the changed-file set and fails closed when a declared `AUTO` block touches it. `DEV-3 conserva HUMAN` is currently prose; it needs to be a computed property of the diff.

---

### G-2

**Severity:** HIGH
**Category:** governance / security
**Location:** `scripts/check_workflow_guards.py::_validate_checks`; `.github/workflows/ci.yml`

**Evidence:** `_validate_checks` selects check runs by the literal string `"Python 3.12 quality"` and asserts `status == "completed" and conclusion == "success"`. It verifies the gate's _name and outcome_. It never verifies the gate's _content_. `.github/workflows/ci.yml` is not in any protected path set (see G-1). Renaming the job would fail closed correctly — but _hollowing it out_ while keeping the job name would not: replacing the pytest/ruff/pip-audit steps with a no-op yields a green check run named `Python 3.12 quality`, and the guard reports the deterministic suite as authoritative and passing.

**Impact:** The single technical gate the protocol calls "la autoridad de la suite determinista" can be disabled by the automation it gates, inside one AUTO Work Block, without any signal to the guard, AUDIT, or FINALIZE.

**Recommendation:** Treat the workflow definition as governance surface (G-1), and additionally bind the gate to content, not identity — e.g. require the merge-base and head versions of `.github/workflows/ci.yml` to be byte-identical unless the block explicitly declares a CI capability, and surface that comparison in the guard's JSON.

---

### G-3

**Severity:** HIGH
**Category:** governance
**Location:** `scripts/check_workflow_guards.py::_parser` (`--smoke-status`), `snapshot_from_live`, `evaluate` (finalize branch)

**Evidence:** Smoke evidence enters the guard as a command-line flag with `choices=("PASS","FAIL","PENDING")`, supplied by the same agent whose work is being gated. In live mode it is injected verbatim: `"smoke": {"status": smoke_status}`. `SmokeSnapshot.evidence` exists but live acquisition always sets it to `""`, and nothing validates it. The guard then enforces `snapshot.smoke.status == "PASS"` for both `audit` and `finalize`.

The protocol states FINALIZE is "estrictamente live-only" and requires "smoke del mismo SHA". Neither holds: smoke is not acquired live, and it carries no SHA, command, timestamp, or artifact binding whatsoever. The existing guard tests (`tests/unit/test_workflow_guard.py:413`) only assert that an _absent_ smoke fails — they do not, and cannot, test that an asserted `PASS` corresponds to anything.

**Impact:** The real-smoke gate — the protocol's only defense against a skill/discoverability regression that unit tests cannot catch — is satisfied by typing five characters. A FAST/AUTO block can self-merge having never run the smoke.

**Recommendation:** Smoke must become acquired evidence, not a parameter: a machine-readable artifact bound to the full SHA, written by the smoke run, read by the guard, and rejected when its SHA ≠ live head. Until then, do not describe FINALIZE as fully live-only.

---

### G-4

**Severity:** HIGH
**Category:** governance
**Location:** `scripts/check_workflow_guards.py::main` (`--json` accepted for `phase in {build, audit}`)

**Evidence:** Only `finalize` rejects `--json`. For `phase=audit`, the entire snapshot — Issue body and labels, PR head/base SHAs, all comments (hence all markers), check-run conclusions, `active_issue_count`, `open_pr_count` — is read from a JSON file the invoking agent authors. `AUDIT GUARD PASS` therefore proves nothing about the live repository when a snapshot is used, yet `.agents/skills/audit/SKILL.md` step 1 treats running the guard as the fail-closed target resolution, and the protocol calls the CLI "read-only, estructural y fail-closed".

It is fail-closed with respect to _its input_. Its input is untrusted.

**Impact:** AUDIT can obtain a structurally valid `AUDIT GUARD PASS` against fabricated state and then publish a genuine `status=PASS` marker to the real PR. FINALIZE's live guard reads that real marker and, with real green CI plus an asserted smoke (G-3), authorizes the merge. The chain from fabricated audit input to real merge is unbroken.

**Recommendation:** Make `--live` the only mode that can produce a `PASS`-shaped decision in any phase. Keep `--json` strictly as an offline diagnostic that emits a decision explicitly labeled non-authoritative, and make the skills refuse to treat a `source: "json"` result as a gate.

---

### G-5

**Severity:** HIGH
**Category:** governance
**Location:** whole workflow — `scripts/check_workflow_guards.py`; `.agents/skills/build/SKILL.md` §4; `docs/development_protocol.md` §"Scope estima tamaño"

**Evidence:** The protocol's scope model rests on three mechanisms: strict allowlists for R3/sensitive surfaces, "semantic scope" for everything else, and SHA-256 verification of protected worktree bytes. **None is executable.** The guard validates Work Block metadata, markers, target resolution and CI — it never reads the diff, never enumerates changed files, and never computes a hash. `tests/unit/test_development_workflow_contract.py::_protected_hash_decision` (line 264) is a helper defined inside the test file that hashes a `tmp_path` fixture; it exercises `hashlib`, not the workflow.

Scope containment is therefore enforced entirely by an LLM voluntarily complying with prose, and verified entirely by a second LLM voluntarily reading a diff.

**Impact:** Directly answers audit objective 2: the workflow **does not prevent** unauthorized scope expansion, accidental governance changes, or mixing functional work with workflow changes. It documents them as prohibited and relies on good-faith compliance. Traceability (markers, exact-SHA binding, marker reconciliation) is the one dimension that _is_ genuinely well-engineered and mechanically enforced.

**Recommendation:** Add a scope guard phase that materializes `base...head`, enumerates changed paths, and evaluates them against the Work Block's declared allowlist / protected / forbidden sets, plus a hash verification of the declared protected set read from worktree bytes. This is the missing half of the guard, and it is the half that makes the other half meaningful.

---

### G-6

**Severity:** MEDIUM
**Category:** testing / governance
**Location:** `tests/unit/test_development_workflow_contract.py` (789 lines)

**Evidence:** This file is presented as the workflow contract test. Structurally it is two things, neither of which tests the system:

1. **String-presence assertions** over `docs/development_protocol.md` and the SKILL files (~90 asserts of the form `assert "<Spanish sentence>" in protocol`). These verify that prose exists, not that behavior matches it.
2. **Reimplementations of the protocol inside the test file**, then tested against themselves: `_build_transition`, `_audit_decision`, `_finalize_decision`, `_temporal_supersession_decision`, `_replan_decision`, `_ui_worker_preflight`, `_role_permissions`. None imports production code. The clearest case is `_role_permissions` (line 660), which does `del model_metadata; return {…}` — and `test_role_permissions_are_explicit_and_complete` asserts it equals that same literal dict.

`.agents/rules/investment-analyst-core.md` explicitly forbids reimplementing the guard's parser "en la skill, regla o tests" — this file does exactly that for the surrounding decision logic.

**Impact:** The suite produces high confidence and near-zero assurance. It will stay green through any behavioral regression in BUILD/AUDIT/FINALIZE, because it never touches them. It also creates a real hazard: a future change to the _real_ logic that contradicts these in-test models produces no failure.

**Recommendation:** Either drive the decision tables from the production module that implements them, or delete the reimplementations and be explicit that the protocol's decision logic has no automated coverage. The current form is worse than either, because it reads as coverage.

---

### G-7

**Severity:** MEDIUM
**Category:** architecture / testing
**Location:** `.github/workflows/ci.yml`; `pyproject.toml [tool.ruff.lint] select`

**Evidence:** `AGENTS.md` line 71: _"Use typed domain models. Do not use `Any` in production code."_ CI runs Ruff (`E,F,I,UP,B,SIM`), pytest+coverage, and pip-audit. There is **no type checker** — no mypy, no pyright — and `ANN` (flake8-annotations) is not selected. `.gitignore` lists `.mypy_cache/`, so it was anticipated and never adopted. `S` is also unselected, while `src/investment_analyst/storage/raw_records.py:137,201,222` carry `# noqa: S608` suppressions for a rule that is not enabled.

**Impact:** A stated architectural invariant has no enforcement. See F-6 for the concrete consequence in the most recently merged Work Block.

**Recommendation:** Add a type-check step to the `Python 3.12 quality` gate (strict on `src/`), or remove the invariant from `AGENTS.md`. An unenforced invariant that AUDIT is expected to check by eye is a recurring source of the exact drift observed.

---

### G-8 — DEV-12 review: permission boundary

**Severity:** MEDIUM
**Category:** governance
**Location:** `docs/development_protocol.md` §"Capability envelope y preflight BUILD" (+4 lines); `.agents/skills/build/SKILL.md` step 3

**Evidence:** The new text reads: _"Un clean-host bootstrap también es default: BUILD puede provisionar automáticamente un entorno efímero aislado desde el lock existente, sin resolver, actualizar ni escribir dependencias versionadas."_

Answering the three questions posed:

**(a) Is bootstrapping from existing lock files safe?** In principle yes — `uv sync --locked` is non-resolving and fails if `uv.lock` is stale, which matches the stated intent, and CI already proves the lock is coherent (`uv lock --check`). The _concept_ is sound.

**(b) Is the permission boundary correctly defined?** No. The boundary is stated as an outcome ("efímero", "aislado", "sin resolver") with no mechanism binding it. The text names no command, no flag, and no path. `uv sync` without `--locked`, or `uv venv && uv pip install -e '.[dev]'`, both satisfy an ordinary reading of "provisionar un entorno desde el lock existente" and both re-resolve. Nothing defines where the environment lives.

**(c) Could it allow dependency drift or environment mutation?** Yes, on two distinct paths:

- **Worktree mutation invisible to every existing control.** `AGENTS.md` line 209 establishes `.venv` _inside the repository_ as the environment. `.venv/` is gitignored, so it never appears in `git status --short`, never appears in a diff, and is not part of any protected-hash set. If a `.venv` already exists, `uv sync` **prunes packages absent from the lock** — mutating the user's primary development environment. On this host `.venv` is currently absent, which is precisely the DEV-12 trigger; the same code path on a host where it exists is destructive. "Efímero aislado" is asserted, never constrained.
- **Network reclassification.** A clean-host bootstrap downloads dozens of packages from PyPI. `UV_PYTHON_DOWNLOADS: never` is set in CI but is not stated for the local bootstrap, so an interpreter download is also permitted by the text. This moves unattended external network fetch-and-install from the declared-capability list into defaults, with no integrity gate stated (no `uv lock --check` precondition, no hash requirement).

**Impact:** BUILD gains an unattended, unlogged, unbounded-location environment provisioning permission whose only limit is prose the agent interprets. The realistic failure is silent destruction of the user's working `.venv` mid-block, with no evidence trail — the class of loss `AGENTS.md` §"Working-tree and Git safety" is written to prevent.

**Recommendation:** Do not merge the boundary as prose. Make it mechanical and narrow: name the exact non-resolving command; require the environment path to be outside the worktree (e.g. an explicit `UV_PROJECT_ENVIRONMENT` under a scratch root) and forbid targeting `.venv` in the repository; require `uv lock --check` to pass first; require the bootstrap to be recorded in the BUILD marker as a capability actually used. Then classify the block itself as governance surface per G-1 — it changes BUILD's authority and must not self-merge under AUTO.

---

### G-9 — DEV-12 review: test additions

**Severity:** MEDIUM
**Category:** testing
**Location:** `tests/unit/test_development_workflow_contract.py:97-98,118-119`

**Evidence:** The block's entire test delta is four string-presence assertions:

```python
assert "Un clean-host bootstrap también es default" in protocol
assert "entorno efímero aislado desde el lock existente" in protocol
assert "clean-host bootstrap puede provisionar automáticamente" in skills["build"]
assert "entorno efímero aislado desde el lock existente" in skills["build"]
```

There is no negative test. Nothing asserts that lock mutation is rejected, that the environment path must lie outside the worktree, or that a resolving command is refused. These assertions pass unchanged if the surrounding paragraph is later rewritten to grant a far broader permission, provided the two sentences survive.

**Impact:** A permission-boundary change ships with coverage that verifies a sentence exists. Under `AGENTS.md` §"Review guidelines" ("Treat silent changes to … provider scope … as high-risk") and the AUDIT criterion "acceptance o invariante crítica no demostrada", this delta should not satisfy an AUDIT PASS.

**Recommendation:** Whatever mechanical constraint results from G-8 should carry the acceptance and negative tests. If the boundary stays prose-only, the honest position is that it has no coverage — not four substring checks.

---

### G-10

**Severity:** LOW
**Category:** governance
**Location:** `scripts/check_workflow_guards.py::_finalize_live_evidence`, `_validate_finalize_live_evidence`, `_validate_checks`

Three narrower weaknesses in an otherwise carefully built guard:

- `base_protected` is set to a hardcoded `True` after `GET /branches/{base}/protection` merely returns an object. Existence of a protection record is not evidence of _any_ rule; a protection object permitting force-push with no required reviews satisfies this check. The protocol claims "protección vigente de `main`".
- Check runs are fetched without `--paginate` (default 30). Fail-closed if the gate is absent, but if a stale successful run appears on page 1 and a later failed re-run of the same name does not, the guard passes on an outdated conclusion. Low likelihood in a one-job repo; the pagination discipline applied to `reviews`/`reviewThreads` was simply not applied here.
- `_latest_reviews_requested_changes` breaks `submittedAt` ties on `review_id`, a base64 node ID whose lexicographic order is not chronological. Rare, and it only affects exact-timestamp ties.

**Recommendation:** Assert the specific protection properties FINALIZE actually depends on; paginate check runs; tie-break on a monotonic field.

---

## Part 2 — Financial & Point-in-Time Correctness

### F-1

**Severity:** CRITICAL
**Category:** financial
**Location:** `src/investment_analyst/evidence/sec_documents/models.py::SecDocumentRevision.validate_identity` (line 139) vs `src/investment_analyst/providers/fundamentals/sec_companyfacts_normalizer.py:313`

**Evidence:** Two mutually contradictory definitions of `available_at` coexist inside the same SEC issuer pipeline.

The XBRL fundamentals path uses true publication time:

```python
available_at=fact.acceptance_at      # sec_companyfacts_normalizer.py:313
```

The document and ownership paths use **local download time**, and enforce it as an invariant:

```python
if self.available_at != self.retrieved_at:
    raise ValueError("available_at must equal the first demonstrated retrieval time")
```

`OwnershipStatement` then inherits it (`statement.available_at != document_revision.available_at → ValueError`), and the pipeline sets `available_at=resolved.semantic.retrieved_at` (`sec_ownership_pipeline.py:145,233`). `SecFiling.accepted_at` — the real EDGAR acceptance timestamp — is parsed, carried, and deliberately not used for availability.

Concretely: a Form 4 accepted by EDGAR on 2024-03-01 and imported on 2026-08-27 carries `available_at = 2026-08-27`. `OwnershipService.query(known_at=2025-01-01)` returns nothing. A 10-K's XBRL facts for the same filing _are_ returned, because they carry `acceptance_at`.

`AGENTS.md` line 78 states: _"Preserve point-in-time behavior using `available_at`; `computed_at` is not a substitute for availability."_ The document layer substitutes retrieval time for availability, which is the same class of error.

**Impact:**

- **Backfilled history is permanently invisible to any historical cut.** Every document and ownership statement is stamped with import time, so no `known_at` earlier than the import date can ever see them.
- **Deterministic replay breaks across environments.** Two workspaces that imported the same corpus on different days return different answers for the identical `known_at`. This defeats the stated replay invariant at its foundation.
- **Silent cross-layer inconsistency.** A consolidated point-in-time view at a historical cut shows a filing's _numbers_ but not its _document_ or the _insider transactions_ from the same accession — with no error, because both layers believe they are correct.

**Recommendation:** This is a semantic decision, not a bug fix, and it needs an explicit ruling before any code moves. If `available_at` means "publicly available" — the reading `AGENTS.md`, the PIT service, and the fundamentals path all assume — then documents and ownership must derive it from `filing.accepted_at`, and local receipt belongs in `retrieved_at`/`received_at`, which already exist and are already persisted. If instead a "first locally demonstrated" notion is genuinely wanted, it needs its own field name and must not occupy `available_at`. Note the migration weight: `revision_id` is `uuid5(document_id | content_sha256 | schema_version)` and is _not_ derived from `available_at`, so identities survive a correction — but existing `raw_record_index.available_at` values do not, and `RawRecord` is append-only. Scope this as a CRITICAL Work Block with HUMAN policy and an explicit migration design.

---

### F-2

**Severity:** HIGH
**Category:** financial / architecture
**Location:** `src/investment_analyst/core/models/base.py::ContractModel`

**Evidence:**

```python
model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)
```

Two gaps, both at the layer every other layer funnels through:

- **Not frozen.** `RawRecord`, `NormalizedObservation`, `MetricResult`, `DiagnosticResult` are mutable. `validate_assignment=True` re-validates a mutation; it does not prevent one. Meanwhile ~40 downstream models across `analytics/`, `application/`, `workspace/`, `evidence/` are explicitly `frozen=True` — including the SEC evidence models, which had to define their own `_FrozenContract`/`_Strict` subclasses to get it. The immutability discipline is real everywhere _except_ the core evidence contracts.
- **Not strict, no float rejection.** In Pydantic's default lax mode, `Decimal` fields accept `float`. `NormalizedObservation.value`, `MetricResult.value`, `DiagnosticResult.final_score`, `DiagnosticComponent.score`/`weight` have no float guard. The codebase clearly knows the risk — `_reject_float` / `_reject_decimal_float` / `_reject_financial_float` validators appear in at least nine peripheral modules, and providers decode JSON with `parse_float=str`. The defense is applied at the edges and omitted at the centre.

**Impact:** Append-only immutability and Decimal exactness are enforced by convention and by peripheral models, not by the contract that defines evidence. A caller that mutates a `RawRecord` between construction and `save()` changes its canonical JSON and checksum silently; a caller that passes a computed `float` into `MetricResult.value` introduces binary rounding into an exactness-critical field with no error. Both are one careless line away, in the layer where they matter most.

**Recommendation:** Move `frozen=True` and float rejection into `ContractModel` itself, so the periphery inherits rather than re-declares. Expect fallout at construction sites that currently mutate; that fallout is the finding.

---

### F-3

**Severity:** MEDIUM
**Category:** financial
**Location:** `src/investment_analyst/evidence/sec_ownership/service.py::OwnershipService.query` (final line)

**Evidence:** `OwnershipRepository.list` returns statements sorted **ascending** by `(available_at, accession, statement_id)`. `query` then applies `return tuple(result[: query.limit])` — the head of an ascending sort, with `limit` defaulting to 100.

A point-in-time insider-ownership query therefore returns the **oldest** 100 statements at or before `known_at`, not the most recent. The analogous fundamentals service does the opposite and takes the tail (`sec_point_in_time_service.py:136`: `periods = periods[-request.limit:]`).

**Impact:** For any issuer with more than `limit` Section 16 filings — routine for a large-cap — the query silently answers a different question than the analyst asked, with no truncation signal in the result. There is no `total_matched` or `truncated` field to make the loss visible. This is exactly the "missing != zero" hazard applied to recency: absent recent insider activity reads as no recent insider activity.

**Recommendation:** Decide and document the intended ordering; near-certainly the most recent statements at or before `known_at`. Whichever is chosen, return an explicit truncation indicator so a capped result is never mistaken for a complete one.

---

### F-4

**Severity:** MEDIUM
**Category:** financial
**Location:** `src/investment_analyst/evidence/sec_ownership/models.py::OwnershipResolutionOutcome.validate_identity_and_time` vs `OwnershipStatement.identity_and_lineage`

**Evidence:** Within one `source_id` (`sec-edgar:section16-ownership`), two record kinds define availability differently: outcomes enforce `available_at == retrieved_at`; statements enforce `available_at == document_revision.available_at`. Today these coincide only because F-1 makes the revision's availability also equal retrieval time. Correcting F-1 decouples them and leaves the same `source_id` carrying two availability semantics.

Additionally, `outcome_id = uuid5(accession | resource_name | content_sha256 | status)` — deliberately independent of time — while the record's content embeds `retrieved_at`. `_save_outcome` guards this by returning early when `get_outcome` finds an existing outcome (`sec_ownership_pipeline.py:207-209`), so re-import is idempotent; but `OwnershipRepository.save_outcome`'s own `existing != outcome → conflict` path would fire on any caller that skips that check.

**Impact:** Mixed availability semantics inside one source make PIT filtering over that source ambiguous, and the ambiguity is currently masked by a defect. It will surface as a regression the moment F-1 is fixed.

**Recommendation:** Resolve alongside F-1: decide whether a resolution _outcome_ is evidence about the source (availability = filing acceptance) or evidence about our acquisition (a separate, explicitly-named field). Do not leave both meanings on `available_at` under one `source_id`.

---

### F-5

**Severity:** MEDIUM
**Category:** financial / runtime
**Location:** `src/investment_analyst/providers/ownership/sec_ownership_pipeline.py:154`

**Evidence:** `parse_ownership_statement(..., parsed_at=datetime.now(UTC))` — an unmediated wall-clock read inside pipeline execution, rather than an injected clock. `parsed_at` becomes the statement's `received_at` and the raw record's `source.retrieved_at`, so it is persisted provenance. `AGENTS.md` §"Review guidelines" requires _"tests use different clocks when validating distinct executions; do not hide time-dependent bugs by reusing one clock"_ — which presumes an injectable clock at these sites.

**Impact:** Persisted timestamps cannot be controlled from a test, so temporal edge cases in this path are untestable, and two executions cannot be distinguished deterministically. This is the hidden-state pattern the invariant list is written against.

**Recommendation:** Thread the clock through the pipeline as the surrounding code does elsewhere, so `parsed_at` is supplied by the caller.

---

### F-6

**Severity:** MEDIUM
**Category:** architecture
**Location:** `src/investment_analyst/evidence/sec_ownership/repository.py:24,110,143`; `service.py:11,13`; `providers/ownership/sec_ownership_pipeline.py:167,183,198-206`; `sec_ownership_parser.py:210,218`

**Evidence:** The most recently merged Work Block (SEC-CORPUS-2, `abb9a1e`) shipped production code with fully unannotated signatures — implicit `Any` on every parameter:

```python
def verify_ownership_records(records, document_repository, content_store) -> None:
class OwnershipRepository:
    def __init__(self, raw_records) -> None:
class OwnershipService:
    def __init__(self, storage, *, configuration) -> None:
    def query(self, query: OwnershipQuery):        # no return type
def _save_outcome(self, repository, filing, discovery_raw_record_id,
                  declared_locator, resource_name, response, manifest,
                  *, status=None, reason_code=None) -> OwnershipResolutionOutcome:
def _decimal(value): ...
def _date(value): ...
```

This directly violates `AGENTS.md` line 71, in a repository that is otherwise rigorously typed. It passed BUILD, passed AUDIT, passed CI, and merged — because no tool checks it (G-7) and AUDIT's check of this invariant is manual.

**Impact:** Beyond the invariant breach, this is the most useful _empirical_ data point in the audit: it demonstrates that the PLAN→BUILD→AUDIT→HUMAN chain, operating normally, merged a stated-invariant violation into `main`. Governance findings G-5 and G-7 are not hypothetical.

**Recommendation:** Annotate these signatures (Protocols for the repository/store collaborators), and add the type-check gate from G-7 so the class of drift is caught mechanically rather than by attention.

---

### F-7

**Severity:** LOW
**Category:** financial
**Location:** `src/investment_analyst/providers/fundamentals/sec_point_in_time_service.py:128`

**Evidence:** Candidate selection filters `quality=DataQuality.VALID`, and `observations_examined` counts only what survives that filter. `DELAYED`, `PARTIAL`, and `SUSPECT` observations are excluded with no trace in the result. A period can therefore report `is_complete=True` while a non-VALID revision for a missing field exists in storage.

**Impact:** The filtering is conservative and correct in direction, but the audit trail does not disclose it. An analyst cannot distinguish "no data" from "data suppressed by quality" — a soft "missing != zero" violation at the reporting boundary.

**Recommendation:** Report the quality-excluded count alongside `observations_examined`.

---

### No material issue identified — PIT selection core

`SecIssuerFundamentalPointInTimeService` (`_resolve_revisions`, `_collapse_equal_availability`, `_build_periods`, `_verify_result`) is the strongest code I read. Revision selection is strictly by `available_at`; semantically different revisions at identical availability raise `AmbiguousSecFundamentalRevisionError` rather than being picked arbitrarily — exactly the behavior `AGENTS.md` §"Review guidelines" demands; `missing_fields` are explicit; and the result is independently re-verified for traceability and PIT safety after construction. Likewise `JsonRawRecordRepository._verify_file` (checksum + index/file byte agreement on every read), `write_local_service_unit` (O_EXCL + fsync + directory fsync), the `_FORBIDDEN` DOCTYPE/ENTITY rejection before `ElementTree.fromstring`, and the exact `Decimal(str)` parsing with `is_finite()` rejection in `_decimal`. These are done well and should not be disturbed by remediation of the findings above.

---

## Part 3 — Runtime & Lifecycle

### R-1

**Severity:** HIGH
**Category:** runtime
**Location:** `src/investment_analyst/application/multi_asset_scheduler.py::MultiAssetScheduleStateStore.write_attempt_from_state` and `MultiAssetScheduler.tick` / `_run_job` (introduced by `4f87069`)

**Evidence:** The commit replaced a fresh `self._store.load()` before each write with an in-memory `state` snapshot threaded through the whole tick. Its docstring states the justification: _"The scheduler is the sole writer during a tick."_

That assumption is asserted, never enforced. `MultiAssetScheduleStateStore` synchronizes with `threading.RLock()` only — no `fcntl.flock`. This is notable because **the three sibling state stores in this same codebase all use `flock`**: `application/operational_state.py:37`, `application/aapl_scheduler.py:55`, `workspace/backup.py:402`. The multi-asset store is the sole exception, and the one that just had its stale-read window widened.

The window is now large. `_run_job` writes the `RUNNING` attempt, executes the job — an arbitrarily long refresh — then writes the terminal attempt from the _same_ snapshot captured before execution. `_write` replaces the entire file. Any attempt appended by another process during that span is overwritten.

**Impact:** Silent lost updates in an append-only history: a concurrent CLI invocation, a manual operations script, or two schedulers briefly overlapping during a release restart (see R-2) will have their recorded attempts erased with no error. This is an append-only-evidence violation reached through a performance optimization, and it fails silently — the worst combination for auditability. The commit's own message frames it as `perf`; the correctness precondition it introduces was neither enforced nor tested.

**Recommendation:** Enforce the single-writer premise the optimization now depends on — adopt the `fcntl.flock` pattern already used by the three sibling stores, held for the tick, with a fail-closed error if the lock is unavailable. Do not rely on a docstring for a cross-process invariant.

---

### R-2

**Severity:** MEDIUM
**Category:** runtime
**Location:** `src/investment_analyst/application/local_release.py::activate` (state write at line ~1805) and `_save_deployment_state` (line 1529)

**Evidence:** Two coupled weaknesses in the activation ordering:

- **Durability asymmetry.** `write_local_service_unit` is fully durable (`O_EXCL`, `fsync` on the file, `fsync` on the parent directory). `_save_deployment_state` does `write_text` + `replace` with **no fsync** on either. The unit file — which determines what systemd actually starts — is durable; the record of what was deployed is not.
- **Ordering.** `activate` retargets the unit, reloads, restarts, health-checks, and only then writes `DeploymentState`. If the process dies after the restart but before/during the state write — or the state write fails, e.g. `init_runtime()` raising or the disk filling — the new release is live and serving while `deployment_state.json` still names the old SHA.

`_validate_current_deployment` then detects the divergence on the next operation and raises `"CRITICAL: deployment state, manifest, and live unit disagree"`, which correctly fails closed but blocks both `activate` and `rollback` until a human reconciles by hand. Worse, a `rollback()` issued in that state computes its target from the stale `previous`.

**Impact:** A crash or full disk during activation leaves the deployment in a state that is un-rollbackable by the tool and requires manual repair — precisely when automated recovery matters most.

**Recommendation:** Give `_save_deployment_state` the same fsync discipline as `write_local_service_unit`, and record an intent marker before the restart so a crashed activation is recoverable from state rather than requiring hand reconciliation. Also add the `try/finally` temp-file cleanup the unit writer has and this one lacks.

---

### R-3

**Severity:** MEDIUM
**Category:** runtime
**Location:** `src/investment_analyst/application/multi_asset_scheduler.py:24,463` (`_MAX_ATTEMPTS_RETAINED = 100_000`) and `_write`

**Evidence:** `attempts` is capped by `Field(max_length=100_000)` with **no rotation, compaction, or archival path**. On reaching the cap, `MultiAssetScheduleState(attempts=tuple(attempts))` inside `write_attempt_from_state` raises `ValidationError`, uncaught (`_write`'s `except OSError` does not cover it). Every subsequent write fails, permanently, with no recovery path in code.

Separately, `_write` serializes and rewrites the _entire_ history on every attempt transition — two writes per job run — giving O(n) per write and O(n²) growth over the deployment's life. This is almost certainly the pressure `4f87069` was responding to; that commit removed the redundant _parse_ but left the full rewrite (and introduced R-1 doing so).

**Impact:** A hard availability cliff at 100k attempts, plus steadily degrading tick latency well before it. For a multi-asset scheduler running frequent jobs this is a matter of runtime, not a theoretical bound.

**Recommendation:** Address the storage shape rather than the read path: segment or roll the history (retaining an archive, since this is audit evidence and must not simply be truncated), so writes are bounded and the cap is not a terminal failure.

---

### R-4

**Severity:** MEDIUM
**Category:** runtime
**Location:** `src/investment_analyst/application/local_release.py::rollback` (line 1857) vs `activate` (line 1715)

**Evidence:** `activate` gates on `verify_pre_restart(...)` — an explicit fail-closed pre-flight. `rollback` performs no equivalent call. It validates the previous release's `manifest.json` (commit SHA, resolved release path, presence of `.venv/bin/python` and `scripts/serve_investment_analyst.py`), which is meaningful but is a strictly narrower check than `verify_pre_restart`.

Additionally, `rollback` sets `previous = current_state.current`, so `previous` is a single alternating slot rather than a history: two consecutive rollbacks return to the starting SHA. That may be intended, but it means there is no path back past one step.

**Impact:** The recovery path — invoked precisely when the system is already in a degraded state — is verified less strictly than the forward path. Whatever `verify_pre_restart` protects against is unguarded during rollback.

**Recommendation:** Apply the same pre-flight to the rollback target, or document explicitly which of its checks are deliberately skipped and why.

---

### R-5

**Severity:** LOW
**Category:** security / architecture
**Location:** `src/investment_analyst/providers/http.py::UrlLibHttpTransport._request` (line ~172)

**Evidence:** The scheme check `if urlsplit(url).scheme.lower() != "https"` applies to the _initial_ URL only. `urlopen` follows redirects via the default `HTTPRedirectHandler`, which permits `https` → `http` and cross-host redirects. No custom opener restricts either. The final URL is then recorded as provenance — `raw_uri` on `SourceReference`, `source_url` on `SecDocumentRevision`, `resource_url` on `OwnershipResolutionOutcome`.

**Impact:** Low in practice (`data.sec.gov` over TLS), but two invariants are weaker than they read: transport confidentiality is not guaranteed end-to-end, and "official or explicitly identified data sources" is enforced on the requested URL rather than the served one. Content integrity is separately protected by SHA-256, which limits the blast radius considerably.

**Recommendation:** Install an opener that rejects scheme downgrade and constrains redirects to the expected host, and assert that the recorded final URL matches the requested origin before persisting it as provenance.

---

## Summary against the stated objectives

**1. Architectural risks.** The layer separation (`evidence → analytics → signal → …`) is genuinely respected in code, and the PIT selection core, raw-record integrity, and XML hardening are well built. The structural risk is that the _invariants_ are enforced unevenly: rigorously in the peripheral models and the fundamentals query path, not at all in `ContractModel` (F-2) or by any tool in CI (G-7).

**2. Does the agent workflow prevent the four named failures?**

|                                   | Prevented?                                                                                                                     |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Unauthorized scope expansion      | **No** — no executable scope or protected-hash check exists (G-5)                                                              |
| Accidental governance changes     | **No** — governance surface is self-amendable under AUTO, no CODEOWNERS (G-1, G-2)                                             |
| Mixing functional + workflow work | **No** — nothing inspects the changed-file set (G-1, G-5)                                                                      |
| Loss of traceability              | **Yes** — marker lifecycle, exact-SHA binding, archive-first, and duplicate reconciliation are rigorous and genuinely enforced |

Traceability is the system's real achievement. Authority containment is documentation. The empirical proof is F-6: a stated-invariant violation traversed the full chain into `main` last week.

**3. DEV-12.** Concept sound, boundary not correctly defined, and it _can_ permit environment mutation — including destructive pruning of the user's in-repo `.venv`, invisible to `git status` and to every protected-work control (G-8). Its tests verify sentences, not behavior (G-9). It should not merge as-is, and specifically should not merge under AUTO, since it widens BUILD's own authority.

**4. Critical financial risks.** F-1 is the one to act on first: `available_at` means "publicly available" in the fundamentals path and "when we downloaded it" in the document and ownership paths. That single split breaks historical PIT reconstruction and cross-environment deterministic replay for the entire SEC document corpus, silently, and it is now baked into a model validator and the freshly merged SEC-CORPUS-2 data.

**5. Runtime risks.** R-1 is the sharpest: a `perf` commit converted a re-read-before-write into a tick-long cached snapshot on the only state store in the codebase lacking cross-process locking, creating silent lost-update potential in append-only history.

**Suggested order:** F-1 (PIT semantics ruling + migration design, HUMAN policy) → G-1/G-2 (protect the governance and CI surface before anything else self-merges) → R-1 (flock the scheduler store) → G-3/G-4 (make smoke and audit evidence real) → G-5 (scope guard) → F-2/G-7 (harden the core contract, add the type gate) → DEV-12 reworked per G-8.

I made no changes to the repository, created no branches, and ran no mutating commands. Want me to write this up as a shareable page as well?
