# UI verification addendum

**Status:** Current

## Verification expectations

Verify user-visible behavior in a browser-capable environment when the project
has a runnable UI. Exercise meaningful loading, success, empty, error, and
permission states as applicable; retain reproducible evidence such as test
output, screenshots, or recorded steps.

For stateful or multi-step flows, add a state graph or equivalent transition
artifact when it materially improves test coverage. Simple views need only their
meaningful states and acceptance checks.

## Framework selection

This template intentionally selects no UI framework, bundler, or host. Document
those choices and their verification commands before adding implementation code.
