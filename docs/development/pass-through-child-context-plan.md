# Pass-through child context implementation plan

Status: implemented
Date: 2026-08-03
Decision: [ADR 0003](../decisions/0003-pass-through-child-context.md)

## Repository identity

- Feature worktree:
  `/Users/kirillzaslavsky/Documents/harness_labs-pass-through-context`
- Feature branch: `codex/pass-through-child-context`
- Base branch: `codex/parallel-child-dispatch`
- Base commit: `d8c52bed444842261dfe73b224d719ad7a101c15`

## Objective

Let a parent supply the smallest useful task-specific context to a child while
the controller initially acts as a transparent transport.

## Acceptance criteria

1. `ChildRequest` carries a string context and the child `TaskAttempt` receives
   exactly the same bytes.
2. Single, retained, and parallel child tool calls carry context through native
   and oMLX-emulated transports.
3. The controller does not select, resolve, rewrite, or authorize the string.
4. Audit evidence records the exact supplied context as an artifact and binds
   it by SHA-256 in structured events.
5. A locator fixture hides the treasure path. The parent receives the locator
   path and instructions, passes them to a Codex child, and the child reads the
   locator before reading and returning `there is booty here`.
6. A backend without the required read capability still returns the established
   refusal.
7. All pre-existing unit tests continue to pass.

## Verification

Run the complete unit suite, then run the live Codex-parent/Codex-child treasure
scenario and verify the resulting audit journal. Exercise oMLX through unit
transport tests; run the live oMLX combination when its local server is
available.

## Verification result

- `python3 scripts/check_repository_contracts.py`: passed.
- `python3 -m unittest discover -s tests`: 64 passed.
- Live Codex parent → Codex child: succeeded in 26.8 seconds, returned
  `there is booty here`, retained the child for the follow-up, and terminated
  it afterward.
- Live run:
  `logs/runs/20260803T193742-codex-to-codex-5d740f67`
- Independent journal verification: 73 events, head
  `dc67a0dd1c7a38d3ffe84a268e638f4c11d71f333f2368d44b82c28873bcca48`.
- The archived parent context and child-dispatch context matched exactly. The
  child prompt contained the locator path and did not contain
  `treasure_chest.txt`.
- Live oMLX was not rerun because no server was listening on loopback port 8100;
  the oMLX individual, retained, and parallel context transports passed their
  component tests.
