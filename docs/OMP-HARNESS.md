# omp display channel

`/te` output must reach the person without becoming a session entry.

- `ctx.ui.notify(text)` — user-only, zero tokens. Plain `Text`, one 2.4s toast,
  back-to-back calls coalesce. No Markdown, no links. Right for one-line notices, wrong for documents.
- `api.sendMessage({ display: true }, { deliverAs: "steer" })` — ALWAYS a `CustomMessageEntry`,
  billed on every later turn. Never a display channel for `/te`.
- `ctx.ui.custom(factory, { overlay: true })` — user-only overlay, zero entries, scrollable,
  full Markdown. The `/te` sink. One call per invocation; fire-and-forget with close-on-keypress.

Link rule: only explicit `[label](file://…)` links render as OSC 8; bare paths and codespans
stay plain. Emit both: the link line plus the plain full Windows path line (never `~/`).

Anchors (oh-my-pi, read-only):

- `packages/coding-agent/src/extensibility/extensions/types.ts:288-313` (custom factory/options)
- `packages/coding-agent/src/extensibility/extensions/render-types.ts:14-27` (plain-rows contract)
- `packages/coding-agent/src/modes/controllers/extension-ui-controller.ts:1609` (overlay host)
- `packages/tui/src/text/markdown/render.ts:39-82,815` (renderer options and entry point)
- `packages/tui/src/text/markdown/inline.ts:154-171` (explicit-link OSC 8 case)
- `packages/coding-agent/src/extensibility/plugins/legacy-pi-compat.ts:1026-1045,1153-1158`
  (compiled extensions resolve canonical imports through the host bundle)

Gotchas:

- omp 19.2 removed the `getMarkdownTheme`, `Markdown` and `ScrollView` extension imports.
  Use `renderMarkdown` from `@oh-my-pi/pi-tui/text/markdown/render` and return an
  `ExtensionRows` object from `ctx.ui.custom`; pass the factory's theme to the renderer.

- uv/pipx Windows shims are binary `.exe` with no `#!`, so shebang-sniffing
  (`tokeneconomizer/plugin/repomap.py:_aider_command_python`) misses them — prefer an
  explicit config path (`repomap_python`).
- `te` CLI wraps long lines to the ambient terminal width (`COLUMNS`, fallback 80):
  never assert a multi-word phrase across a wrapped line without normalizing whitespace.
