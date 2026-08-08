# PlanGraph: autonomous FeatureRun queue

Status: proposed
Date: 2026-08-08

## Goal

An operator approves one human-readable engineering plan, with referenced UI
mockups and functionality tests where applicable. PlanGraph decomposes that plan
into dependent FeatureRuns and executes them without the operator manually
queueing each run.

```text
approved plan
  -> FeatureRun list and dependencies
  -> automatic FeatureRun execution
  -> tested integration candidate or concise failure
```

PlanGraph owns only the queue. FeatureRun continues to own implementation,
verification, frozen-ledger review, repair, and candidate commit creation.

The CLI can load an in-process `module:callable` launcher or invoke a subprocess
launcher command. For the subprocess form, the controller stays alive, writes
one `FeatureRunRequest` JSON object to the command's stdin for each ready node,
requires one `FeatureRunOutcome` JSON object on stdout, checkpoints the returned
candidate commit, and immediately advances to the next node. The subprocess is
the narrow backend adapter; backend-specific behavior does not enter PlanGraph.

## First release

The first release supports one repository and executes FeatureRuns sequentially
in dependency order. Parallel execution and multi-repository programs are not
part of this release.

The input is:

- the approved plan file;
- any mockups or other artifacts referenced by that plan;
- the repository and exact base commit; and
- the functionality-test commands named by the plan.

The decomposition result contains only:

```json
{
  "plan": "docs/development/APPROVED_PLAN.md",
  "base_commit": "<commit>",
  "runs": [
    {
      "id": "contract",
      "objective": "Implement the contract described by plan section 2",
      "plan_sections": ["2"],
      "criteria": ["AC-1", "AC-2"],
      "depends_on": []
    },
    {
      "id": "consumer",
      "objective": "Implement the consumer described by plan section 3",
      "plan_sections": ["3"],
      "criteria": ["AC-3"],
      "depends_on": ["contract"],
      "verification_argv": ["python3", "scripts/ui_walk.py"]
    }
  ],
  "functionality_tests": ["python3 scripts/ui_walk.py"]
}
```

FeatureRun derives its ordinary implementation context and writable scope from
the cited plan sections and repository inspection. PlanGraph does not define a
second task, capability, review, or recovery contract.

## Execution

1. A decomposition model reads the approved plan and proposes the FeatureRun
   list.
2. The controller rejects unknown plan sections or criteria, duplicate run IDs,
   missing dependencies, cycles, and uncovered acceptance criteria.
3. The controller starts the first dependency-ready FeatureRun.
4. A successful FeatureRun supplies its candidate commit as the base for the
   next dependent FeatureRun.
5. The controller repeats until every run succeeds.
6. A FeatureRun with a declared verification command runs it after
   implementation and again if review repairs change its candidate.
7. The final plan test commands run against the resulting candidate.
8. The controller returns that candidate or stops with the failed FeatureRun and
   its existing evidence.

There is no automatic redesign of the decomposition after execution starts. A
failed FeatureRun does not modify PlanGraph or the harness.

## Required behavior

- Every acceptance criterion from the approved plan is assigned to at least one
  FeatureRun.
- A FeatureRun cannot introduce a criterion or objective absent from its cited
  plan sections.
- A dependent FeatureRun starts from the exact successful candidate commit of
  its dependency.
- A failed or blocked FeatureRun prevents its dependents from starting.
- Restarting the queue does not repeat a completed FeatureRun.
- Backend selection is passed through to FeatureRun; PlanGraph contains no
  backend-specific behavior.

## Explicitly excluded

- Markdown normalization into a second semantic plan representation.
- A projection proposal/review/adjudication pipeline.
- Projection manifests, approval envelopes, or authority amendments.
- Automatic plan revision or generalized invalidation.
- Parallel scheduling.
- Multi-repository integration.
- Program-level review or a second verification lifecycle.
- Recovery that changes the harness, decomposition, or approved plan.

These exclusions may be reconsidered only after a delivered feature exposes a
specific need.

## Implementation slice

Add one small PlanGraph runner around the existing FeatureRun entrypoint:

- `harness_labs/plan_graph.py`
- `tests/test_plan_graph.py`
- one CLI entrypoint after the Python path works end to end

Do not modify FeatureRun except for a concrete incompatibility demonstrated by
the end-to-end test.

## Acceptance tests

The implementation is accepted when:

1. A two-run `A -> B` plan executes both FeatureRuns without an operator message.
2. `B` starts from `A`'s exact candidate commit.
3. The final functionality test passes and the runner returns the final candidate
   commit.
4. A cycle, missing criterion, or unknown plan reference fails before creating a
   worktree.
5. If `A` fails, `B` is never started and the failure identifies `A`.
6. Restart after `A` succeeds continues with `B` without rerunning `A`.
7. The same test works with two interchangeable stub FeatureRun backends without
   changing PlanGraph.

## Retinology proof

After the deterministic end-to-end test passes, use one approved Retinology plan
that naturally decomposes into two dependent FeatureRuns. Success means the
runner reaches a final Retinology candidate and passes the plan's functionality
tests without manual FeatureRun queueing or changes to Harness Labs during the
run.

Work stops at this proof. Additional PlanGraph features require evidence from
this run that the minimal queue cannot deliver the approved plan.
