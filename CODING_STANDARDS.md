# Coding standards

Read during review, not implementation. The reviewer gets the diff, so no
exploration is needed — which makes the reviewer, not the implementer,
responsible for imposing these.

- Dead-code claims carry proof: every "delete X / zero uses" finding cites the
  exact repo-wide grep and its zero-hit output (paths searched plus the empty
  result). A scoped line range is not evidence.
