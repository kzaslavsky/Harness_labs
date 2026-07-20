# __PROJECT_NAME__ — Agent Context

**Status:** Initializing
**Purpose:** __PROJECT_PURPOSE__

## Bootstrap

When this project is still initializing: inspect the repository, confirm the
name and purpose above, select only the needed templates, then replace this
section with current project navigation and verification commands.

## Working rules

- Inspect before modifying; preserve unrelated work and keep changes minimal.
- Use subagents liberally for independent research, review, and parallel work.
  Keep work in progress near one unit when practical.
- Treat remembered information and assumptions as unverified. Distinguish
  observed repository facts, source-backed external claims, and inferences.
  Cite material repository claims with paths or commands; use authoritative
  sources for time-sensitive external claims; state verification gaps plainly.
- Do not call work complete without relevant verification. Report what ran,
  its result, and remaining limitations. Inspection is not a substitute for a
  test unless the request explicitly calls for inspection.
- Keep documentation and status records synchronized. Record material
  decisions, failures, deviations, and deliberate deferrals in the relevant
  development record.
- For stateful or multi-step UI flows, add a state graph or equivalent
  transition artifact when it improves testability. Otherwise document the
  meaningful states and acceptance checks only.
- Destructive, external, security-sensitive, and data-contract actions require
  explicit operator authority. Do not bypass a safety control.

## Navigation

- Current work: [docs/development/NEXT_STEPS.md](docs/development/NEXT_STEPS.md)
- Plans and work records: [docs/development/INDEX.md](docs/development/INDEX.md)
- Technical debt: [docs/development/TECH_DEBT.md](docs/development/TECH_DEBT.md)
- Reusable lessons: [learnings/PITFALLS.md](learnings/PITFALLS.md)
- Git and PR practice: [docs/governance/git-policy.md](docs/governance/git-policy.md)

## Checks

Run the checks declared by the selected template(s). When present, run:

```bash
python3 scripts/check_doc_status.py
python3 scripts/check_doc_links.py
```
