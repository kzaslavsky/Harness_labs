# Boris Cherny — Autonomous Codebase-Maintenance Routines

Source: https://x.com/bcherny/status/2088014489438621990 (thread by Boris
Cherny). Transcribed 2026-08-14 from a screenshot of the thread's table; the
thread itself could not be fetched from this environment (x.com blocked by the
network egress proxy), so the table below may be a truncated excerpt — the
screenshot cuts off after the last row shown here.

## The routines

| Routine | What it does |
| --- | --- |
| Crash fuzzer | Finds real-app crashes and opens root-cause fixes |
| Ant-only shipper | Ships or deletes forgotten internal-only features based on usage |
| Logic simplifier | Simplifies convoluted business logic |
| Logic bugfixer | Models tricky logic to find and fix bugs |
| Dup unifier | Merges duplicated implementations into one |
| Dead-code removal | Deletes provably unreachable code |
| Useless-test pruner | Deletes tests that can't fail |
| Shipped-feature inliner | Removes flags for fully shipped features |
| Flaky-test fixer | Root-causes flaky CI tests |
| Abstraction improver | Flattens over-engineered abstractions |
| Abstraction police | Fixes layering violations |

Notes on terminology: "Ant-only" refers to features visible only to Anthropic
employees ("ants") — i.e., internal-only features that shipped behind an
internal gate and were then forgotten.

## Reading of the list

Each routine is a standing background agent with (a) a narrow, nameable class
of maintenance debt, (b) an implicit oracle for "done" (crash reproduced and
fixed; code provably unreachable; test cannot fail; flag fully rolled out), and
(c) a pull request as the unit of output. The common design move is to convert
open-ended "improve the codebase" into closed, verifiable predicates an agent
can check before opening a PR.

See `automation-routines-analysis.md` in this directory for critique,
speculated implementations, and applicability to Retinology and Harness Labs.
