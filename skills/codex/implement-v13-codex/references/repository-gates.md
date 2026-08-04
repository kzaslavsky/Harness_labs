# Repository gates

Repository instructions override generic defaults. Discover exact commands
from the target tree and record structured argv, cwd, environment policy, tree
identity, counts, and exit status. Python/pytest closure commands use the
absolute interpreter and runtime hash certified by `capability-manifest/2`;
generic non-pytest repository argv remain exact. The controller executes each
selected command once through the manifest-bound host broker and records the
`production_certification` receipt; reviewers do not rerun the command.

At minimum require:

- the repository's certification test suite with zero failures/errors and no unexplained pass-count reduction;
- PHI/secret and security gates;
- import/layer boundary gates;
- documentation status, links, decision records, and required generated-artifact checks;
- the archived Markdown plan's exact decision-record backlink via
  `scripts/validate_plan_decision_link.py`;
- smoke B on the final tree;
- a real browser walk for UI-impacting changes;
- no unresolved blocking review findings;
- clean, policy-authorized Git staging/commit/merge proof.

Do not reinterpret a required skip as pass. Diagnostic system-interpreter runs never replace the declared certification environment. Never expose production roots or real clinical data to tests.
