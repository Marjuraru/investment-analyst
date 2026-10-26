# Continuous integration

The `CI` GitHub Actions workflow validates the repository with the same static and test checks used
locally. It runs for every pull request, every push to `main`, and manual dispatches. Branch
protection requires the uniquely named `Python 3.12 quality` check before merging into `main`.

For Development Workflow v1, this required check is the authoritative full deterministic gate for
the exact pull-request SHA. Builders run focused local checks while iterating; reviewers reuse a
green check for that SHA and do not repeat the full suite without a concrete uncovered risk. A full
local `bash scripts/check.sh` remains appropriate when CI, dependencies, lock, or toolchain changes,
when CI is unavailable or under diagnosis, or when the Work Block explicitly requires it.

## Environment and commands

The gate uses the fixed `ubuntu-24.04` runner label and Python 3.12, matching the supported project
runtime. It bootstraps the pinned `uv 0.11.29` release, verifies that `uv.lock` matches
`pyproject.toml`, and synchronizes the exact locked development environment. The literal required
check remains `Python 3.12 quality` and aggregates:

1. a deterministic collection of the complete test universe (`ci-partition-manifest-v1`);
2. four disjoint shards, each validating the planned universe digest before running its exact node
   ids;
3. repository-wide Ruff check and format verification;
4. combined branch/subprocess coverage with a global floor of 82.00%;
5. `pip-audit --local --skip-editable` against the locked environment.

The authoritative recipe and local shard commands live in
`.github/workflows/ci_support/README.md`. Builders run `FOCUSED` checks while iterating; the
aggregate `FULL` gate is mandatory for the exact final SHA before AUDIT and merge. `FOCUSED` cannot
produce `FULL`, and green evidence is never transferred between SHAs.

The local `FULL` recipe remains `bash scripts/check.sh`, executed once when CI, dependencies, lock,
or toolchain changes, when CI is unavailable, or when the Work Block explicitly requires it. The
optional `compare_serial=true` dispatch compares the partition engine with a serial reference and
requires at least 40% critical-path reduction; it is not a second required check. Serial-reference
evidence is reused only for the same SHA and collection.

## Security and resource limits

- The workflow-level token permission is limited to `contents: read`.
- Checkout does not persist GitHub credentials in the working copy.
- Third-party workflow code is limited to official GitHub actions pinned to full immutable commit
  SHAs. The adjacent release comments record the reviewed versions.
- The workflow uses `pull_request`, not `pull_request_target`, so untrusted pull-request code does
  not receive a privileged base-repository context.
- A concurrency group cancels superseded runs for the same workflow and ref.
- The job has a 45-minute timeout (extended from 20 minutes by `DEV-CI-TIMEOUT-45`) and never calls
  market or fundamental providers.
- Artifacts retain the exact run/attempt manifests, shard receipts, coverage and balance report for
  7 days; a missing artifact or receipt fails the aggregate closed.
- CI requires no Alpaca, SEC, broker, or trading credentials.
- Dependency synchronization cannot update `uv.lock` during CI.
- The security audit never applies automatic dependency fixes.

## Scope and limitations

CI validates the deterministic local test suite on one supported Linux/Python combination. It does
not run the real-provider Apple bootstrap, mutate the permanent workspace, deploy software, execute
operations, or replace the real read-only validation required for provider-sensitive changes.

Provider-sensitive smoke evidence is recorded separately and bound to the same head SHA. Any new
commit invalidates prior CI, smoke, and independent audit evidence. See
[`development_protocol.md`](development_protocol.md).

The committed lock makes installation repeatable, but vulnerability findings and package releases
can change over time. Dependency upgrades therefore remain explicit reviewed changes. The security
audit requires outbound access to its public advisory service; an advisory-service outage is an
environmental CI failure and must not be confused with a product-test failure.

See `docs/dependency_management.md` for lock maintenance and upgrade rules.
