# omp `/te`: a user-only overlay panel

omp stays the native host of ADR 0001; this record covers only what `/te` shows and where. The
command must reach the person without becoming a session entry, because omp replays entries into
the model's context and bills them on every later turn.

**The overlay, not a message.** `/te` renders in `ctx.ui.custom({ overlay: true })` as a scrollable
Markdown panel, and never calls `sendMessage`. A user-only panel leaves zero session entries and
zero context tokens. Rejected: `notify` (a 2.4s toast, coalesced, plain text, no links) and
`sendMessage` (persists a `CustomMessageEntry` into the model's context, so every later turn pays
for it again).

**Progressive disclosure.** Bare `/te` is the full index overlay; a subcommand shows its own result
and that subcommand's next actions only. Hover tips stay per-command and contextual.

**Two config links, no dump.** `/te config` shows exactly two entries, the repository's
`config.json` and the global one, and never the captured stdout the CLI prints for other hosts.
Each entry is a Markdown link, labelled `config.json (this repo)` and `config.json (global)`, plus
the plain full Windows path, because the terminal must resolve the path without the link renderer;
an absent file adds `(not created yet — config set writes it)`. One trailing line names which file
`config set` writes: the repository's, or the global one with `--global`.

## Considered Options

- `notify`: a toast disappears in 2.4s, coalesces repeated output, and renders plain text with no
  links, so the index and the config path cannot be read or copied.
- `sendMessage`: always persists a `CustomMessageEntry` into the LLM context, which bills tokens on
  every turn for a command whose whole point is to save them.
