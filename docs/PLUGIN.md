# The plugin: design and verified omp behaviour

How the plugin runs the three mechanisms inside your own omp sessions, through the events the
extension exposes: what it does at each event, what differs from the experimental setting and
why, where its state lives, and its known limitations. The code is `tokeneconomizer/plugin/`
(what the dispatcher runs) and `plugin/` (the omp packaging). To install and use it, see
[`plugin/README.md`](../plugin/README.md).

---

## 1. What it does, event by event

omp has no hook protocol: the TypeScript extension (`plugin/omp/tokeneconomizer.ts`) speaks the
dispatcher's contract on omp's behalf, and applies each answer with omp's own levers.

| omp event | Dispatcher call | What the plugin does |
|---|---|---|
| `session_start` | `SessionStart` | Injects the repository's micro-skills (a cached render) as `additionalContext`, with the tools to remove, which the extension takes out of the active set; announces changed facts, tools the deny list newly left out, or a proposal still waiting under `toolprune=propose` (or when `auto` could not write the file), each in one line; folds any earlier session whose end was never seen |
| `session_switch` | `Stop`, then `SessionStart` | Closes the old turn, then starts the new session as above |
| `before_agent_start` | `UserPromptSubmit` | Opens a prompt-turn for the loop guard, and re-sends the manual as a system-prompt block (omp keeps only a digest of its system prompt, so the extension sends it on every prompt) |
| `tool_result` | `PostToolUse` | Folds the call into the turn's guard; delivers a redirect as a steering message, then, if the turn is still stuck, a one-line notice through `ui.notify`, only when the session is the main one |
| `agent_end` | `Stop` | Closes the turn; a turn that *ended green* feeds the per-repository envelope |
| `session_shutdown` | `SessionEnd` | Hands the session to a detached `te fold` and returns at once |

`te fold` runs the **same stages over the finished session's log**: `PrefixStage` (with
micro-skills on, the free envfacts layer under the proposer) and `ToolingStage` (evidence
pruning). The loop guard (`loopguard.TurnGuard`) folds one tool call at a time. Three things
the interactive setting adds:

* It counts **edits made through the shell**: `sed -i`, `perl -pi`, a redirect or `tee` into a
  source file, an inline Python script that writes a file back, `patch`/`git apply`. The loop
  state counts only edit tools, and sessions that edit through the shell would otherwise look as
  if they had edited nothing.
* The **redirect always comes before the still-stuck notice.** With the small envelopes of
  interactive repositories, the first call that counts as stuck can already have tripped the
  backstop, and the turn would get the notice without ever being redirected. An unspent redirect
  goes first, and the notice needs a second stuck segment after it.
* The **stuck judge and nudge writer run in a detached `te judge` job**, not in the event
  handler. The handler starts the job and returns; a later `tool_result` picks up the verdict,
  with the redirect already written. Each job's spend is charged to the ledger when it is spent.

omp keeps no session log the plugin can read from before it was installed, so there is no
earlier-session import: `te history` says the import is refused.

Every entry point fails open: any error becomes an empty output (no change) and a traceback in
`%USERPROFILE%\.tokeneconomizer\errors.log`. The dispatcher is stdlib only, and never waits on
a model call.

One session's events run one at a time (`tokeneconomizer/plugin/turnsession.py`): events that arrive together are
serialized through the session document. A handler that gets no lock within a few seconds saves
its event to the session's queue and answers nothing, and whoever holds the lock applies the
queue. `session_start`, and prompt admissions under `repomap=on`, answer with context, so they
are not queued: they wait, and then run without the lock. A decision the guard reaches on a
queued call has no answer to go in, so it waits on the turn and the turn's next call acts on it,
a call late. A turn that ends first drops it (an `undelivered` event): no nudge is counted, and
there is no one left to redirect.

## 2. What changes relative to the experimental setting

| | Experiments | Plugin | Why |
|---|---|---|---|
| Unit of work | one benchmark task | a **session** for micro-skills and tool pruning; a **prompt-turn** for the loop guard | In interactive use a person already splits work into short turns. A whole session folded as one trajectory would look stuck merely by being long |
| Learning signal | the official grader's `resolved` | the envelope learns from turns that **ended green**: the agent stopped on its own, was not stopped by the guard, and no verification after its last edit failed | There is no grader. Micro-skills and evidence pruning never needed one |
| Injection channel | a prefix on the task prompt | `SessionStart` `additionalContext`, re-sent as a system-prompt block on every prompt | Verified to reach the model (§3) |
| Tool removal | `--disallowed-tools` at launch | a **deny list**, applied by default once the evidence forms it and the repository's sessions have been idle longer than a prompt cache lives (§1), and announced at the next session start (`toolprune=propose` waits for approval instead), effective from the **next** session. The extension removes the tools from the active set from its own state and never writes an omp settings file | The plugin launches nothing. A session keeps the tools it started with, but a conversation resumed after a change sends the new ones, re-writing its cached prefix, hence the idle wait |
| Pruning candidates | orchestration, Q&A, plan mode, web — "inapplicable by construction" for a sealed headless task | nothing is inapplicable by construction; only **evidence** (never used, and unused over `toolprune_evidence` prompts (10) across at least `toolprune_min_sessions` sessions (2), or over `toolprune_sessions` sessions (5)) proposes a tool, and the protected work surface is never proposed | A person asks questions, plans and browses in a real session |
| Network | every arm carries an answer-key guard and drops the web tools | **no guard**: no blocked command or host; the web tools stay | A benchmark's answer is a fetch away. A real session has no answer key, and the web is part of the work |
| Who decides a turn is stuck | the trace judge, woken by the rule gate or a turn past the envelope | **always the judge** when the guard is on, woken only by a sign of trouble (a repeated failure, or dense errors) after `min_turns` calls, and never before the repository has an envelope; a `productive` or ambiguous verdict spares the turn. The hard backstop (far past the envelope AND the strict stall rule) still acts without it. While the model calls are paused, the judge gives no verdict and only the backstop acts | The rule alone redirected turns that had found their cause and were working around it |
| Redirect | kill the agent, resume it with a continuation prompt | a mid-turn steering message attached to the tool result | Different intervention, so its effect is not inherited from the experiments |
| Still stuck after the redirect | terminal abort, in autonomous runs | a one-line **notice** to the person through `ui.notify`, ending with how to stop the turn (§1); the plugin never stops a turn | The person decides |
| Micro-skills' reading | a domain vocabulary per benchmark (Python/git, Node/web) | **generic**, whatever the repository: every ecosystem's interpreters, package tools and test runners at once, builds and checks (`make`, `tsc`, `npm run build`, `cargo check`, ...) as their own intent, every edited file, and a language-neutral digest (failing commands with a normalized error line, paths relative to the repository) and prompt, with tests one topic among several | A person's repository may be in any language, or several |
| Micro-skills | on in the arms that measured it: the proposer every 5 tasks | **off by default**; `on` runs the free layer under the proposer, every `microskills_propose_every` sessions (3 by default), since a real session usually carries more work than one benchmark task | The proposer calls a model on the person's plan, and what it injects is derived from command output a repository controls: turning it on is the person's call |
| Optimizer model | the agent's own model, so overhead could not look cheaper than its arm | the person's own default model on the omp CLI (`paid_model`, over it `omp_paid_model`): the proposer when micro-skills is on, the loop guard's judge and redirect writer whenever the guard is on | Every call is still charged to the session ledger |

## 3. Verified omp behaviour

What the code relies on about omp, and how it was checked.

**oh-my-pi 18.22.2 — verified live, 2026-11-18:**

* `omp` loaded the extension its `omp` manifest key names, `plugin/omp/tokeneconomizer.ts`,
  from `@oh-my-pi/pi-coding-agent` (a type-only import, so the installed package needs no
  dependency).
* omp has no hook protocol: `session_start`, `session_switch`, `before_agent_start`,
  `tool_result`, `agent_end` and `session_shutdown` each become one `te-hook --host omp` call,
  with a `prompt_id` the extension numbers itself.
* `SessionStart` answers with the learned manual as `additionalContext` and, only there, the
  tools to remove; the extension removes them from the active set with `setActiveTools`, from
  the plugin's own state, and never writes an omp settings file.
* omp keeps only a digest of its system prompt, so the extension re-sends the manual as a
  `<tokeneconomizer>` block on every `before_agent_start`; the returned `systemPrompt`
  replaces the array for the next request and its continuations.
* A bash `tool_result` carries the command's exit code in `details.exitCode`, which reaches the
  dispatcher as `tool_response.exit_code`; a redirect is delivered as a steer message, and a
  still-stuck notice goes to `ui.notify` only when `api.agent.kind` is `"main"`, so a sub
  session shows none.
* The plugin's own calls run `omp -p`.
* The above was checked on 18.22.2 against recorded sessions and a live smoke.

**oh-my-pi 19.2.0 — extension load and `/te` overlay verified live, 2026-11-25:**

* The compiled loader resolves `renderMarkdown` from `@oh-my-pi/pi-tui/text/markdown/render`;
  the removed `getMarkdownTheme`, `Markdown` and `ScrollView` imports are not used.
* `/te status` rendered through the rows contract in the user-only overlay and closed on a
  keypress, with no extension-load warning.

## 4. State and privacy

All state is local, under `$TOKENECONOMIZER_HOME` (default `%USERPROFILE%\.tokeneconomizer`),
one directory per repository: the mechanisms' own state, `ledger.jsonl` (one priced record per
session), `events.jsonl` (every injection, redirect and still-stuck notice), and per-session
guard state. Uninstalling the plugin leaves this directory; delete it to forget. `te repos`
lists what it holds.

Which repository a session belongs to (`plugin/paths.py`, `plugin/repos.py`):

* **One key per repository.** A checkout with an `origin` remote is identified by the remote, so
  every clone, worktree and re-clone shares what was learned, whatever its folder is called; one
  without a remote by its main checkout's path (its worktrees share it); a directory outside any
  checkout by itself. A `.git` counts only as git counts it -- a directory holding `HEAD`, or a
  `gitdir:` file naming one that exists -- and the walk up from a session's directory stops at
  the home directory. `$TOKENECONOMIZER_REPO`, when set, is the repository's remote URL where git
  names none: a session in a directory without git metadata, or in a checkout without an
  `origin`, is keyed as a clone of that remote and shares what was learned there. A checkout's
  own `origin` wins.
* **One repository per session.** A hook's `cwd` is where the agent is now, so the first hook of
  a session pins it (`sessions/omp/<id>.json`) and every later hook, and the fold it hands off,
  uses the repository it started in. `te status` run inside a session says so when the two differ.
* **Deny lists stay per checkout.** The clones of a repository share its evidence and proposal,
  but each has its own active set, so `untool_applied.json` records per checkout what was
  removed, and what the person gave back with `restore`: `toolprune: auto`, the default, never
  applies that again (an explicit `toolprune apply NAME` does), as the evidence would keep
  proposing it.
* **State from earlier keys is carried over.** Up to 0.1.0 a checkout's folder name was part of
  the key, any `.git` entry made a checkout, and a session was recorded wherever the agent was.
  The first time a checkout is seen (`index.json`) the state under its earlier keys is sent
  where it belongs, and once after an upgrade a background `te repos migrate` does the same for
  every directory: each recorded session goes to the repository the first directory in its log
  resolves to now, sessions whose directory is gone stay put, and a directory none of whose
  sessions resolves to it any more is merged whole -- its learned state too, if it learned from
  more sessions than the destination. Learned state is not additive, so the other side's
  sessions are folded again from their logs where those still exist; a session folded under two
  keys counts once, at its later fold. Nothing is deleted: a merged directory moves to
  `merged/`, displaced learned state to `premerge-*`. `te repos merge KEY [INTO]` joins two by
  hand, for state whose checkouts are gone.

The state directory is private to you: the plugin creates it `0700` and its files `0600`, and
tightens a looser one it finds.

Nothing leaves the machine but the model calls of the loop guard (its judge and nudge writer, on
by default with the guard), of micro-skills (its proposer, when it is on), and, only when you run
it, of `toolprune measure`. Those calls send
**digests** — command templates, edited file basenames, error lines with paths and numbers
masked, never file contents — through your own omp CLI (`omp --print --mode json`, with nothing
loaded but the model), billed where your session is. The proposer keeps the digests of the last
eight sessions with its state, raw commands and output capped, so that each run sees more than the
session that just ended.

A learned fact is text injected into your agent's context, derived from your sessions' command
output — which a repository controls. The free layer only fills fixed templates with observed
command forms and hosts, and lints out completion advice; the proposer's facts are
open-vocabulary. On a repository you do not trust, leave micro-skills off (the default) or use
`microskills=envfacts`, and read what it learned with `te microskills`.

## 5. Known limitations

* **The loop guard, live** — both rungs have run on a provoked loop, not on a real spiral, and
  whether a nudge changes a looping model's course is not established. The rule counts edits and
  repeated failures, not understanding, so with a small learned envelope it could fire on a turn
  that had already found its cause and was working around it; the judge decides every redirect
  but the backstop's, which still counts.
* **Windows only** — the launchers are `te.cmd`/`te-hook.cmd` plus the bootstrap, and the tests
  run on Windows. The plugin's own calls have no on-device route here.
* **Known gaps:**
  * A turn that ran no recognised verification counts as ended green, so a failed turn without
    one can enter the envelope and loosen the guard for every later turn.
  * Test runs launched from a script are not recognised as verification.
* omp's session format is internal to omp and changes between versions; the reader is pinned by
  tests against the shape observed on the version above.
