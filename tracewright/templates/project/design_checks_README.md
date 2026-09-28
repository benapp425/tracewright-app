# design/checks/

Project-specific checks, loaded by `./tw check` along with the built-in ones. Each file registers checks
with `@check(...)` (see `.claude/tracewright.md`). Every check needs a planted-fault test
(`test_<name>.py`) that breaks the design on purpose and asserts the check catches it -- a check that
cannot fail is not evidence.
