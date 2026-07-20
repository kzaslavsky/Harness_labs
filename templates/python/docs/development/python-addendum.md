# Python addendum

**Status:** Current

## Layout

- Put importable code in `src/`.
- Put behavior-focused tests in `tests/`.
- Create module documentation with `initialize-project --module`; every module
  must retain both `context.md` and `API.md`.

## Verification

Use an isolated environment and run the project’s declared formatter, linter,
type checker, and tests. Start with:

```bash
python3 -m pytest
```

Add project-specific commands to the root README and agent guidance once chosen.
