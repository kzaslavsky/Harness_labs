# PlanApproval: repository-bound admission for PlanGraph

**Status:** Slices 0–1 implemented; Slices 2–3 deferred — 2026-08-10

**Origin:** flow-editor plan review (source-binding and decomposition reviewers
run against `FLOW_EDITOR_AUTHORING_AND_NODE_EXECUTION_UX_PLAN.md`,
`flow-node-mockup-parity` at `c00d0b1`)

## Decision summary

Plan approval is useful if it has one narrow job: prevent PlanGraph from
executing a decomposition that was not reviewed against the exact plan,
repository revision, and policy claimed by the run.

The original proposal had a strong core—immutable input binding, deterministic
checks before model review, structured findings, and explicit human
decisions—but combined that core with an overly broad reviewer/reviser
framework. Hashes alone do not prove approval, several proposed “static gates”
were heuristics, and an editable receipt would weaken rather than strengthen
the audit trail.

This revision therefore proposes:

1. one canonical **approval subject** describing the exact inputs under review;
2. one controller-issued, immutable **approval receipt** over that subject;
3. deterministic admission gates, with heuristics reported only as warnings;
4. structured, independently persisted reviewer evidence;
5. blocking and supersession—not receipt mutation—when a plan, decomposition,
   decision, reviewer policy, or bound repository input changes; and
6. incremental delivery, with automatic plan revision deferred until the
   admission-only production path is proven insufficient.

## Overall assessment

### Strengths retained

- **Correct placement.** Review belongs before PlanGraph dispatch, not inside
  each FeatureRun. This avoids repeated planning and makes FeatureRun consume a
  stable planning handoff.
- **Mechanical binding.** PlanGraph should reject an approval for different
  bytes or a different base commit. “Approved” must be a checkable condition,
  not a label in a prompt.
- **Deterministic-first ordering.** Schema, Git, reference, coverage, and graph
  checks should run before model calls. This improves both accuracy and cost.
- **Structured findings.** Findings need stable identities, evidence, and
  dispositions so required issues cannot disappear between review attempts.
- **Explicit human authority.** Product, policy, safety, and other genuinely
  semantic decisions must block rather than be guessed by a reviser.

### Weaknesses corrected

- **A digest is integrity evidence, not approval authority.** Within the first
  release's single-machine trust domain, controller issuance makes policy
  satisfaction explicit and prevents accidental drift or unsupported social
  claims; it does not prevent a malicious local actor with equivalent file and
  process authority from forging approval. PlanGraph must still validate the
  receipt's protocol, subject, status, and evidence references—not just compare
  two hashes—because that is the enforceable local admission contract.
- **The decomposition hash was underspecified.** Hashing an in-memory mapping or
  incidental JSON formatting is unstable. The subject must bind a canonical,
  schema-versioned decomposition artifact.
- **The design had a time-of-check/time-of-use gap.** A comparison during
  `validate()` is insufficient if files can change before dispatch or resume.
  Approval must bind Git objects at an exact commit, and PlanGraph must
  revalidate the subject before its first launch and on resume.
- **Several “static gates” were actually heuristics.** Grepping prose for
  sign-off language and edit-distance symbol matching can find useful leads but
  cannot safely approve or reject a plan. They are advisory checks.
- **Automatic revision mixed authority boundaries.** A model may repair
  mechanical defects in a draft, but it must not silently decide requirements
  or transform an approved plan. Every revision creates a new subject and
  invalidates all prior approval claims.
- **Mutable operator resolutions would corrupt provenance.** A blocked receipt
  is historical evidence. Resolution produces a new decision artifact and a
  superseding approval attempt; it never edits the old receipt.
- **The first build was too broad.** A pluggable multi-role review framework,
  reviser, new schemas, new CLI, and PlanGraph integration at once violates the
  repository's complexity-admission discipline. The first slice should prove
  admission and rejection end to end before adding autonomous revision.

## Relationship to the current PlanGraph design

`plan-projection-design.md` explicitly excludes approval envelopes, a
proposal/review/adjudication pipeline, and automatic plan revision. This draft
does not silently override that decision. Implementation requires an explicit
amendment or ADR identifying:

- the demonstrated production failure: reviewed plan/decomposition inputs can
  currently be supplied to PlanGraph without durable approval evidence;
- the production consumer: `PlanGraph.run()` and its resume path; and
- the end-to-end assertion: an unapproved or mismatched subject launches zero
  FeatureRuns, while the exact approved subject proceeds through the shipped
  production entrypoint.

The current `PlanGraphAudit` already records a plan digest and a digest of the
supplied graph. That protects checkpoint resumption from some input drift. It
does **not** establish that review occurred. PlanApproval should reuse the same
canonical identity function or replace it once; it must not create a second,
incompatible definition of PlanGraph identity. Slice 0 is incomplete unless a
test proves PlanApproval and `PlanGraphAudit` derive the same identity from the
same subject through that one implementation.

## Scope and non-goals

PlanApproval owns admission of an already-authored plan and decomposition. It
does not own FeatureRun implementation, verification, review/fix, integration,
or recovery.

The first production slice does not include:

- autonomous plan authoring;
- automatic semantic revision;
- a general-purpose role/profile framework;
- dynamic reviewer selection;
- cryptographic signatures or remote trust; or
- a second review lifecycle inside FeatureRun.

Local controller issuance is sufficient only for accidental-integrity and
policy-enforcement failures inside the initial trust boundary. It provides
auditable provenance, not cryptographic authenticity. A deliberately forged,
internally consistent receipt produced by an actor with equivalent local write
authority is out of scope. If receipts cross machines or administrative
boundaries, or deliberate local forgery enters the threat model, authenticated
actor identity, protected keys, signature verification, and key rotation
require a separate security design and decision.

## Approval subject

The controller first writes a canonical `plan-approval-subject/1` artifact.
Its digest is computed from canonical JSON (UTF-8, sorted keys, compact
separators) and covers at least:

```json
{
  "protocol": "plan-approval-subject/1",
  "repository": {
    "identity": {
      "id": "<repository-owned stable id>",
      "path": "<versioned identity artifact>",
      "git_blob": "<blob id>"
    },
    "base_commit": "<40-character commit>"
  },
  "plan": {
    "path": "docs/development/APPROVED_PLAN.md",
    "git_blob": "<blob id>",
    "sha256": "<digest>"
  },
  "decomposition": {
    "protocol": "plan-graph-plan/1",
    "path": "<tracked canonical JSON path>",
    "git_blob": "<blob id>",
    "sha256": "<canonical digest>"
  },
  "referenced_artifacts": [
    {"path": "<tracked path>", "git_blob": "<blob id>", "sha256": "<digest>"}
  ],
  "review_policy": {
    "policy_id": "plan-approval-policy/1",
    "policy_sha256": "<digest>",
    "reviewer_profile_digests": ["<digest>"]
  }
}
```

The exact field schema belongs in `schemas/`; the example defines the intended
boundary, not a final schema.

`repository.identity.id` is a repository-owned stable identifier stored in the
recorded versioned artifact and bound at `base_commit`. It is invariant across
worktrees, clones, and remote-URL changes. Filesystem paths and origin URLs are
not repository identity. A fork that intends to become a distinct approval
authority domain must deliberately rotate the identifier.

The canonical decomposition includes every controller-enforced input reviewed
for each run: normalized repository-relative `allowed_paths`, structured
`verification_argv`, `verification_timeout_seconds`, explicit path intents and
command-path dependencies, plus any additional attempt budget that the
production launcher actually consumes. These values travel in
`FeatureRunRequest`; the PlanGraph-bound adapter must use them as the
authoritative `run_feature_worktree` options and reject caller overrides. It is
not sufficient to repeat an allowed-path list in an advisory build briefing.
Likewise, all final functionality commands belong in the canonical
decomposition; the production CLI must not append unbound commands after
approval.

`base_commit` is deliberately absent from the committed
`plan-graph-plan/1` artifact. A file cannot contain the hash of the commit that
contains that same file without a circular identity problem. The approval
subject supplies `base_commit` and binds the decomposition blob found there;
PlanGraph constructs its executable plan only from that validated pair.

The plan, decomposition, and reviewable referenced artifacts must exist as Git
objects at `base_commit`. “Tracked and clean at a commit” is not meaningful;
the check is that the named path resolves to the recorded blob in the recorded
commit and that the reviewed bytes equal that blob. If draft revision changes
those bytes, a new commit, subject, and approval attempt are required before
execution.

Because `base_commit` already binds the complete Git tree, the
`referenced_artifacts` list does not add integrity coverage. It explicitly
enumerates reviewer inputs, explains their relevance, and supports minimal
context assembly. An unlisted tree path remains protected by the commit; it is
not thereby represented as a reviewed input.

Path intent must be explicit in the decomposition or plan metadata. A path
expected to be created cannot be validated with the same existence rule as a
path expected to be modified. The controller must not infer this distinction
from prose.

## Deterministic gates

Hard gates run before any reviewer. A failure prevents approval.

| Hard gate | Required assertion |
|---|---|
| Schema | Subject, plan decomposition, findings, and receipt validate against versioned schemas. |
| Repository binding | The repository-owned identifier and `base_commit` resolve; every commit-bound input resolves to the recorded Git blob at that commit. |
| PlanGraph validation | The production `PlanGraph.validate()` logic accepts the exact canonical decomposition without creating a worktree or launching a child. |
| Coverage and references | Sections, criteria, dependencies, and explicit create/modify path intents are internally consistent. |
| Scope binding | Every run has non-empty normalized `allowed_paths`; the reviewed values are exactly the values transmitted to and enforced by `run_feature_worktree`. |
| Command shape and availability | Each required per-run and final functionality-test command uses non-empty argv. Base-available executables and required paths resolve in a clean worktree at the bound commit. A graph-created command path names its creating run and is consumed only after that run in dependency order. |
| Policy bounds | Declared attempts and timeouts are within limits enforced by the production launcher that consumes them. A value with no enforcing consumer is rejected as unsupported. |
| Decision completeness | Every explicit `operator_decision` item has a referenced, immutable resolution artifact or the attempt blocks. |

The current PlanGraph representation stores final functionality tests as shell
strings and executes them with `shell=True`, while per-run verification uses
argv. Slice 0 must normalize final tests to structured argv in
`plan-graph-plan/1` and execute them without an implicit shell. A test that
intentionally needs shell semantics must name the shell explicitly in argv,
for example `['sh', '-c', '<command>']`, and remains subject to policy. This
closes the observed clean-clone exit-127 failure class while the canonical
contract is already changing.

Admission cannot require every command path to exist at `base_commit`: an
upstream run may create a verification script used by a dependent run or by the
final functionality test. The canonical command record therefore declares its
repository-relative required paths and links each graph-created path to an
explicit create intent. A missing base-available path is a hard failure. A
declared graph-created path is a reviewer-verified obligation: its producer
must precede every consuming run transitively, or precede graph completion for
a final test. Runtime verification remains authoritative that the producer
actually created the path. The controller must not guess path semantics from
arbitrary argv values.

Command dependencies distinguish three location classes. Repository-relative
paths resolve inside the verified snapshot and participate in base-versus-create
intent checks. Host-absolute executables resolve on the admission host and must
exist and be executable; gate evidence records their absolute path, SHA-256,
size, and modification time. Bare executable names resolve through the recorded
admission `PATH`, and the evidence records the resolved absolute executable with
the same identity fields. PlanGraph re-resolves and compares every host
executable before first launch and on resume. An identity mismatch invalidates
admission. This is an explicit local environment dependency, not repository
reproducibility; portable plans should prefer stable toolchain entrypoints.

These checks are useful diagnostics but **not** approval gates by themselves:

- phrases such as “obtain sign-off” or “operator ruling required”;
- near-neighbor symbol matches based on edit distance; and
- guessed file intent derived from natural language.

They may emit warnings or reviewer leads. Promoting one to a hard gate requires
measured precision and an explicit false-positive policy.

## Reviewer contract

The initial implementation needs the two demonstrated roles only:
source-binding and decomposition review. A general plugin registry is deferred
until a third real role demonstrates the need.

Each reviewer receives a versioned context packet containing the subject,
relevant plan sections and artifacts, exact repository revision, acceptance
criteria, exclusions, budget, and output schema. Repository access and writable
authority remain separate from that packet. Reviewers are read-only. The
controller materializes a clean, detached worktree at exactly `base_commit` (or
provides an equivalent immutable Git-object view), verifies that its tree is
clean immediately before dispatch, and records the resolved HEAD, tree digest,
worktree or snapshot identity, and command environment in the attempt receipt.
A review performed against a dirty tree or another revision is invalid evidence.

Each finding has at least:

```json
{
  "finding_id": "<stable id>",
  "reviewer_role": "source-binding",
  "severity": "high",
  "class": "incorrect_source_binding",
  "criterion_ids": ["AC-3"],
  "claim": "<falsifiable claim>",
  "evidence": [
    {"kind": "git_blob", "ref": "<commit>:<path>", "location": "<symbol or line>"}
  ],
  "proposed_resolution": "<bounded recommendation>",
  "requires_operator_decision": false
}
```

The controller validates schema and evidence references, persists the raw
attempt separately, and maintains a disposition ledger. A reviewer saying
“approved” is not sufficient. Approval requires:

- all required reviewer attempts to finish successfully;
- zero unresolved critical or high findings;
- every lower-severity finding to have an explicit accepted, rejected,
  deferred, or fixed disposition under policy; and
- all deterministic gates to pass against the final subject.

The versioned policy enumerates which actor roles may apply each disposition by
severity and finding class. Every ledger entry records the acting identity,
role, authority rule, timestamp, rationale, and evidence. A reviewer may not
dispose of its own finding unless an explicit policy rule grants that authority;
the default policy does not.

A superseding attempt carries a disposition forward only when the finding's
stable identity and complete evidence digest are unchanged and the bound
authority policy still permits that disposition. Changed or missing evidence,
changed policy, or changed finding identity voids the prior disposition and
requires a fresh authorized action. Carry-forward is recorded as a new ledger
event referencing the prior disposition; history is never copied silently.

The decomposition reviewer has a required
`undeclared_operator_decision` check. Its result envelope must attest that this
check ran even when it produced no findings; omitting the check makes the
attempt invalid. Prose heuristics may supply leads, but the model review is
responsible for identifying embedded sign-offs, unresolved product choices,
and other decision points that were not declared in plan metadata.

Parallel execution is allowed because the two reviewers are independent, but
the run owner remains accountable for validating both results. Using different
sessions or models may reduce correlated error; it is not itself proof of
independence.

### Invalidation and review cost

The controller runs deterministic gates after each draft edit and batches all
known mechanical corrections into one revision before launching another model
review. It must not pay for a reviewer cycle after each independently fixable
static-gate failure.

Reviewer profiles declare the complete input classes that can affect their
verdict. Persisted reviewer evidence may be reused for a new subject only when
the policy is unchanged and every declared input has an identical digest. A
base-commit change therefore always creates a new subject and reruns static
gates, but it need not automatically rerun a reviewer whose complete declared
inputs remain byte-identical. Source-binding review defaults to the entire Git
tree because repository changes may invalidate its claims. Decomposition review
may use a narrower set only if its role contract excludes repository-dependent
claims and enumerates every artifact it consumes; command viability, path
existence, or code-bound dependency analysis requires the relevant tree inputs.

Missing, ambiguous, or newly expanded dependency declarations force re-review.
Reuse is recorded as a controller decision containing the prior attempt digest,
old and new subject digests, declared input set, equality proof, and policy
digest. Equality of the plan and decomposition blobs alone is never sufficient
when a reviewer was allowed to inspect more of the repository.

With the two initial reviewer contracts, safe reuse will be uncommon; batching
mechanical corrections is the primary cost control. Reviewer dependencies must
never be narrowed merely to increase cache hits. Any future optimization must
first preserve the role's complete decision inputs and demonstrate equivalent
finding quality on the same task suite.

## Revision and human decisions

Revision is not part of the first production slice.

If later evidence justifies it, a reviser may operate only in a dedicated
approval worktree with explicit writable paths for the draft plan and canonical
decomposition. It may fix mechanical defects whose intended outcome is already
determined—for example, a wrong path prefix or missing criterion assignment.
It must block on ambiguous requirements, policy choices, safety decisions,
acceptance-criterion changes, or scope expansion.

Every changed byte creates a new subject. The controller reruns all hard gates
and applies the declared-input invalidation policy above. “Re-review only
changed claims” is unsafe without a complete, tested dependency declaration
because a local edit can alter coverage, dependencies, or another reviewer's
conclusion.

An operator decision is an immutable artifact with actor, timestamp, question,
chosen resolution, alternatives, rationale, and subject digest. It causes a new
approval attempt that supersedes the blocked attempt. Historical subjects,
receipts, findings, and decisions remain append-only.

## Lifecycle, ownership, and stopping

PlanApproval is the pre-dispatch admission boundary between an authored plan and
PlanGraph. Logically it completes the planning handoff, but the current
PlanGraph runtime begins after planning and does not persist a `plan` phase.
The required ADR must define how approval maps onto the canonical lifecycle
before implementation; this draft does not claim that phase already exists in
the runtime. The internal approval state machine is:

```text
draft -> frozen -> gating -> reviewing -> approved
                      |          |
                      +----------+-> blocked
                      +----------+-> failed
```

Every transition emits a structured event and atomically updates a checkpoint.
The checkpoint records the run owner, subject digest, active attempt, budgets,
gate states, reviewer states, findings ledger, pending decisions, and next
responsible component. Resume trusts this checkpoint plus revalidated Git
objects, never chat history.

`blocked` is used only for an operator decision or unavailable required input.
Invalid schema, failed deterministic checks, invalid reviewer output after the
attempt limit, and exhausted runtime budgets terminate as `failed` with
evidence. Retry and runtime limits must be finite in the production profile.

Approval supersession never grafts completed nodes from an old graph onto a new
subject. In the initial policy, a mid-graph plan correction creates a new
approval attempt and a new PlanGraph execution from its newly approved base;
all nodes are executed again. The old candidate lineage is retained as evidence
but is not automatic input. Preserving completed work would require a separately
approved subject based on the last good candidate plus an explicit remaining-work
decomposition and lineage-transfer contract; that recovery is deferred.

## Approval receipt and PlanGraph consumption

The controller writes an immutable `plan-approval-receipt/1` only after approval
conditions are true. It contains the subject digest, terminal status, controller
identity as provenance within the local trust domain, approval-run identity,
policy digest, gate-result references,
review-attempt references, disposition-ledger digest, and creation timestamp.
Large evidence stays in content-addressed artifacts; the receipt references it.

PlanGraph accepts the receipt as a required input only after the admission path
is enabled. Before audit initialization, before the first FeatureRun launch, and
on every resume, it must fail closed unless:

1. the receipt schema and protocol are supported;
2. the receipt status is `approved`;
3. referenced evidence exists and matches its digest;
4. the recomputed subject matches the receipt subject digest;
5. the repository, base commit, plan blob, canonical decomposition, referenced
   artifacts, and enforced review policy still match; and
6. its existing PlanGraph checkpoint identity agrees with the same subject.

Validation failure launches zero new FeatureRuns and records a concise admission
failure. The controller should read bound content from the verified Git objects
or an immutable snapshot, not re-read mutable working-tree files after checking
them.

## Incremental build plan

### Slice 0: decision and canonical identity — implemented

- Amend `plan-projection-design.md` or add an ADR authorizing this narrow
  exception to its exclusions.
- Define canonical `plan-graph-plan/1` serialization, including structured argv
  for final functionality tests; per-run `allowed_paths`, path intents,
  command-path dependencies, and verification timeouts; and migrate the
  production runner away from implicit shell execution.
- Make `PlanRun`/`FeatureRunRequest` carry the approved per-run scope and timeout
  values. Make the PlanGraph-bound adapter enforce those exact values and reject
  separately supplied overrides.
- Add and bind the repository-owned stable identifier used across clones and
  worktrees.
- Implement one canonical identity function consumed by both PlanApproval and
  `PlanGraphAudit`; neither component may maintain a private digest definition.
- Add a regression test proving formatting-independent identity where intended
  and byte-sensitive identity for bound source artifacts.
- Add a cross-component test proving both consumers produce the same identity
  and fail together when any bound input changes.

### Slice 1: deterministic admission — implemented

- Add subject and receipt schemas.
- Make the Slice 1 policy explicitly operator-attested. Controller issuance
  requires one immutable operator approval artifact bound to the exact subject;
  a transcript, reviewer prose, or generic “manual review” pointer is not
  approval evidence. The receipt names this policy and must not imply that
  automated reviewers ran.
- Require and validate that receipt through the shipped PlanGraph CLI.
- Prove that changed plan bytes, decomposition, base commit, policy, or evidence
  cause zero FeatureRun launches, including on resume.

### Slice 2: demonstrated reviewers — deferred

- Add the findings schema, source-binding reviewer, decomposition reviewer, and
  disposition ledger.
- Use the existing subprocess attempt and context-packet machinery where it
  satisfies the contract; do not introduce a parallel execution abstraction
  merely for PlanApproval.
- Run each reviewer against the controller-verified clean snapshot at the
  subject's exact base commit and record its declared invalidation inputs.
- Require fresh successful reviewer evidence before issuing the full-policy
  receipt, except where the controller records a valid byte-identical reuse
  proof under the declared-input policy.

### Slice 3: optional bounded revision — deferred

- Admit only after production evidence shows manual correction is the material
  bottleneck.
- Add mechanical-only classification, explicit writable paths, no-progress
  detection, a finite attempt limit, and full re-gating/re-review.
- Keep semantic changes operator-owned.

## Acceptance criteria

The design is implemented only when the production entrypoint demonstrates:

1. An exact approved subject launches the first FeatureRun and records the
   receipt and subject digest in the PlanGraph audit.
2. A changed plan, decomposition, referenced artifact, base commit, or enforced
   policy launches zero FeatureRuns.
3. The same mismatch is rejected on resume before any additional FeatureRun is
   launched.
4. A malformed or internally inconsistent receipt, missing evidence artifact,
   unsupported schema version, or non-approved status fails closed. Prevention
   of a deliberately forged but internally consistent local receipt is not
   claimed without the deferred authentication design.
5. A deterministic gate failure spawns no reviewer and launches no FeatureRun.
6. An unresolved critical/high finding or operator decision prevents receipt
   issuance.
7. A resolved decision creates a new immutable subject/attempt and leaves the
   blocked history unchanged.
8. The shipped CLI executes the admission-to-PlanGraph handoff as one production
   subprocess path; direct function tests are supporting evidence only.
9. Events, checkpoint, subject, receipt, findings, dispositions, and final
   summary are sufficient to reconstruct who approved what and why.
10. The implementation changes no FeatureRun review, verification, integration,
    or recovery semantics.
11. Review attempts run against a clean snapshot whose HEAD and tree identity
    match the recorded subject; dirty or wrong-revision attempts are rejected.
12. Final functionality tests use structured argv in the canonical plan,
    execute without an implicit shell, and either resolve at the base commit or
    declare a path created by a graph run that completes before the final test.
13. A byte-identical reviewer-input set can reuse prior evidence with a durable
    equality proof, while any changed or undeclared dependency forces a fresh
    review.
14. The exact reviewed `allowed_paths` and verification timeout for each node
    reach `run_feature_worktree`; a launcher or caller attempt to widen or
    replace them fails before creating the node worktree.
15. A verification command may consume a path declared as created by a
    transitive predecessor, while a missing base path, absent creator, or
    unordered creator/consumer relationship fails admission.
16. Every finding disposition is accepted only from an actor authorized by the
    bound policy for that severity, class, and disposition.
17. Two worktrees or clones with the same repository-owned identifier and
    commit validate as the same repository; matching filesystem paths or remote
    URLs alone do not establish identity.
18. Host-absolute and `PATH`-resolved executables record identity evidence at
    admission; a changed executable fails pre-launch or resume validation.
19. A disposition carries into a superseding attempt only when finding identity,
    evidence digest, and authority policy are unchanged.
20. A superseding subject cannot resume or import completed nodes from the old
    PlanGraph; the initial policy starts a new graph and retains the old lineage
    only as evidence.

## Recommendation

Slices 0 and 1 now provide the highest-value property—fail-closed execution of
only the exact admitted subject—without committing to a generalized
review/revision framework. Proceed with Slice 2 only after production use of the
operator-attested path demonstrates that model review is the next material gap.
Do not build Slice 3 until observed approval runs justify its additional
authority and complexity.
