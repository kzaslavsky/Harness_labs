# JSON phase-flow contract

## Scope and authority

Use one validated `flow.json` as the authority for child phase order, prompts,
declared context selection, model, reasoning, sandbox, output schema, and child
receipt policy. Keep the outer feature checkpoint, repository gates, Git
transaction, merge proof, and immutable feature result authoritative in
`implement-v13-codex`.

The generic runner must not inspect or update a serial queue. A dispatcher
payload is immutable run metadata. Only the production feature coordinator may
turn a successful project flow into repository gate and integration evidence.

## Flow contract

Require protocol `codex-phase-flow/1`, a stable flow ID, prompt catalog, and
ordered phases. An omitted context catalog is equivalent to an empty one.
Reject duplicate JSON keys before schema
validation. Core objects are closed to unknown fields unless an explicit
extension namespace is defined.

Each phase identifies a prompt, model, reasoning effort, sandbox, and output
schema. Reasoning effort is restricted to `low` or `medium`; higher values are
schema-invalid and may not launch. The prompt entry lists exact context IDs. The
runner resolves only those IDs and never adds repository documents, skills, or
chat history implicitly.

Keep these concepts separate:

- declared context inserted into a prompt;
- workspace access granted by the sandbox and working directory;
- immutable run and dispatch metadata;
- child-authored schema-bound output;
- controller-owned prompts, hashes, events, receipts, and terminal result.

## Derived modes

Derive mode from the context catalog; never accept a caller mode label.

- An absent or empty catalog means `debug` with certification scope
  `orchestration_only`.
- A nonempty catalog means `project`.
- Reject any attempt to produce a project result from an empty catalog.

Debug mode uses a caller-selected new mode-0700 run directory outside every
repository (enforced by Git/instruction-root ancestor checks), an empty private
workspace, a built-in neutral identity-marker
prompt, no repository or planning reads, no Git, and no command execution. The
only allowed tool evidence is one `file_change` for the identity marker. Its
result protocol is distinct from production and cannot satisfy queue
acknowledgment.

## Context resolution

Resolve project paths beneath the declared project root without following a
symlink or traversal escape. External snapshots require explicit authorization,
a declared digest, and a mode-0600 frozen copy in the artifact root.

Validate regular-file type, UTF-8, and configured byte limit. Freeze path, role,
size, and SHA-256 in `resolved-context.json` before launching the first child.
Revalidate each selected input before a dependent phase and on resume. Never put
raw context in normal logs or receipts.

## Prompt compilation

Use a non-executable template language limited to a closed placeholder set such
as `task`, `phase`, `phase_detail`, `run_id`, `context_bundle`, and
`output_contract`. Reject missing and unknown placeholders. Insert selected
context once in stable ID order with ID, role, path, and hash delimiters.

Persist and hash the byte-identical compiled prompt. Receipts record flow,
template, compiled-prompt, resolved-context, schema, executable, and output
hashes plus selected context IDs and total bytes.

## Process and recovery

Before creating the run directory or checkpoint, validate controller dependencies,
flow schemas, support-file hashes, private auth-file permissions, Codex executable
identity, and declared model names. Persist the successful preflight receipt when
the run starts. Treat the first child as the live availability probe; the CLI does
not expose exact remaining quota, so never describe preflight as a quota guarantee.

Use one generic supervised child runner. Require fresh threads, owned process
groups, process fingerprints, wall timeouts, `thread.started`,
`turn.completed`, no terminal error event, and schema plus semantic validation.
Persist `prepared -> running -> succeeded|failed` receipts and reconcile the
crash window between terminal receipt and checkpoint advancement.

Support start, bounded stop, resume, read-only verify, and inspect. Resume rejects
changed flow, prompt, context, schema, executable, or version evidence. Phase
order must come from the frozen flow, never a Python catalog.

## Skill-ingestion isolation

Neutral protocol and role names reduce accidental skill triggering but do not
prove isolation. `--ignore-user-config` skips user configuration but does not
disable skill discovery. The implemented debug runner creates private empty
`HOME`, `CODEX_HOME`, XDG, temporary, and workspace directories; exposes only the existing
mode-0600 authentication file through a private symlink; disables plugins,
bundled skills, skill instruction injection, rules, and project-document
loading; passes a closed environment allowlist rather than controller secrets;
and invokes Codex with an ephemeral session. Raw stdout, stderr, child output,
and the marker stay in the private runtime until the audit succeeds. Failed raw
bytes are deleted rather than promoted to durable logs. The runner removes the
private runtime after success or failure and never hashes, copies into
artifacts, or logs credential bytes.

Debug validation rejects every `command_execution`, repository or external read
evidence, skill-loading statements, unexpected item types or file changes,
nonempty stderr, reused threads, and raw context. The prompt, final output,
archived marker, JSONL, empty stderr, schema, executable, flow, and receipt are
hashed and revalidated. A persisted secret-free launch contract binds the exact
arguments, environment-policy digest, controller hash, supervisor hash, and
working directory. The process's sole file operation is the expected
`apply_patch` marker change.

## Implemented boundary and remaining gates

Implemented for debug mode: schemas, duplicate-key rejection, derived mode,
neutral deterministic prompt compilation, a JSON-owned 32-unit catalog,
isolated fresh children, gated `prepared -> spawned_unconfirmed -> running ->
succeeded|failed` receipts, owned process-group recovery, stop/resume reconciliation,
read-only verify, inspect, artifact corruption tests, and a protocol-distinct
result.

Live certification on 2026-07-20 exercised start, absolute stop after unit 1,
resume, all 32 fresh children, completion, independent verify, and log
inspection. Evidence showed 32 distinct threads, 32 expected marker changes,
zero commands, zero document/discovery evidence, zero unexpected items, empty
stderr, no retries, and no remaining private runtime. Usage was 732,036 input
tokens (579,840 cached) and 7,162 output tokens.

Remaining before project mode: confined context freezing, project templates and
schemas, unrelated project-flow fixtures, outer feature-checkpoint integration,
and full repository/Git gate proof. The runner rejects project execution until
those contracts exist. Keep the legacy synthetic driver only as a regression
fixture during that work.

Event validation is necessary but not sufficient evidence of absent implicit
skill ingestion. Do not weaken this gate to complete the migration.
