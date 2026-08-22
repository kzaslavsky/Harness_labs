# Committed improvement artifacts

Status: active

This tree holds only self-improvement-agent artifacts an operator has
already reviewed: accepted `improvement-proposal/1` records
(`proposals/`) and the `blocker-pattern/1` records they cite
(`patterns/`), sanitized and hash-referencing local journals. Mining
state, unaccepted drafts, and per-campaign ledgers stay local under the
gitignored `logs/improvement/` — see `self-improvement-agent-plan.md`
(SI-00) and `self-improvement-agent-guide.md` for the full lifecycle.

Every `*.json` file under this directory is validated by
`scripts/dev/check_improvement_artifacts.py` (exit 0 required); run it, or
`python3 -m pytest tests/test_improvement_closeout.py -q`, before adding
or editing anything here.
