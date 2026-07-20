---
name: capture-learning
description: Capture a reusable, evidence-backed engineering learning in a project's learning log and regenerate derived documentation. Use after discovering a significant pitfall, failure mode, or validated practice worth preserving.
---

# Capture learning

Record only a specific, reusable lesson. Do not create entries for routine work or
unverified speculation.

1. Read `learnings/learnings.json` and preserve its schema. If it is absent, report
   the missing prerequisite rather than inventing one.
2. Record the observed problem, cause, verified solution, tags, affected files when
   relevant, and evidence/validation. Use a stable lowercase hyphenated ID.
3. Remove secrets, personal data, customer data, and inappropriate source excerpts.
4. Append the entry without modifying unrelated records.
5. Run `python3 learnings/scripts/regenerate_md.py`. If unavailable or failing,
   report that gap and do not claim derived documentation is current.

Report the entry ID, changed files, evidence captured, and regeneration result.
