---
description: Capture an evidence-backed reusable learning in the project learning log
argument-hint: [optional focus]
---

# Capture learning

Review the current task, diff, tests, and recent history for a reusable lesson. Do
not create an entry for routine work or speculation.

1. Read `learnings/learnings.json` and its existing schema. If it is absent, report
   the missing project prerequisite rather than inventing a schema.
2. Capture only a specific, actionable learning with an observed problem, its cause,
   a verified solution, tags, affected files when relevant, and the supporting
   validation/evidence.
3. Remove secrets, personal data, customer data, and source excerpts that should not
   be retained.
4. Generate a stable, lowercase hyphenated identifier and append the entry without
   changing unrelated entries.
5. Run `python3 learnings/scripts/regenerate_md.py` and report its result. If the
   script is unavailable or fails, preserve the JSON update, state the gap, and do
   not claim generated documentation is current.

Report the entry ID, files changed, evidence recorded, and whether regeneration
succeeded.
