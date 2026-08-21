# 0008 — Campaign-open commissioning checklist

Status: accepted
Date: 2026-08-20
Owners: PlanGraph controller
Run: `delta-to-run-pipeline-attempt-3`, node `DTR-MC`
Concerns-paths: scripts/run_convergence_campaign.py, harness_labs/plangraph/convergence_campaign.py

## Context

`docs/development/delta-to-run-plan.md` (`dtr-problem`, `DTR-F4`) names a gap
that predates this record: no campaign pins a target and opens without ever
proving the measurer it will run against can be trusted. Capture instability
(a cell whose end state disagrees across otherwise-identical runs) and
inspector recall gaps (real findings the inspector fails to recover) both
surface today for the first time at the first post-repair audit, mid
campaign — after a repair round has already spent its budget on a base that
may itself be untrustworthy evidence. `harness_labs/core/measurer_commissioning.py`
(DTR-F4) now exists to run that calibration ahead of time: it runs the
capture matrix `N` times through an injected runner and classifies each cell
stable/unstable against a declared divergence threshold, and it scores an
injected inspector's recall against a seed-findings file. Both calibrations
produce a report that `scripts/commission_measurer.py` seals via the one
artifact store every other campaign artifact already goes through
(`CampaignArtifactStore`, CC-02).

Building the calibration machinery is not, by itself, a decision — nothing
compelled anyone to run it before opening a campaign. The decision this
record makes is where that compulsion lands: `build_campaign_config` in
`harness_labs/plangraph/convergence_campaign.py`, the one function
`pin_target` calls to assemble the config it hands to
`ConvergenceLedger.open_campaign` (CC-01). That is the real seat, not a
second, parallel check bolted onto the driver or the CLI: every
`campaign_opened` record in the repository is produced by `pin_target`, so a
refusal here is a refusal for every campaign, present and future, with no
second code path to keep in sync.

## Decision

`build_campaign_config` gains two optional keyword arguments,
`stability_report_digest` and `recall_report_digest` (the two digests
`scripts/commission_measurer.py` seals), and refuses to build a config
missing either one — naming each missing artifact by name in the raised
`ConvergenceCampaignError` — unless the caller supplies
`commissioning_override: {"reason": <non-empty string>}`. When both digests
are present they are recorded verbatim in the returned config (and therefore
in the ledger's `campaign_opened.config`); when an override is used instead,
the reason is recorded in the config's own `commissioning_override` key, so
the decision to skip commissioning is itself durable, auditable data, not a
silent gap. `pin_target` threads all three parameters straight through, so
`pin_target` — and every driver call site that reaches it, `open_campaign` —
inherits the refusal for free with no separate gate to add or drift out of
sync.

The driver's `close` step gains a matching, symmetric wiring:
`evaluate_termination` now derives `inspector_recall` from the sealed recall
report named by the campaign's own `recall_report_digest` config key when the
caller does not pass one explicitly, so the calibrated number this
checklist forces into existence actually reaches the recall-threshold gate
`evaluate_success_termination` already enforces (`bounds-termination`) — a
calibration nobody wires into the gate it exists to inform is dead weight,
not authority. An explicit `inspector_recall` argument still wins; a campaign
with no sealed recall report (a `commissioning_override` campaign) falls back
to `0.0`, the exact hardcoded default this field replaces, so an
override-opened campaign's termination behavior is unchanged.

## Alternatives

- **Gate at the driver's `open_campaign` method instead of
  `build_campaign_config`.** Rejected: the driver is one caller of
  `pin_target`, not the only one (`tests/test_convergence_campaign.py`'s
  `TargetPinTests` calls `pin_target` directly, and any future non-driver
  caller would too). A driver-level gate is a second copy of the same
  refusal that the direct-`pin_target` path — and any future one — could
  silently miss.
- **A separate `commission_campaign_open(config)` validator, called
  explicitly by every campaign-open path.** Rejected: "called explicitly by
  every path" is precisely the failure mode `dtr-risks` calls out — a
  refusal that depends on every caller remembering to invoke it is a refusal
  that will eventually be forgotten by exactly the caller that most needed
  it (a rushed manual campaign open). Folding the check into
  `build_campaign_config`, which every campaign-open path already calls to
  get a valid config at all, makes forgetting structurally impossible rather
  than merely discouraged.
- **Require the two digests unconditionally, no override.** Rejected: it
  would make every one of this campaign checklist's own author's tests, and
  every pre-DTR-MC test fixture across `tests/test_convergence_campaign.py`,
  `tests/test_convergence_campaign_driver.py`, and
  `tests/test_convergence_lifecycle.py`, into a commissioning fixture whether
  or not that is what it is testing — and would give a genuinely
  time-pressured operator no legal way to open a campaign against a target
  they have already manually verified. The override exists precisely so that
  the decision to skip commissioning is made once, explicitly, with a
  recorded reason, rather than by quietly disabling the checklist function
  itself.

## Evidence

- `harness_labs/core/measurer_commissioning.py` — `build_stability_report`
  (per-cell divergence classification, threshold recorded in the report,
  `unruled_unstable_cells`/`ruling_requests`/`success`) and
  `score_inspector_recall` (seed-findings recall scoring), both plain
  JSON in/out, importing nothing from `harness_labs.plangraph`
  (`tests/test_import_boundaries.py`, plus this module's own direct AST
  check in `tests/test_measurer_commissioning.py`).
- `scripts/commission_measurer.py` — `stability`/`recall` subcommands,
  sealing both reports via `CampaignArtifactStore`
  (`harness_labs/plangraph/convergence_campaign.py`).
- `harness_labs/plangraph/convergence_campaign.py` — `build_campaign_config`
  (`CONFIG_STABILITY_REPORT_DIGEST_KEY`, `CONFIG_RECALL_REPORT_DIGEST_KEY`,
  `CONFIG_COMMISSIONING_OVERRIDE_KEY`) and `pin_target`'s pass-through.
- `tests/test_convergence_campaign.py::CampaignCommissioningChecklistTests` —
  the refusal, the named-missing-artifact message, and the override path.
- `tests/test_convergence_campaign_driver.py::CampaignOpenRefusalTests` —
  the same refusal reached through `ConvergenceCampaignDriver.open_campaign`,
  and `DriverCloseTerminationRecallFromSealedReportTests` — `close`'s
  derived-vs-explicit `inspector_recall` wiring.
- `scripts/run_convergence_campaign.py` —
  `ConvergenceCampaignDriver.evaluate_termination`'s
  `_sealed_inspector_recall`.

## Consequences

**Required.** Every new campaign must either run
`scripts/commission_measurer.py stability` and `... recall` and pass both
sealed digests to `pin_target`, or supply an explicit, reasoned
`commissioning_override`. Every one of this run's own test fixtures across
the three granted test files that opens a campaign without real commissioning
artifacts now says so, in the fixture, via that override — no test fixture
opens a campaign under the pre-DTR-MC config shape by accident.

**Prohibited.** A config missing either digest with no override can no
longer reach `ConvergenceLedger.open_campaign` at all; `campaign_opened` can
no longer be recorded silently uncommissioned.

**Easier.** A campaign's own `campaign_opened.config` becomes a complete,
self-describing record of whether — and, if not, why not — the measurer was
calibrated before repair rounds began spending budget against it. The
recall-threshold gate at termination is driven by a real, sealed number by
default instead of every caller having to remember to compute and pass one.

**Harder.** Opening a campaign without commissioning now requires a
deliberate, recorded decision instead of simply omitting two keyword
arguments nobody previously had to think about.

## Validation and reversal

Keep this authority while every `campaign_opened` record in the ledger
either carries both commissioning digests or a reasoned override, and while
no campaign is observed opening with a stale or unrelated digest (a digest
that resolves in `CampaignArtifactStore` but was sealed for a different
target or a different repository state — this record does not yet bind the
digest to the target it commissioned). A future revision may need to close
that gap by recording the target digest inside the sealed report itself.

Reversal is a config-shape change, not a code deletion: dropping the refusal
means deleting the `missing_artifacts` check in `build_campaign_config`
while leaving the two digest keys as ordinary optional config fields —
existing `campaign_opened` records (with or without the digests, with or
without an override) remain valid either way, since `ConvergenceLedger`
already passes config through unvalidated beyond its own required keys.
