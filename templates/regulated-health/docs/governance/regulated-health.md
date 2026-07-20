# Regulated-health addendum

**Status:** Current

This additive template supports disciplined health-related development. It does
not itself establish legal, regulatory, privacy, security, clinical-safety, or
medical-device compliance.

## Safety and sensitive content

- Do not place real patient, participant, customer, or other sensitive data in
  source, tests, fixtures, screenshots, logs, examples, commits, or public issue
  trackers. Use synthetic data.
- Define project-specific data classes, retention, access, and disclosure rules
  before handling sensitive content.
- Preserve provenance for material clinical, scientific, safety, or regulatory
  claims. Separate observed facts, source-backed claims, and inferences.
- Escalate unresolved safety, privacy, security, or data-contract decisions;
  never silently treat them as implementation detail.

## Evidence and review

Record safety-relevant decisions and deviations in
[the decision log](../development/DECISION_LOG.md). Identify the applicable
requirements, verification evidence, reviewer/approver, and any remaining
limitations. Add a scoped regulatory program only after the organization defines
the jurisdiction, intended use, and accountable roles.

## Sensitive-content scan

Configure `config/sensitive-content-scan.json` before relying on
`scripts/scan_sensitive_content.py`. The supplied configuration is intentionally
disabled and contains no project-specific detection rules.
