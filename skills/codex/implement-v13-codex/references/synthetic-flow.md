# Synthetic coordinator flow

This driver is the legacy parity fixture for the migration described in
[json-phase-flow.md](json-phase-flow.md). Its hard-coded catalog is historical
behavior to preserve for regression comparison, not the target architecture.

This mode certifies coordinator plumbing, not feature correctness. Although its
controller reads only a dispatch JSON, its child prompt explicitly names
`implement-v13-codex`; installed skill discovery can therefore load skill
documents. A successful legacy result does not prove empty context or zero
document reads. Use `run_phase_flow.py` for that claim.

The dispatch file is the unchanged JSON object returned by
`feature_queue_state.py dispatch`. The synthetic coordinator requires and validates
the normal production identity, lease, decision, path, planning-input, and
run-directive fields. It preserves and hashes the complete payload, including
unknown additive fields. Its core identity is:

```json
{
  "protocol_version": "1.0",
  "queue_run_id": "qr_example",
  "feature_run_id": "fr_example",
  "feature_index": 0,
  "description": "certify coordinator flow",
  "base_branch": "integration",
  "engine": "v13-codex",
  "runner": "implement-v13-codex",
  "dispatch_action": "launch",
  "coordinator_id": "coordinator_example",
  "lease_id": "lease_example",
  "decision_key": "Q1",
  "decision_record": "docs/development/decisions/2026-07-q1-decisions.md",
  "planning_inputs": [],
  "run_directives": [],
  "branch": "impl-codex-fr-example",
  "worktree_name": "impl-codex-fr-example",
  "worktree_path": ".claude/worktrees/impl-codex-fr-example",
  "artifact_dir": "handoff/serial-runs/qr_example/fr_example",
  "artifact_root": "handoff/serial-runs/qr_example/fr_example",
  "checkpoint": "docs/development/current_implementation_checkpoint.json",
  "checkpoint_path": ".claude/worktrees/impl-codex-fr-example/docs/development/current_implementation_checkpoint.json",
  "transaction_path": "handoff/serial-runs/qr_example/fr_example/feature-transaction.v1.json",
  "feature_result_path": "handoff/serial-runs/qr_example/fr_example/feature-result.v1.json",
  "merge_receipt": "handoff/serial-runs/qr_example/fr_example/merge-receipt.v1.json",
  "cleanup_proof": "handoff/serial-runs/qr_example/fr_example/cleanup-proof.v1.json",
  "clearance_report": "handoff/serial-reports/q1.md"
}
```

Run and independently verify:

```text
python3 scripts/run_synthetic_flow.py start --dispatch /absolute/dispatch.json
python3 scripts/run_synthetic_flow.py start --dispatch /absolute/dispatch.json --stop-after 4
python3 scripts/run_synthetic_flow.py resume /absolute/run-directory
python3 scripts/run_synthetic_flow.py verify /absolute/run-directory
```

The terminal `synthetic-feature-result.json` is deliberately protocol-distinct
from `feature-result.v1.json`; a feature queue controller must never accept it as merge
proof. The replacement debug protocol must preserve this non-interchangeability.
