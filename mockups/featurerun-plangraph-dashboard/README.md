# React Flow FeatureRun + PlanGraph dashboard

Status: design mockup

This Vite/React prototype explores a dark, operator-first dashboard built on
React Flow (`@xyflow/react`). The dependency graph is the primary workspace;
selecting any node opens its corresponding FeatureRun inspector.

## Acceptance criteria

- Render the PlanGraph with genuine React Flow nodes, edges, zoom, pan, fit-view,
  controls, and minimap behavior.
- Make program state, dependency flow, blocking conditions, and the active run
  understandable without opening raw logs.
- Keep evidence provenance explicit: availability, source, integrity, and
  production/synthetic classification must never be visually conflated.
- Let an operator select any graph node and inspect that FeatureRun's overview,
  lifecycle, acceptance criteria, activity, evidence, and Git custody without
  losing graph context.
- Model the repository lifecycle `orient -> plan -> implement -> verify ->
  review -> integrate -> report`, including retries, queued work, and failure
  propagation.
- Provide a polished fixed dark theme with responsive behavior down to mobile
  widths and keyboard-accessible native controls.
- Use realistic, clearly fictional sample data. This mockup does not read or
  mutate live run state.

## Preview

Use Node.js 20.19+ or 22.12+ (Node 21 is unsupported by Vite).

```sh
npm install
npm run dev
```

The graph nodes, viewport controls, minimap, filters, FeatureRun ledger, and
inspector tabs are interactive. All displayed data is fictional and local.
