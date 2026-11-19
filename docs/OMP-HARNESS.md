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

- `packages/coding-agent/src/extensibility/extensions/types.ts:321-427` (custom/notify surface)
- `packages/coding-agent/src/modes/controllers/extension-ui-controller.ts:1322-1403` (overlay),
  `:1309-1317` (notify dispatch)
- `packages/tui/src/chat/custom-message.ts` (transcript Markdown fallback)
- `packages/tui/src/components/markdown.ts:4303` (explicit-link OSC 8 case)
- `packages/coding-agent/src/extensibility/plugins/legacy-pi-compat.ts:825,1046`
  (`pi-tui` remap — `ScrollView` imports from `@oh-my-pi/pi-tui`)
- `packages/coding-agent/src/modes/interactive-mode.ts:8771` (status-notice path)

Gotchas:

- uv/pipx Windows shims are binary `.exe` with no `#!`, so shebang-sniffing
  (`tokeneconomizer/plugin/repomap.py:_aider_command_python`) misses them — prefer an
  explicit config path (`repomap_python`).
- `te` CLI wraps long lines to the ambient terminal width (`COLUMNS`, fallback 80):
  never assert a multi-word phrase across a wrapped line without normalizing whitespace.
