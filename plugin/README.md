# TokenEconomizer plugin

How to install and use TokenEconomizer in oh-my-pi (omp) on Windows. What the plugin does and
why is in the [main README](../README.md); its design and the verified omp behaviour are in
[`docs/PLUGIN.md`](../docs/PLUGIN.md).

- [Before you start](#before-you-start)
- [Install](#install) · [Check it](#check-it)
- [What to expect](#what-to-expect) · [Commands](#commands) · [Settings](#settings)
- [Repository map](#repository-map) · [Using with rtk](#using-with-rtk) ·
  [Model calls](#model-calls) · [Troubleshooting](#troubleshooting)

## Before you start

- [ ] omp installed and signed in, any way it supports, at a supported version
  ([Supported versions](../README.md#supported-versions))
- [ ] Python 3.10 or newer on `PATH` (or set `TE_PYTHON` to one: a path with spaces or
  parentheses works, quoted)

No extra API key is needed. The extension loads when a session starts, so start a new session
after installing.

> **Where commands go.** Lines starting with `/te` are typed into **omp's prompt**, not your
> shell. Shell commands are in `powershell` blocks.

## Install

In a terminal:

```powershell
omp plugin uninstall tokeneconomizer
omp plugin install git:github.com/vadash/TokenEconomizer
```

Then start omp in one of your repositories.

**Update.** Run the install command again. **Uninstall.** `omp plugin uninstall
tokeneconomizer`. Optional: delete `%USERPROFILE%\.tokeneconomizer` to forget what it learned.
(In a repository where the plugin left tools out, run `/te toolprune restore` first.)

## Check it

Type `/te status` in omp. It shows a short status block in an overlay panel. Then work normally;
the plugin learns from each finished session:

```text
/te microskills            # the facts it injects at session start, and those still forming
/te toolprune              # the tools it left out, and the evidence
/te config set microskills=on   # learn proposing facts too (needs model calls, off by default)
```

omp reports its tool catalog at each session start, so there is nothing to record by hand.

## What to expect

The plugin works session by session: what it learns from a repository's earlier sessions is
applied to the ones that follow. Facts and the deny list take effect when a session starts; the
loop guard acts during a turn.

- **Facts** (once you turn micro-skills on; it is off by default). Two sources propose them, in
  any language or toolchain. The first needs no model call: the same environment failure in a few
  sessions (a tool, interpreter, build or test command that fails as typed and works spelled
  another way). The second runs every `microskills_propose_every` sessions (3 by default): a model
  reads digests of the recent sessions (the commands run, including build, test and lint commands,
  the files edited, and each failing command with its error line, with paths relative to the
  repository) and writes facts about the repository, each with patterns that test it against later
  sessions. A fact becomes active once three sessions confirm it (for a proposed fact about the
  code, sessions that edited different files), and is injected at every session start while the
  failure it prevents keeps showing up, or keeps being avoided, in at least 40% of the sessions
  where it applies (over at least four). It is retired when a session contradicts it (for a
  command-spelling fact, two in a row). How much this helps depends on the repository's state and
  the tasks you run in it. A session start that adds facts says so once, in one line. omp keeps
  only a digest of its system prompt, so the extension re-sends the facts as a block on every
  prompt; a resumed conversation keeps the facts it started with, word for word. `/te
  microskills` shows the facts injected and those still forming.
- **Deny list.** A tool that is loaded but never used here is left out without asking you, once
  it has gone unused over `toolprune_evidence` prompts (10 by default) across at least
  `toolprune_min_sessions` sessions (2), or over `toolprune_sessions` sessions of any length
  (5). A session that ran no prompt does not count. The list is written once the repository's
  sessions have been idle for `toolprune_quiet_minutes` (60 by default), so that no conversation
  you resume loses its prompt cache (a list that has waited a day is written anyway), and it
  applies from the next session, whose start says so once. The extension removes the tools from
  the session's active set from its own state; it never writes an omp settings file. `/te
  toolprune` shows a list still waiting. `/te toolprune restore` gives back what the plugin
  removed, and only that; what you give back is never removed again (`/te toolprune apply NAME`
  removes one again). With `toolprune=propose`, the list is only proposed, the session start says
  so, and nothing changes until you run `/te toolprune apply`. `toolprune=off` stops new lists but
  leaves an applied one in place.
- **Loop guard.** Quiet on normal turns, and idle until `loopguard.envelope_min_samples` turns in
  a repository (5 by default) have ended well: on their own, with no failing check after the last
  edit. After that, a turn that keeps hitting the same failures, or piles up errors, after
  `loopguard.min_turns` tool calls is put to a judge model. Only a "stuck" verdict with confidence
  `loopguard.judge_confidence` (0.7) or more attaches a redirect (written by that model) to a tool
  result as a steering message, at most `loopguard_nudges` (1) per turn; if the turn is still stuck
  after that, a one-line notice in the UI tells you how to stop it (on stderr under `omp -p`). A
  long turn with no sign of looping is never judged. A turn past `loopguard.backstop_mult` (2.5)
  times the repository's usual length that also keeps churning edits through the same failures is
  redirected even if the judge disagrees. The plugin never stops a turn itself. Where the notice
  appears, and how to stop the turn: [When a turn gets stuck](../README.md#when-a-turn-gets-stuck).
- **Cost.** `/te status` shows each session's tokens, priced after it ends, and the plugin's own
  overhead: the loop guard's judge, and micro-skills' proposer when it is on.

## Commands

| Command | What it does |
|---|---|
| `status` | what it has learned in this repository, what it did, and what your sessions cost |
| `microskills` | the facts it injects at session start, and those still forming |
| `microskills forget ID …` | stop injecting the facts with these ids (as `microskills` lists them), and never learn them again under that id or from the same evidence; an unknown id changes nothing |
| `microskills unforget ID …` | bring forgotten facts back, with the evidence they had |
| `toolprune` | the deny list: what it left out, what it would leave out next, and the evidence |
| `toolprune apply [ITEM …]` | apply a waiting proposal (with `toolprune=propose`), or the named items; from the next session |
| `toolprune restore [ITEM …]` | give back the plugin's entries (all, or the named ones); they are not removed again |
| `toolprune refresh-catalog` | record the tools a session loads (omp reports its catalog at each session start, so this is rarely needed) |
| `toolprune measure` | the request with and without the proposal |
| `config` | the settings: what is on, and every value in effect |
| `config set KEY=VALUE … [--global]` | change settings for this repository (or every one); see [Settings](#settings) |
| `config unset KEY … [--global]` | remove the setting at this level, so the global value (or the default) applies |
| `repomap` / `repomap refresh` | the cached repository map / make it now (see [Repository map](#repository-map)) |
| `history` | what the import of this repository's earlier sessions did (omp keeps no session log the plugin reads, so an import is refused) |
| `repos` | every repository it keeps state for: how it was identified, its sessions, the checkouts seen |
| `repos migrate` | file state an earlier version recorded under the repository it belongs to (runs by itself, once, after an upgrade) |
| `repos merge KEY [INTO]` | merge one repository's state into another's (default: this one), for state whose checkouts are gone |
| `doctor` | check Python, the state directory, omp, how this directory was identified, and the error log |

Run every command as `/te <command>` in omp, or from a terminal in the repository as the
installed plugin's `scripts\te.cmd <command>` (before the first session, the checkout's
`plugin\scripts\te.cmd`). From a terminal, `--host` is accepted but ignored (omp is the only
host). Quote a fact id that holds spaces or shell characters: `te.cmd microskills
forget 'form_repair:run_tests:pytest -q'`.

Inside omp, each command answers in an overlay panel; from a terminal it costs nothing. `/te
config` shows links to the two config files instead of values.

## Settings

Each of the three mechanisms can be turned on or off on its own, and each has numbers you can
set by hand. Settings apply to the current repository, or to every repository with `--global`.
The examples use omp's form:

```text
/te config set microskills=on               # micro-skills, in this repository
/te config set toolprune=off loopguard=off
/te config set microskills=on --global       # in every repository
/te config                                # what is on, and every value in effect
```

| Mechanism | Key | `on` means | Other values |
|---|---|---|---|
| Micro-skills (off by default) | `microskills` | `envfacts+distiller`: facts found without model calls, plus a model proposing facts every `microskills_propose_every` sessions | `off`; `envfacts`: only the facts found without model calls; `distiller`: only the proposer |
| Tool pruning | `toolprune` | `auto`: applied without asking once your sessions have been idle for `toolprune_quiet_minutes` (or a day after it formed), from the next session, which announces it | `off`; `propose`: only proposed, applied when you run `toolprune apply` |
| Loop guard | `loopguard` | `nudge`: a judge model decides whether a looping turn is stuck; if so, up to `loopguard_nudges` mid-turn redirects, then a notice to you if the turn is still stuck | `off` (no judge either); `stop` is read as `nudge`, with a warning |

**Tune a mechanism:**

```text
/te config set microskills_max_facts=2            # inject at most two facts
/te config set toolprune_evidence=20              # wait for twenty unused prompts
/te config set loopguard_nudges=2 loopguard.min_turns=30
/te config unset loopguard.min_turns              # back to the default
```

| Key | Default | What it sets |
|---|---|---|
| `microskills_max_facts` | `4` | the most facts from repeated failures injected at session start (the proposer's facts are not capped) |
| `microskills_max_tokens` | `150` | the size of those facts' block, in tokens (about 4 characters each) |
| `microskills_propose_every` | `3` | sessions between runs of the fact proposer (the first run comes one session early: after the second, by default) |
| `toolprune_evidence` | `10` | prompts a never-used tool must go unused, over at least `toolprune_min_sessions` sessions, before it is left out |
| `toolprune_min_sessions` | `2` | sessions those prompts must span; `1` lets a single session's prompts decide |
| `toolprune_sessions` | `5` | or this many sessions, however few prompts they ran |
| `toolprune_quiet_minutes` | `60` | how long the repository's sessions must have been idle before `auto` writes a deny list, so that no conversation resumed while its prompt cache is warm loses it (a list is written at the latest a day after it formed); `0` writes at once |
| `toolprune_protect` | none | tools never left out however long they go unused, by name or pattern (`debug,*mcp*`), separated by commas; a deny rule the plugin added for one is removed at the next fold |
| `loopguard_aggr` | `default` | the guard's threshold preset: `conservative`, `default` or `aggressive` |
| `loopguard_nudges` | `1` | redirects per turn before the guard only tells you |
| `loopguard.<name>` | from the preset | one guard threshold, set by hand (below) |

The guard looks for a turn that keeps editing the same files and hitting the same failures. It
also learns how long this repository's turns usually run when they end well (on their own, with
no failing check after the last edit): the *envelope*. Until `envelope_min_samples` such turns
exist the guard does nothing; after that, some thresholds are learned from those turns, on top
of the preset. A value you set with `loopguard.<name>` wins over both. Defaults at each preset:

| `loopguard.<name>` | `conservative` | `default` | `aggressive` | What it sets |
|---|---|---|---|---|
| `min_turns` | 28 | 20 | 14 | tool calls in a turn before it can count as stuck |
| `edit_churn` | 5 | 4 | 3 | edits to one file that count as churn |
| `distinct_edit_files` | 3 | 3 | 3 | files edited that count as scattered |
| `error_outputs` | 8 | 6 | 4 | failing tool results that count as sustained errors |
| `stagnation` | 0.30 | 0.20 | 0.12 | share of failures that repeat an earlier one |
| `envelope_pct` | 90 | 90 | 90 | percentile of the repository's well-ended turns the envelope starts from |
| `envelope_margin` | 1.5 | 1.25 | 1.0 | multiplier on that percentile |
| `envelope_min_samples` | 7 | 5 | 3 | well-ended turns needed before the guard acts |
| `backstop_mult` | 3.5 | 2.5 | 1.8 | past this multiple of the envelope, a turn the rule calls stuck is redirected even if the judge disagrees, then flagged to you |
| `judge_confidence` | 0.85 | 0.7 | 0.55 | how sure the judge must be that a turn is stuck |
| `gate_min_turns` | 24 | 18 | 12 | tool calls before the judge is asked (never fewer than `min_turns`) |
| `gate_error_outputs` | 8 | 8 | 8 | failing results that ask the judge without a repeated failure |

**Repository map** (off by default; see [Repository map](#repository-map)):

| Key | Default | What it sets |
|---|---|---|
| `repomap` | `off` | `on`: a map ranked toward the session's first prompt; `start`: the cached map at session start |
| `repomap_tokens` | `2048` | the map's size, in tokens |
| `repomap_python` | auto | the Python that has aider installed |
| `repomap_timeout` | `8` | seconds the first prompt may wait for a ranked map before the cached one is used |

**Other settings:**

| Key | Default | Values |
|---|---|---|
| `enabled` | `true` | `false`: the whole plugin is off; its entry points return at once and record nothing |
| `paid_model` | `""` | model for the plugin's own calls; empty: omp's own default model |
| `omp_paid_model` | `""` | the same, over `paid_model` |
| `omp_cli` | `""` | the `omp` the plugin's own calls run; empty: the first on PATH |
| `shadow` | `false` | decide and log everything, act on nothing |
| `mode` | `auto` | `interactive` or `headless` to override what the plugin detects |

**Where settings live, and when they apply.** For one session, an environment variable wins:
`TE_` plus the setting's name in capitals (`TE_MICROSKILLS`, `TE_TOOLPRUNE_EVIDENCE`,
`TE_LOOPGUARD_NUDGES`, ...), and `TE_LOOPGUARD_THRESHOLDS='{"min_turns": 30}'` for the guard's
thresholds; `TE_DISABLE=1` turns the whole plugin off. Otherwise the repository's
settings win over the global ones, and those over the defaults; `unset` goes back one level. A
change applies from the next hook call: the guard at the next tool call, facts and the
deny list at the next session start. A new fact-size cap shows once the next session has ended.

**Only in some repositories.** Turn the plugin off everywhere, then back on in each repository
you choose; in a repository left off, the plugin records nothing:

```powershell
.\te.cmd config set enabled=false --global
.\te.cmd config set enabled=true          # in each chosen repository
```

**Earlier names.** Before the mechanisms were renamed they were `inject`, `untool` and `spiral`,
with settings such as `inject_max_facts`, `untool_evidence`, `spiral_nudges` and `spiral.<name>`,
and variables `TE_INJECT`, `TE_UNTOOL`, `TE_SPIRAL`, ...; none of them are read any more, and the
value `guard` is no longer accepted for `loopguard`. A config file that still holds an old name
loads with an unknown-key warning for each and keeps its defaults. `config set` refuses old names
with an unknown-key error. The earlier commands `te facts` and `te untool` are unknown commands.
Use `te microskills` and `te toolprune` instead.

## Repository map

An opt-in fourth mechanism: aider's map of the repository's key files and symbols (signatures
only, ranked by how much of the code refers to them), added to the session's context so omp
knows where things are before it starts reading files. Turn it on per repository:

```powershell
pip install aider-chat
.\te.cmd config set repomap=on
```

- **What goes in.** At most `repomap_tokens` tokens (default 2048), once per session. With
  `repomap=on`, the first prompt waits up to `repomap_timeout` seconds for a map ranked toward
  that prompt's identifiers before the cached one is used instead. `repomap=start` uses the
  cached map at session start and never delays a prompt.
- **What it reads.** The files `git ls-files` lists, in every language aider understands,
  without vendored code, minified files or files over 256 KB.
- **Where it runs.** In whichever Python has aider: `repomap_python` if you set it, else the one
  behind an installed `aider` command, else the plugin's own. `/te doctor` says which it found.
  The plugin never imports aider itself. On Windows, prefer an explicit `repomap_python`: uv and
  pipx shims are binary launchers the probe cannot read a `#!` line from.
- **What it writes.** Nothing in the repository. The map and aider's tag cache live in the
  plugin's state directory. The map is cached per commit and remade in the background when
  `HEAD` moves.
- **What it costs.** No model calls. The map is part of every request of the session, so it
  costs on sessions that already knew where to look and saves on those that would have searched.
  `/te status` shows its size.

## Using with rtk

[rtk](https://github.com/rtk-ai/rtk) compresses shell output. Install it on its own (`rtk init
-g`); it does not need to be part of this plugin, and the two do not get in each other's way:
rtk rewrites shell commands before they run, and this plugin reads sessions, injects context and
watches tool results. The plugin reads `rtk`-prefixed commands as the commands they wrap, and
rtk adds no tools for the deny list to see. `/te doctor` reports an installed rtk.

One interaction to know: with rtk 0.45.0, `rtk pytest` prints nothing when pytest is missing (it
exits with code 1). The agent sees no error and the plugin cannot learn the "`pytest` fails here;
`X` works" fact from it. Commands that name their interpreter (`.venv\bin\python -m pytest`) are
not rewritten, so the commands that fact recommends keep working.

## Model calls

**With the defaults, the plugin's only model calls are the loop guard's**: the judge, asked about
a turn that shows signs of looping, and, when the judge finds the turn stuck, the redirect it
writes. Tool pruning never calls a model. Turning micro-skills on adds its fact proposer, once
every `microskills_propose_every` sessions (`microskills=envfacts` keeps only the facts found
without a model call); `loopguard=off` turns the judge off along with the guard.

What each call is sent, when it runs, and roughly how long its prompt is (about four characters
to a token). No call is sent file contents or your prompts:

| Call | When | What it reads | Prompt |
|---|---|---|---|
| Judge | in a turn that shows signs of looping, one call at a time, at most every 5 tool calls; after each "not stuck" answer the gap grows by 5 calls, up to 20 | the turn's last 24 actions (tool names, commands cut to 90 characters, edited file names, error signatures), its counts of edits, failing outputs and test runs, and how long this repository's turns usually are | at most about 1,500 tokens; the reply is a one-line verdict |
| Redirect writer | right after the judge finds the turn stuck, at least `loopguard.judge_confidence` (0.7) sure | the judge's digest | at most about 1,500 tokens; the reply is 2 to 4 sentences |
| Fact proposer | with `microskills=on` (or `distiller`), once every `microskills_propose_every` (3) sessions | digests of the last 8 sessions (for each: up to 12 command shapes, 6 checks and 6 failing commands with their error lines, all cut to 80 or 120 characters, and the edited files' names), and the facts already learned | a few thousand tokens, more when those sessions edited many files; the reply is a few facts and the patterns that test them |

Each call is one request through omp's CLI (`omp --print --mode json`, with nothing loaded but
the model), so it uses whatever your sessions already use (the provider and login omp is set up
with), is billed there, and is shown in `/te status`. `status` prints the repository's total as
`plugin overhead`, with its share of all tokens. Per session, the repository's `ledger.jsonl`
(under `%USERPROFILE%\.tokeneconomizer\repos\`) has the proposer's tokens in the session's record,
under `overhead`, and a record of its own for each judge call (`"kind": "overhead"`, with the
session's id).

A call that gets no reply leaves a line in `%USERPROFILE%\.tokeneconomizer\errors.log`. If omp
says it is not signed in, the plugin's calls are also paused (the loop guard then redirects only
at its backstop), the next session start says so once, and `config set microskills=on` (or
`loopguard=on`) tries again.

## Troubleshooting

| Symptom | Fix |
|---|---|
| a command prints an import error, or the hooks seem to do nothing | run `scripts\te.cmd doctor` (in the installed plugin, or the checkout) and read its errors.log lines |
| errors.log says the package "was not found for the plugin" | the checkout the plugin was installed from moved or is gone: install again, or build a [self-contained bundle](#install) with `python scripts\build_plugin.py --out dist\plugin` |
| `TokenEconomizer needs Python >= 3.10 (this is 3.x)` | install Python 3.10+ or set `TE_PYTHON` to one |
| the deny list leaves nothing out after many sessions | check `/te toolprune`: omp reports its catalog at each session start, so a missing catalog means no session has started since installing |
| errors.log has `sidecall omp: no reply`, and a session start says the plugin's model calls are paused | omp is not signed in: sign in, then `config set microskills=on` (or `loopguard=on`) tries again |
