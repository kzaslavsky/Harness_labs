# Delta-to-run: agent guide

For AI agents operating campaigns in harness_labs. The delta-to-run pipeline
turns statements about required/deviant behavior into an approved, running
PlanGraph. Full design: `delta-to-run-plan.md`; decomposition:
`delta-to-run-decomposition.json`. Read this instead of re-deriving the flow.

## Pipeline at a glance

```
statement/audit ──intake──▶ finding ──synthesis──▶ decomposition
  ──refine/prepare──▶ operator approval ──issue──▶ receipt
  ──register──▶ PlanGraph run (FeatureRun per node)
```

## 1. Finding intake (`harness_labs/plangraph/finding_intake.py`)

- `draft_finding(statement, *, repo_root, target, ...)` →
  `DraftFinding | IntakeQuestion`. An `IntakeQuestion` means ambiguity:
  surface it to the operator; NEVER guess `required_paths` — wrong
  attribution recreates scope-fence churn (ADR 0007's dominant-cost lesson).
- `required_paths` is the load-bearing field: node grants derive from it
  alone. `file ∈ required_paths`; key is `(file, subject)`.
- `confidence` is `"S"` for statements, `"C+S"` when capture evidence refs
  attach. Judgment calls get `requires_disposition: true`.
- CLI: `scripts/report_finding.py "<statement>" --ledger ... --campaign-root
  ... --target ...`; `--batch <json>` transcribes a seed audit.
- **Sealing never folds.** The CLI seals via `CampaignArtifactStore.seal`
  and never calls `ingest_audit` — folding a partial statement mid-round
  marks every other open key `unobserved` and fabricates failed repair
  claims. The artifact rides the next round's real measure/ingest.
  (`seal_audit_result` seals only evidence files — not the envelope.)
- Idempotent by digest: byte-identical input reseals, changes nothing.

## 2. Plan synthesis (`harness_labs/plangraph/plan_synthesis.py`)

- `plan_synthesis(...)` over `ConvergenceLedger.open_findings()` (folded
  envelopes per open key — use it, never re-fold the journal).
- Emits: one run per `required_paths` group, `allowed_paths` = union of the
  group's `required_paths`, a path intent per grant, and a trailing
  `OBSERVABLE:{"kind":..., "referent":...}` on every criterion — that arms
  conformance blocking (S5); `enforce=True` rides the driver's approve path
  only (`scripts/approve_plan.py` has no enforce flag).
- Output must round-trip `canonical_plan_graph_payload` (CLOSED top-level
  key set — never invent payload fields) and satisfy the driver round
  contract (`join_regression_node_id`, `validate_round_grants`).

## 3. Sanitizer policy (capture + driver)

Two surfaces, deliberately distinct — do not confuse them:
- capture-side `resolve_sanitizer`/`sanitize_before_journal`
  (`scripts/ui_fidelity_capture.py`, dispatches on artifact kind, raises
  `SanitizerError`);
- driver-side `resolve_pre_journal_sanitizer`/`sanitize_before_journaling`
  (`scripts/run_convergence_campaign.py`, reads `CONFIG_SANITIZER_KEY`,
  raises `SanitizerFailure`).

Config: legacy string (uniform hook) OR mapping
`{"text": <hook>, "binary": {"<kind>": "scan"|"admit:<reason>"|"reject"}}`.
Undeclared binary kinds fail closed. Capture takes the mapping as
`--sanitizer-policy <json-file>` (mutually exclusive with `--sanitizer`).
`--dry-run` reports would-be rejections and journals nothing. Policy CONTENT
(PHI rules etc.) is product config, never harness code.

## 4. Measurer commissioning (before round 1, not at first audit)

- `scripts/commission_measurer.py stability --runs N ...` — capture-matrix
  stability report; exits nonzero while any cell is chronically unstable and
  unruled (`--rulings-file`: `{"<cell>": {"disposition": "excluded"|
  "threshold_amended", "reason": ...}}`).
- `scripts/commission_measurer.py recall --inspector module:callable
  --seed-findings <batch envelope>` — inspector recall report.
- Both reports seal via `CampaignArtifactStore`. **Campaign open requires
  them**: `build_campaign_config` refuses without `stability_report_digest`
  + `recall_report_digest` unless `commissioning_override: {"reason": ...}`
  (ADR 0008). The driver's `close` derives `inspector_recall` from the
  sealed recall report when not supplied.
- CI uses `--driver stub`; core module is plain-JSON in/out (no plangraph
  imports).

## 5. Launching campaigns (`harness_labs/graphrun/campaign_launcher.py`)

- `build_campaign_launch_config(...)` carries every campaign-learned
  default: recovery_limit=5, continuation_recovery_limit=3,
  verification_repair_limit=3, 7200s coordinator silence tolerance (must
  exceed longest worker runtime), dirty-baseline wiring, max_parallelism=5,
  CC-08 escalation (judge seat + `transfer_ownership` +
  `max_structural_decisions`).
- Worker instructions: operator-notes fold into implementer/review/fix;
  `ANTI_PLACEHOLDER_FLOOR` appears in all four roles. Do NOT hand-copy the
  experiments launcher for new campaigns — use the kit
  (`experiments/run_dtr_plan_graph.py` is the last copy; it predates the
  kit).
- Plan amendments mid-lineage go through `--transition <file>` on the `run`
  or `resume` stages: the file is a `plan-graph-version-transition/1` record
  (`schemas/plan-version-transition.json`) whose `predecessor_plan_sha256`
  must equal the persisted registration's `plan_sha256` exactly — refused
  otherwise, before any registration or ledger state changes. The successor
  registration atomically replaces the persisted predecessor (same logical
  id, lineage, and run root preserved; no `-r2` re-registration needed), and
  the retry ledger consumes one structural decision with per-node
  `budget_carryover`. Requires the campaign's `automatic_recovery` to have
  granted the revision action (e.g. `revise_acceptance`) at first
  registration — the authority is registration-immutable, so opt in via
  `build_campaign_launch_config(automatic_recovery=...)` when creating the
  lineage. After the transition is consumed, plain `run`/`resume` (no flag)
  keeps working: the launcher adopts the persisted transition-carrying
  registration and the ledger replays the consumed record idempotently.

## Operational gotchas (each cost a real blocked attempt)

1. **Structured output has no dry run.** A worker's structured result is
   accepted exactly once; a stub (`"summary": "test"`) becomes the permanent
   report, trips the deliverable floor, and the recovery authority treats
   floor trips as NON-transient (stops the node, no retry from budget).
2. **prepare and issue must run in the same shell**: gate evidence pins the
   `PATH` env var (`host_path`) and issue refuses on mismatch (TOCTOU
   guard).
3. **Approval dirs are write-once** (hard-link semantics): a re-prepare
   needs a fresh `--run-id`.
4. **The base repo must be pristine at every node launch** — including
   tracked-file edits made mid-run (a dashboard config edit blocked a
   resume). Commit operational changes before resuming.
5. Resume needs an explicit retry frontier and a
   `artifact:sha256:...` blocker evidence ref; completed nodes are reused
   via reuse receipts.
6. Operator rulings go in `logs/plan-approval/operator-notes/<node>.md` —
   folded into ALL retry-worker instructions, gitignored, never dirties base.
7. Dashboard: `scripts/run_dashboard.py --audit-root logs/runs/<graph-root>
   --assets-root dashboard/plan-graph/dist` (see `.claude/launch.json`).
