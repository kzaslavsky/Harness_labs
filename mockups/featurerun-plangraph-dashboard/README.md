# FeatureRun + PlanGraph dashboard mockup

Status: design mockup

This standalone prototype explores a dark, operator-first dashboard that joins
PlanGraph program orchestration with the evidence and execution detail of a
single FeatureRun.

## Acceptance criteria

- Make program state, dependency flow, blocking conditions, and the active run
  understandable without opening raw logs.
- Keep evidence provenance explicit: availability, source, integrity, and
  production/synthetic classification must never be visually conflated.
- Let an operator move from graph overview to run details, gates, agent work,
  events, Git state, and usage without losing the selected-run context.
- Model the repository lifecycle `orient -> plan -> implement -> verify ->
  review -> integrate -> report`, including retries, queued work, and failure
  propagation.
- Provide a polished fixed dark theme with responsive behavior down to mobile
  widths and keyboard-accessible native controls.
- Use realistic, clearly fictional sample data. This mockup does not read or
  mutate live run state.

## Preview

Open `index.html` directly in a browser. The graph nodes, run table rows,
sidebar sections, and evidence tabs are interactive.

