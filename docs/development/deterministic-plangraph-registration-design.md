# Deterministic PlanGraph Registration — Minimal Design

## Purpose

PlanGraph must register its complete executable graph before creating an attempt
or dispatching a child. Registration prevents an ad hoc partial decomposition
from silently becoming the authoritative graph and ensures every attempt is
bound to one immutable graph definition.

Registration consumes an explicit, reviewed, machine-readable decomposition.
It does not infer executable nodes from Markdown headings.

This is an admission-boundary change, not a scheduler rewrite. The current
engine remains sequential in this bounded slice. Registration makes the full
DAG visible and immutable before execution; later PlanGraph work may change how
registered dependency-ready nodes are scheduled without changing their
definition.

## Frozen boundaries

Registration owns every input that changes graph semantics:

- logical graph identity;
- repository-relative approved-plan path and content hash;
- base commit;
- declared node order;
- node objectives, plan sections, criteria, dependencies, and per-node
  `verification_argv`;
- graph functionality tests.

Runtime must not add or override nodes, edges, criteria, verification commands,
or functionality tests after registration.

Launcher selection remains attempt-scoped execution infrastructure rather than
graph semantics. Both the existing `module:callable` launcher and subprocess
launcher forms remain supported by the `run` command. Their argv, cwd, and
timeout are recorded in attempt events but are excluded from the registration
digest. The launcher remains constrained by reserved child
identity, manifest, ancestry, and outcome validation.

The injected `functionality_test_runner` callable remains available only as a
test seam in Python unit tests. Production CLI execution always uses the
repository-owned runner and the exact registered functionality-test commands.

## Registration type

Add a frozen registration type in `harness_labs/plan_graph.py`:

```python
@dataclass(frozen=True)
class PlanGraphRegistration:
    protocol: str
    logical_graph_id: str
    plan_path: str
    plan_sha256: str
    base_commit: str
    graph_digest: str
    definition_json: str
```

`definition_json` is canonical JSON rather than a mutable `Mapping`. This gives
the frozen dataclass deep byte-level immutability and makes idempotent equality
unambiguous.

The persisted registration file is exactly one JSON object containing these
seven dataclass fields and no others. Its write-once canonical bytes are UTF-8
canonical JSON followed by one newline.

The decoded definition contains the complete executable graph:

```json
{
  "runs": [
    {
      "id": "PG-00",
      "objective": "...",
      "plan_sections": ["PG-00"],
      "criteria": ["PG00-01"],
      "depends_on": [],
      "verification_argv": ["python3", "-m", "unittest", "..."]
    }
  ],
  "plan_sections": {},
  "acceptance_criteria": {},
  "functionality_tests": []
}
```

The `runs` array preserves declared plan order because that order is semantic.
Object keys are sorted only during canonical JSON serialization.

`canonical_definition()` removes exactly the top-level `plan` and
`base_commit` fields accepted by `plan_from_mapping()`. Those fields are stored
once as `plan_path` and `base_commit` on the registration. It preserves every
other accepted decomposition field. `plan_from_registration()` reconstructs the
input mapping by combining those two registration fields with the decoded
definition before calling `plan_from_mapping()`.

## Graph and attempt identity

`logical_graph_id` is both an audit identity and a filename component. Validate
it before any path construction:

```text
^[a-z0-9][a-z0-9-]{0,127}$
```

Reject separators, dots, uppercase aliases, empty IDs, and any value that does
not round-trip as one direct filename.

`graph_attempt_id` is also an audit-directory filename component. Validate it
with the same rule before constructing an audit path or creating any state.

Including `logical_graph_id` in `graph_digest` is deliberate identity binding:
identical graph content registered under two logical IDs has two different
digests. Cross-ID duplicate-content detection is out of scope.

## Repository-bound plan bytes

Registration receives the repository root explicitly. The decomposition's
`plan` value must resolve to a normalized repository-relative POSIX path. Reject
absolute paths, `..`, symlinks escaping the repository, and paths absent from the
registered base commit.

Do not hash the working-tree file. Verify the base object and read approved-plan
bytes from Git:

```text
git rev-parse --verify <base_commit>^{commit}
git show <verified_base_commit>:<repository-relative-plan-path>
```

Compute `plan_sha256` from those committed bytes. Store only the normalized
repository-relative path. This makes the same repository state produce the same
registration in different worktrees and prevents a working-tree race between
hashing and attempt creation.

The approved machine-readable decomposition must therefore name a base commit
that contains the approved plan. An untracked, dirty-only, or otherwise
base-absent plan is not registrable. A working-tree modification of a path that
does exist at the base does not affect registration because only the committed
bytes are read.

## Deterministic registration function

Add one function:

```python
def register_plan_graph(
    *,
    repository: Path,
    logical_graph_id: str,
    decomposition: Mapping[str, object],
) -> PlanGraphRegistration:
    validate_logical_graph_id(logical_graph_id)
    plan = plan_from_mapping(decomposition)
    validate_plan_graph_plan(plan)

    base_commit = verify_commit(repository, plan.base_commit)
    plan_path = normalize_repository_path(repository, plan.plan)
    plan_bytes = git_show(repository, base_commit, plan_path)
    plan_sha256 = sha256(plan_bytes).hexdigest()

    definition_json = canonical_json(canonical_definition(plan))
    digest_input = {
        "protocol": "plan-graph-registration/1",
        "logical_graph_id": logical_graph_id,
        "plan_path": plan_path,
        "plan_sha256": plan_sha256,
        "base_commit": base_commit,
        "definition": json.loads(definition_json),
    }
    graph_digest = sha256(canonical_json(digest_input).encode()).hexdigest()

    return PlanGraphRegistration(
        protocol="plan-graph-registration/1",
        logical_graph_id=logical_graph_id,
        plan_path=plan_path,
        plan_sha256=plan_sha256,
        base_commit=base_commit,
        graph_digest=graph_digest,
        definition_json=definition_json,
    )
```

Extract the existing `PlanGraph.validate()` logic into:

```python
validate_plan_graph_plan(plan: PlanGraphPlan) -> None
```

Registration and runtime both call this function. There must not be separate
registration and runtime validators that can drift.

## Exact registration verification

`verify_registration(repository, registration)` performs these checks in order
without reading working-tree plan bytes:

1. Require protocol `plan-graph-registration/1` and reject unknown fields.
2. Validate `logical_graph_id` with the filename-safe rule.
3. Require normalized repository-relative `plan_path`.
4. Parse `definition_json` as one JSON object, canonicalize it again, and require
   byte equality with the stored string.
5. Reconstruct the digest input from the stored fields and decoded definition,
   recompute `graph_digest`, and require equality.
6. Resolve `base_commit^{commit}` and require the resolved SHA to equal the
   stored full SHA.
7. Read `plan_path` from that commit with `git show`, recompute
   `plan_sha256`, and require equality.
8. Reconstruct `PlanGraphPlan` with `plan_from_registration()` and run
   `validate_plan_graph_plan()`.
9. Recompute `canonical_definition(plan)` and require exact equality with the
   stored canonical definition.

Any failure occurs before attempt-directory creation, audit initialization, or
launcher invocation.

## Write-once persistence

Store registrations outside the dashboard's scanned run root:

```text
logs/plan-graph-registrations/<logical-graph-id>.json
```

Do not place registrations under `logs/runs/`: the current run catalog scans
every direct child directory and does not exclude dotted directories.

Persistence uses a genuinely write-once publication sequence:

1. Write canonical registration bytes to a same-directory private temporary
   file with mode `0600`.
2. `fsync` the temporary file.
3. Publish with an atomic non-overwriting operation such as
   `os.link(temp_path, final_path)` on the same local filesystem.
4. `fsync` the containing directory and unlink the temporary name.
5. If publication reports that the final path already exists, read and verify
   the existing registration.
6. Return idempotently only when the existing verified registration has the
   same `graph_digest` and canonical bytes. Otherwise refuse the logical-ID
   collision.

Do not use `os.replace()` or rename-overwrite for the final publication. An
advisory lock is unnecessary for this single immutable object because the
filesystem's exclusive publication decides the winner. Temporary files left by
a crash are not registrations and may be cleaned separately.

Registration requires a local filesystem that supports same-filesystem hard
links. If `os.link()` is unsupported, refuse publication with an explicit error;
do not silently weaken write-once semantics by falling back to rename.

Registration time may be recorded in a separate audit event; it is not part of
the deterministic artifact.

## Attempt boundary

Change the runtime boundary from:

```python
PlanGraph(plan, launcher, ...)
```

to:

```python
PlanGraph(repository, registration, launcher, ...)
```

Runtime first performs:

```python
plan = verify_registration(repository, registration)
```

All initial nodes and functionality tests come from the verified registration.
The constructor no longer accepts additive `functionality_tests=`. The
production CLI removes `--functionality-test`; callers must register a new
logical graph when graph-level functionality commands change.

`repository` is an explicit constructor input supplied by CLI `--repository`.
It is used for registration verification and as the source repository for
functionality-test candidate clones. `_run_functionality_test` must clone from
this repository, never from the coordinator process's `.` working directory.

The attempt registration binding records:

```json
{
  "logical_graph_id": "plangraph-parallelization",
  "registration_protocol": "plan-graph-registration/1",
  "registration_digest": "...",
  "graph_attempt_id": "plangraph-parallelization-attempt-3"
}
```

This exact object is persisted at attempt creation as
`checkpoint.json`'s `state.registration_binding`. It is part of the first
durable checkpoint, not inferred from journal history. On resume, the audit
journal first verifies the checkpoint envelope, then runtime reads only this
binding block and validates it against the supplied registration before it
consumes node status, candidate commits, functionality-test state, or a terminal
result. The generic `descriptor.json` schema remains unchanged, so this bounded
change does not require run-catalog or dashboard descriptor changes.

For compatibility in this bounded slice, existing `graph_run_id` remains the
attempt ID and audit-directory name. The CLI exposes it as
`--graph-attempt-id`; the internal rename may be deferred if necessary, but the
checkpoint registration binding must distinguish it from `logical_graph_id`.

Before audit-directory creation, validate `graph_attempt_id` with the same
filename-safe rule as `logical_graph_id`.

### Minimal retry semantics

A new attempt is a full rerun of the same verified registration. It does not
inherit completed nodes, candidate commits, or checkpoint state from a prior
attempt. This keeps registration independent of the current sequential resume
implementation and avoids claiming selective repair reuse before its evidence
contract exists.

Re-invoking `run` with the same `graph_attempt_id` is not a new attempt; it
preserves the existing intra-attempt crash-resume behavior. Before reading a
checkpoint, completed candidate commit, or terminal result, runtime must:

1. verify the supplied registration normally;
2. read the existing `state.registration_binding` from the verified checkpoint
   envelope;
3. require its `registration_protocol`, `logical_graph_id`, and
   `registration_digest` to equal the supplied registration; and
4. require the checkpoint node-ID set to equal the registered node-ID set and
   each checkpoint node's immutable graph projection (`objective`,
   `plan_sections`, `criteria`, `depends_on`, and `verification_argv`) to equal
   the registered node definition.

Any mismatch refuses resume before launcher invocation or audit mutation. An
existing terminal attempt may short-circuit to its stored result only after
these binding checks pass. Checkpoint state is trusted as resume evidence only
within that same verified attempt; it is never imported into a differently
named attempt.

Cross-attempt retry-frontier selection, candidate reuse, and protected-ref
validation belong to the dedicated repair-resumption work. When implemented,
they will be attempt-scoped receipts that reference the immutable registration;
they will not mutate it.

Changing a PG-00-only graph into PG-00 through PG-07 creates a new logical graph
registration rather than another attempt of the old graph.

## Bypass closure and compatibility

Every production path that can create PlanGraph audit state must consume a
verified registration.

- `scripts/run_plan_graph.py run` accepts a registration, not a raw
  decomposition.
- `scripts/import_plan_graph_state.py` is retired with an explicit
  incompatibility error. Legacy sequential state lacks the manifest, lineage,
  verification, ref, and attempt evidence required for safe registered reuse;
  it must not synthesize a registered attempt.
- The runtime constructor and CLI cannot append functionality tests after
  registration.
- Existing `--launcher module:callable` and `--launcher-command ...` modes remain
  available under `run` and are recorded as attempt infrastructure.

Update repository documentation that shows the old raw-decomposition invocation.

## Minimal CLI

Add two modes to `scripts/run_plan_graph.py`:

```bash
python3 scripts/run_plan_graph.py register \
  DECOMPOSITION.json \
  --repository /absolute/repository/root \
  --logical-graph-id plangraph-parallelization \
  --registration-root logs/plan-graph-registrations
```

Then run a full attempt from the immutable registration:

```bash
python3 scripts/run_plan_graph.py run \
  --repository /absolute/repository/root \
  --registration logs/plan-graph-registrations/plangraph-parallelization.json \
  --graph-attempt-id plangraph-parallelization-attempt-1 \
  --run-root logs/runs \
  --launcher-command ...
```

The `run` mode does not accept a raw decomposition or additive functionality
tests.

When `--registration-root` is omitted, it defaults to
`<repository>/logs/plan-graph-registrations`, not a cwd-relative path. Likewise,
relative `--registration` and `--run-root` values are resolved against the
explicit repository root so invocation directory cannot change their meaning.

Each child request carries the registered repository-relative path in its
existing `plan` field (sourced from registration `plan_path`), plus
`plan_sha256` and `plan_base_commit`. Although a later child may start from a
descendant candidate commit, it resolves the approved plan from the
`plan_base_commit` Git object available in its repository, not from the
coordinator's working tree or potentially changed candidate-tree bytes. It
verifies `plan_sha256` before executing the PlanGraph-bound child profile.
`PlanGraphFeatureRunBinding` owns `plan`, `plan_base_commit`, and
`plan_sha256`; `run_plan_graph_feature_worktree()` enforces the check in-repo
with `git show <plan_base_commit>:<plan>` before it calls
`run_feature_worktree()`. External launcher adapters cannot satisfy or bypass
this child-side admission check.

This is an explicit versioned change to `FeatureRunRequest` and the subprocess
launcher stdin JSON contract. The request fields are:

```json
{
  "protocol": "plan-graph-feature-run-request/1",
  "run": {},
  "base_commit": "<chained candidate commit>",
  "plan": "<registered repository-relative plan path>",
  "plan_base_commit": "<registration base commit>",
  "plan_sha256": "<registered plan hash>",
  "plan_graph_id": "<graph attempt ID>",
  "plan_node_id": "PG-01",
  "feature_run_id": "<reserved child run ID>",
  "run_dir": "<reserved child run directory>"
}
```

Add required `protocol`, `plan_base_commit`, and `plan_sha256` fields to
`FeatureRunRequest`; `SubprocessFeatureRunLauncher` serializes the same names.
The existing `base_commit` field retains its current meaning: the chained
candidate commit on which this child builds. The registration base is never
placed in or substituted for `base_commit`. Callable launchers receive the same
versioned dataclass contract. External subprocess launchers must require
`plan-graph-feature-run-request/1` and reject unsupported protocols rather than
guessing field semantics.

Delete the `_audit is None` request-construction branch. Registered execution
must initialize and bind `PlanGraphAudit` before `_request_for_run()` can create
a reserved `FeatureRunRequest`; calling it without an audit is an error.

## Lifecycle

```text
compile explicit full decomposition
→ validate and register immutable DAG
→ verify registration against committed Git objects
→ allocate full-rerun attempt
→ initialize every registered node as queued
→ launch the next dependency-eligible child in declared order
```

The current scheduler remains sequential. Registration does not introduce
parallel dispatch or a new stored `dependency-blocked` status. Dependency
relationships are already stored on each queued node; the dashboard may derive
that a queued node is waiting on dependencies without changing checkpoint
vocabulary.

The dashboard can display the full registered graph immediately because every
node and edge exists in the initial checkpoint before PG-00 starts.

## Required tests

1. The same committed repository state registered from different clean
   worktrees produces byte-identical canonical content and `graph_digest`.
2. Changing a node, node order, edge, criterion, verification command,
   functionality test, base commit, or committed plan content changes the
   digest.
3. An absolute, escaping, untracked, dirty-only, or base-absent plan path is
   rejected.
4. Re-registering identical canonical bytes is idempotent.
5. Concurrent different registrations for one logical ID produce one winner and
   one explicit collision; neither overwrites the other.
6. Runtime rejects protocol drift, noncanonical definition JSON, digest
   tampering, base-commit mismatch, plan-hash mismatch, or reconstructed-plan
   mismatch.
7. Registration validation failure invokes no launcher and creates no attempt.
8. Runtime provides only registered functionality tests; additive runtime tests
   are unavailable.
9. Before any launcher call, the initial checkpoint contains every registered
   node with status `queued` and every declared dependency edge.
10. While the first node runs, its checkpoint still contains every downstream
    node as `queued` with its registered `depends_on` edges; no new checkpoint
    status is introduced.
11. Reusing an existing attempt ID with a different registration protocol,
    logical ID, or digest is refused before checkpoint state is consumed or a
    launcher runs.
12. Reusing an existing attempt ID is refused when the checkpoint node set or
    any stored immutable node-definition field differs from the registered
    graph.
13. A valid same-ID invocation resumes its existing nonterminal attempt, and a
    terminal attempt short-circuits only after registration binding succeeds.
14. A second, differently named attempt uses the same registration digest but
    starts with no inherited completed nodes or candidate commits.
15. The retired legacy importer exits explicitly and creates no audit state.
16. Both callable and subprocess launcher modes still operate from a verified
    registration.
17. Functionality-test candidate clones use the explicit repository even when
    the coordinator cwd is elsewhere.
18. Callable and subprocess child requests use protocol
    `plan-graph-feature-run-request/1`, preserve the chained candidate in
    `base_commit`, and separately carry the registered relative `plan`,
    `plan_base_commit`, and `plan_sha256`; child-side resolution does not use the
    coordinator working tree or candidate-tree plan bytes.
19. Invalid graph attempt IDs are rejected before audit path construction.
20. Registration-root defaults and relative run paths resolve against the
    explicit repository, independent of cwd.
21. The run catalog ignores registration storage because it is outside
    `logs/runs/`.

## Bounded implementation scope

Primary files:

- `harness_labs/plan_graph.py`
- `harness_labs/plan_graph_audit.py`
- `harness_labs/feature_run.py` (registered child plan-path resolution only)
- `scripts/run_plan_graph.py`
- `scripts/import_plan_graph_state.py`
- `tests/test_plan_graph.py`
- `tests/test_plan_graph_observability.py`
- `tests/test_feature_run.py`

Documentation references to the old CLI invocation must be updated in the same
change. `run_catalog.py` and dashboard code remain out of scope because
registration storage is outside the scanned run root and no new stored node
status is introduced.

This change must not introduce Markdown graph inference, a generalized workflow
registry, parallel scheduling, selective cross-attempt reuse, unrelated
FeatureRun lifecycle changes, or mutable in-place graph expansion.
