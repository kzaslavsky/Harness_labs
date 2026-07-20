# Initializing Package — Implementation Plan

**Status:** Current
**Feature:** Reusable project-directory initializer with portable agent guidance, skills, and composable templates.
**Planning evidence date:** 2026-07-14

## Outcome and scope

Create a self-contained `Initializing` package that can create a new project directory from a base template plus one or more ordered overlays. The first supported overlays are `python`, `web`, and `regulated-health`; combinations such as `python + regulated-health` are required. It will seed a compact root `AGENTS.md`, reusable documentation/governance skeletons, a `/logs/` directory, module documentation templates, and usable project-local Claude and Codex skills.

The package will not copy Retinology's clinical implementation, private operational history, PHI-specific examples, historic workflow versions, or GitHub-plan-specific merge mechanics. Those are source material for generic templates and policies only.

## Observed source inventory

| Source | Observed reusable element | Planned destination / treatment |
|---|---|---|
| `/Users/kirillzaslavsky/claudeprojects/Retinology/AGENTS.md` | Bootstrap sequence, repository navigation, documentation lifecycle, verification discipline, evidence-minded behavioral rules, flexible WIP/subagent/UI guidance, and the long Git policy | Distill portable rules into the base `AGENTS.md`; move a greatly simplified Git/PR policy to `templates/base/docs/governance/git-policy.md`. Exclude Retinology architecture, PHI/L1 constraints, language mandates, dates, and GitHub-plan details. |
| `/Users/kirillzaslavsky/claudeprojects/ClaudeProjects/SOP-lite.md` | Workspace/repository orientation, one-active-unit preference, test-result honesty, synchronized status docs, decision/deviation records, and ticket-type-scaled process | Incorporate as concise base-agent and documentation rules; do not impose its full SRS/SDS/SVR/DHF process outside the regulated-health overlay. |
| `Retinology/.claude/commands/module-docs.md` | Navigation-focused module `context.md` and supporting API reference workflow | Preserve as a neutral Claude skill; create a Codex `SKILL.md` counterpart. Also seed generic `context.md` and `API.md` templates, because every module is required to have both. |
| `Retinology/.claude/commands/capture-learning.md` | Structured, PHI-free learning capture plus regenerated pitfall summary | Preserve as a neutral Claude skill and Codex counterpart; seed an empty schema-valid learning ledger, pitfall template, and a portable regeneration utility or explicitly make the neutral skill maintain the summary without a utility. |
| `Retinology/.claude/commands/implement-v11.md`, `serial-implement.md` | Resumable implementation/checkpoint and serial-queue workflows | Keep a Claude-source copy in the package after removing Retinology-specific paths, hooks, model/tool syntax, and merge rules. Port their operational intent to Codex using the current local Codex sources below, rather than copying Claude-only orchestration syntax. |
| `/Users/kirillzaslavsky/.codex/skills/implement-v11/SKILL.md`, `serial-implement/SKILL.md` | Current Codex-compatible durable checkpoints, planning/review/implementation/verification phases and serial queue behavior | Package as portable Codex skill sources, with paths and package metadata made project-neutral. |
| `Retinology/.claude/commands/local-review-v2.md` and `.agents/skills/local-review-PR/SKILL.md` | Complementary uncommitted-diff and committed-range review workflows, including evidence/cross-reference checks | Include as the two additional relevant skills: neutral `local-review` and `local-review-pr` variants for both Claude and Codex. They answer the requested “any others?” without importing obsolete v6/v8/v9/v10 workflows. |
| `Retinology/retinology/*/context.md`, `*_API.md` | Module-doc and API-doc shapes | Convert to content-free module templates with explicit placeholders; use the neutral API filename `API.md` in new modules, while the skills accept legacy `*_API.md` references when encountered. |
| `Retinology/docs/development/INDEX.md`, `learnings/learnings.json`, `learnings/PITFALLS.md`, `scripts/check_doc_status.py`, `scripts/check_doc_links.py` | Development index, learning/pitfall store, and documentation validation | Seed generic/empty equivalents. Preserve logic only where it is portable; parameterize exclusions and remove Retinology naming/history. |
| `Retinology/CONTRIBUTING.md`, `SECURITY.md`, `docs/architecture/ADR-*.md`, `logs/` | Useful top-level document and directory shapes | Seed content-free `CONTRIBUTING.md`, `SECURITY.md`, an ADR template, and `logs/.gitkeep`; never copy clinical content or actual logs. |

## Proposed package layout and contracts

```text
Initializing/
├── README.md
├── bin/initialize-project
├── AGENTS.md                         # package-maintenance instructions
├── skills/
│   ├── README.md
│   ├── claude/{module-docs.md,capture-learning.md,implement-v11.md,serial-implement.md,local-review.md,local-review-pr.md}
│   └── codex/{module-docs,capture-learning,implement-v11,serial-implement,local-review,local-review-pr}/
├── templates/
│   ├── base/
│   ├── python/
│   ├── web/
│   └── regulated-health/
├── assets/
│   └── module/{context.md,API.md}    # copied only for --module, never as an overlay
├── tests/test_initializer.py
└── docs/development/
```

`bin/initialize-project` will be a dependency-free, repeatable command-line initializer. Its contract will be:

1. Require a target directory, project name, and purpose; always select `base`, then accept repeatable `--template {python,web,regulated-health}` values and optional repeatable `--module` values. Document an exact invocation for base-only and each supported composition.
2. Reject unknown or duplicate overlays, unsafe project/module identifiers, a target that already exists (including a symlink), and any destination path that cannot be safely confined beneath the resolved target. Version 1 has no merge or overwrite mode; a future merge mode requires a separately reviewed, non-destructive manifest contract.
3. Copy `templates/base` first, then requested overlays in user-provided order. Each overlay has a machine-readable manifest with an identity, optional prerequisites, and an exact allowlist of paths it may override. Fail before writing on an undeclared collision, malformed manifest, or template symlink; never follow a destination symlink.
4. Render only well-defined neutral tokens such as `__PROJECT_NAME__`, `__PROJECT_SLUG__`, and `__PROJECT_PURPOSE__`. Reject control characters in CLI metadata, use context-appropriate escaping/serialization for every rendered format, restrict the slug to a safe filename/config identifier, and fail if any token remains unresolved.
5. For each `--module`, validate the relative module path, create it beneath the generated project, and copy both `assets/module/context.md` and `assets/module/API.md`. Asset templates are not copied by an overlay.
6. Seed the selected portable skills into their loadable project-local locations by default: cleaned Claude commands to `.claude/commands/<name>.md` and Codex skills to `.agents/skills/<name>/SKILL.md`. Provide `--skill-surface claude|codex|both` (default `both`) and generate an inventory explaining those local locations; never alter global skill directories.
7. Emit a short, evidence-based completion report: selected overlays, created files, skipped/conflicted files, and verification results.

## Implementation steps and ownership

### 1. Establish package instructions and public entry points

**Owner:** integration worker

- Add package-level `AGENTS.md` for maintaining this repository, plus `README.md` with installation-free usage, composition examples, extension guidance, and the distinction between package files and generated project files.
- Implement `bin/initialize-project` and a compact, validated manifest format that defines each overlay's identity, dependencies, and allowed overrides. Do not implement executable post-copy hooks in version 1.
- Keep the initializer standard-library/dependency-free so it remains quick to port. Validate every input, manifest, source path, collision, token render, and final output path before modifying a destination; abort without a partially initialized project on preflight failure.

### 2. Create the compact generated root `AGENTS.md` and separate governance policy

**Owner:** documentation-policy worker

- Add `templates/base/AGENTS.md` with a short project metadata/bootstrap section and portable working rules: inspect before modifying, minimal scope, use subagents liberally, WIP≈1 when practical, evidence-bound claims, test/verification honesty, documentation synchronization, decision/deviation logging, and terminal-gated destructive/external actions.
- Define evidence-bound claims precisely: distinguish observed repository facts, source-backed external claims, and inferences; cite repository paths/commands for material assertions; use authoritative sources for time-sensitive external claims; state unverified gaps explicitly.
- Make UI documentation proportional: require a state graph or equivalent transition artifact only for stateful/multi-step UI flows where it improves testability; otherwise describe only meaningful states and acceptance checks.
- Add `templates/base/docs/governance/git-policy.md` with the simplified policy: classify change risk, use a focused branch/PR, run declared checks, confirm CI green before merge, never treat auto-merge as proof of checks, and require operator approval for consequential/security/data-contract changes. Leave hosting-plan mechanics and repository-specific `gh` commands out.
- Link governance from the generated `AGENTS.md` rather than duplicating it.

### 3. Build reusable base scaffolding from stripped Retinology structures

**Owner:** template worker

- Add content-free base templates: `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `/logs/.gitkeep`, `docs/development/{INDEX.md,NEXT_STEPS.md,TECH_DEBT.md}`, `docs/architecture/ADR-template.md`, `docs/governance/git-policy.md`, `learnings/learnings.json`, `learnings/PITFALLS.md`, and portable documentation-check scripts.
- Add `assets/module/{context.md,API.md}`, outside every overlay root. The generated `context.md` is concise navigation/ownership/pitfall guidance; `API.md` is always generated and covers public contract, inputs/outputs, errors, examples, and dependencies.
- Either seed a portable `learnings/scripts/regenerate_md.py` derived from the required neutral ledger schema or revise both captured-learning skills to update the compact pitfall summary directly. Test the chosen contract end-to-end; do not ship a skill that invokes a missing script.
- Do not place individual execution plans in the template; archive policy and current-plan references remain generic.

### 4. Add composable deployment-target overlays

**Owner:** template worker (disjoint from base files except manifest registration)

- Add `templates/python` with language-appropriate package/test/config skeletons, module layout compatible with the common module-doc contract, and a short Python addendum rather than Python rules in the base agent guide.
- Add `templates/web` with an intentionally framework-neutral application/test/static layout and a UI verification addendum. Avoid choosing React, FastAPI, or a deployment provider without a user request.
- Add `templates/regulated-health` with additive safety/governance documentation, a no-sensitive-data rule, evidence/provenance expectations, a decision log, and a configurable sensitive-content scan placeholder. Keep this overlay domain-neutral; it must not claim regulatory compliance or reproduce Retinology clinical architecture.
- Define collisions/precedence in manifests so `python + regulated-health`, `web + regulated-health`, and base-only are deterministic and tested.

### 5. Preserve and port portable skills in a dedicated folder

**Owner:** skills worker

- Add `skills/README.md` that maps each source asset to the neutral Claude and Codex variants and explains that these package sources are installed into generated projects' local loader paths, never into global user skill directories.
- Preserve cleaned Claude command variants in `skills/claude/` for `module-docs`, `capture-learning`, `implement-v11`, `serial-implement`, `local-review`, and `local-review-pr`. Add the command front matter and filenames required by Claude's project-local command loader. Remove all Retinology paths, clinical policies, user identities, dated history, and tool/model-specific assumptions that cannot travel.
- Add `skills/codex/<name>/SKILL.md` equivalents. Adapt command front matter and Claude team/task APIs to Codex skill metadata and collaboration tools; retain durable file contracts (`docs/development/current_implementation_checkpoint.json`, current plan, decision/queue records) where they are useful.
- Use the current Codex `implement-v11` and `serial-implement` skills as the authoritative port baseline, not the older Retinology command syntax. Make review skills evidence-first and require source/line verification of material findings.
- Do not include Retinology's obsolete `implement-v6`, `v8`, `v9`, or `v10` variants, `.claude/hooks`, `.claude/settings.json`, or `plan-reviewer.md`; each is either superseded or tied to that repository's local enforcement/model configuration.

### 6. Validate the initializer and its generated outputs

**Owner:** integration worker after the above work is merged

- Add focused automated tests that initialize temporary directories and assert: base files/tokens, `/logs/`, documentation checks, loadable Claude (`.claude/commands/*.md`) and Codex (`.agents/skills/*/SKILL.md`) skill locations and inventory, a module's `context.md` + `API.md`, valid empty learning JSON, the capture-learning regeneration/direct-update contract, and no unresolved template tokens.
- Add composition tests for `python + regulated-health` and `web + regulated-health`, including manifest precedence/collision behavior. Test refusal of invalid input and non-empty targets.
- Add security-focused tests for target/template symlinks, module traversal, unsafe metadata/control characters, undeclared collisions, and token values embedded in every supported rendered format.
- Run the initializer in a temporary directory for each supported combination; execute portable doc-status/link checks against the generated fixtures; parse all JSON/manifests; run shell syntax checks for the launcher.

## Runtime and data contracts

| Contract | Verification |
|---|---|
| Base plus multiple selected overlays is deterministic and collision-safe. | Test generated file lists/content for `python + regulated-health` and `web + regulated-health`; force an undeclared collision and expect failure. |
| Every generated module has `context.md` and `API.md`. | Initialize with at least one module and assert both paths and required headings. |
| Generated root guidance is compact, portable, and links to the external Git policy. | Text assertions: required evidence/verification/subagent/UI-flexibility wording; no Retinology-specific terms; link resolves. |
| Generated docs have valid status/link policy and template data is valid. | Run seeded doc checkers and JSON parser over generated projects. |
| Skills are separately maintained yet loaded by generated projects. | Check package source pairs, generated `.claude/commands/*.md`, generated `.agents/skills/*/SKILL.md`, appropriate metadata/front matter, and no source-only Retinology paths/tokens. |
| Initializer never silently destroys existing work or escapes its target. | Test a pre-existing/non-empty/symlink target fails before writes, template and destination symlinks fail, traversal is rejected, and all generated paths resolve under the target. |
| Portable learning capture has an executable supporting contract. | Run the selected regenerate/direct-update path on the empty ledger and assert valid JSON, a stable pitfall summary, and no Retinology-specific content. |

## Risks and decisions

- **Packaging interface:** no packaging tool or language runtime was requested. The plan chooses a portable command-line package using only the standard shell/Python facilities already present. If this cannot be implemented without requiring a runtime, document the requirement and avoid hidden installation steps.
- **Template composition:** overlay collision behavior is the main correctness risk. A manifest with explicit override declarations is safer than copy-order-only semantics and keeps future overlays auditable.
- **Write and rendering safety:** a template initializer is a filesystem boundary. Version 1 intentionally rejects existing/symlinked targets and contains every resolved output path beneath the new target. Metadata rendering must be format-aware; a naïve text substitution could corrupt JSON/configuration or create an unsafe generated file.
- **Skill portability:** Retinology's Claude commands contain repository names, local hook policies, and Claude tool syntax. Direct copies would be misleading. Preserve their intent as cleaned Claude assets and intentionally port Codex equivalents.
- **Skill loading:** package source folders alone are inert. The initializer must place the selected variants in each surface's project-local loader path and test that layout, while leaving global skill installations untouched.
- **Learning capture:** the Retinology command invokes a pitfall regeneration script. The neutral variant must ship an equivalent portable utility or remove that invocation consistently; otherwise a generated skill has a broken runtime dependency.
- **Regulated-health overlay:** it must encourage safety/evidence/provenance but cannot assert HIPAA, medical-device, or other compliance without a scoped legal/regulatory program.
- **No Git repository exists here:** do not assume a branch, PR, CI provider, or merge tool. The generated Git policy must describe portable principles and project-specific extension points.

## Completion criteria

1. A user can initialize a clean target with base, `python + regulated-health`, or `web + regulated-health` using documented commands.
2. Generated projects include the compact `AGENTS.md`, separate `docs/governance/git-policy.md`, `/logs/`, base documentation/learning skeletons, and required module docs.
3. The named skills and both relevant review skills exist in separate Claude/Codex package folders, are installed into their selected project-local loader paths, and contain no Retinology-specific dependencies.
4. All declared automated and manual validation passes with recorded output.
