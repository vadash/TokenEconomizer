# Architecture

The three mechanisms (see the [main README](../README.md)) know neither the agent
nor the task domain. Everything specific to those enters through a few small interfaces in the
standard-library-only package `tokeneconomizer`:

- `tokeneconomizer.trace` is the one place a saved session log is read (`load_trajectory`,
  `call_cwds`, `tool_usage`, `parse_catalog`), with one module per log shape. A new log shape
  needs a reader here and nothing else.
- `tokeneconomizer.ledger` and `tokeneconomizer.pricing` are the one definition of cost.
  Mechanisms record their per-task overhead (in the plugin, a session counts as one task); the
  plugin's session ledger reads it back through the same modules, so recorded and reported cost
  cannot drift.
- `tokeneconomizer.domain.DomainAdapter` is the interface to the task domain: reading a session,
  the tools pruning protects or excludes, a trajectory's loop signals, the task text for the
  optional judge, and the micro-skill vocabulary. A new domain is a new adapter, with no change
  to a mechanism. The plugin's adapter is `tokeneconomizer.plugin.adapter.LiveAdapter`.
- `tokeneconomizer.stage.Stage` is the set of hooks a mechanism offers whoever runs it:
  `before_task`, `build_tools`, `run_kwargs` and `after_task`.

One caller runs the stages, and the mechanisms import it never:

- **The plugin** (`tokeneconomizer.plugin`) runs them inside a person's sessions through omp's
  events: `PrefixStage` and `ToolingStage` over each finished session, and the loop guard
  (`loopguard.TurnGuard`) one tool call at a time. What happens at each event, and what was
  verified on omp, is in [`PLUGIN.md`](PLUGIN.md).

`tests/test_plugin_package.py` pins the boundary: the mechanisms never import
`tokeneconomizer.plugin`.
