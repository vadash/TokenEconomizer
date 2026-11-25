# TokenEconomizer: Experience-Driven Black-Box Cost Control for Coding Agents

<p align="center">
  Aaron J. Li<sup>1</sup>,
  Weijia Xu<sup>2</sup>,
  Chandan Singh<sup>2</sup>,
  Zixiao Chen<sup>3</sup>,
  Jeevana Priya Inala<sup>2</sup>,
  Charlotte Siska<sup>3</sup>,
  <br>
  Acar Tamersoy<sup>3</sup>,
  Bin Yu<sup>1</sup>,
  Ion Stoica<sup>1</sup>,
  Michel Galley<sup>2</sup>,
  Jianfeng Gao<sup>2</sup>
</p>
<p align="center">
  <sup>1</sup>UC Berkeley &nbsp; <sup>2</sup>Microsoft Research &nbsp; <sup>3</sup>Microsoft Security
</p>

<p align="center">
  <a href="https://arxiv.org/abs/2610.22732"><img src="https://img.shields.io/badge/arXiv-2610.22732-b31b1b.svg" alt="arXiv"></a>
  <a href="https://tokeneconomizer.github.io"><img src="https://img.shields.io/badge/project-page-2f6fb0.svg" alt="Project page"></a>
  <a href="https://x.com/aaron_jli/status/2114797313613053952"><img src="https://img.shields.io/badge/X-post-000000.svg?logo=x&logoColor=white" alt="X post"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776ab.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license">
</p>

<p align="center">
  <a href="https://trendshift.io/repositories/315380"><img src="https://trendshift.io/api/badge/trendshift/repositories/315380/daily?language=Python" alt="#1 Python repository of the day on Trendshift" width="250" height="55"></a>
  <a href="https://trendshift.io/repositories/315380"><img src="https://trendshift.io/api/badge/trendshift/repositories/315380/weekly?language=Python" alt="#10 Python repository of the week on Trendshift" width="250" height="55"></a>
</p>

<p align="center">
  Our measurement on SWE-Bench tasks: <b>18.4–30.9% lower mean cost</b> across five agent–model
  combinations, cheaper in <b>all 30</b> repository × combination cells, and Pass@1 up in
  <b>4 of 5</b>. <a href="#results">Results</a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-reduced-motion: reduce)" srcset="docs/assets/teaser.png">
    <img src="docs/assets/teaser.gif" width="100%" alt="An animation that opens on a title card: TokenEconomizer, experience-driven black-box cost control for coding agents, with its three mechanisms (micro-skills, tool pruning, failure-loop intervention), and the UC Berkeley and Microsoft logos. Then a schematic of six sessions in one repository, each a bar of what it cost: the work itself plus three kinds of waste, namely re-learning the repo, tools never used, and getting stuck in loops. Each of the first three sessions teaches TokenEconomizer one thing, in order: 1 micro-skills remember how the repo works (run tests with python -m pytest), 2 tool pruning skips tools never used here (Read and Edit kept, Notebook and Browser left out), and 3 failure-loop intervention spots loops and steps in (the same error three times meets a nudge to try another way). In the session after each lesson that waste shrinks, so the bars step down below a dashed line marking the cost without TokenEconomizer. It ends on results measured on SWE-Bench tasks: 18.4–30.9% lower mean cost across five agent–model combinations, cheaper than default in 30 of 30 repository × combination cells, and Pass@1 up in 4 of 5; per combination, cost 23.6% lower and Pass@1 +1.3 for Claude Code with Sonnet 5, 30.9% and −1.0 with Opus 4.8, 24.7% and +0.9 for Codex with GPT-5.6 Terra, 26.1% and +1.6 with GPT-5.6 Sol, and 18.4% and +2.6 for Copilot CLI with Sonnet 5.">
  </picture>
</p>

TokenEconomizer makes coding agents cheaper to run in the repositories you work in often,
without changing the agent itself. It learns from the sessions an agent has already run in a
repository, and uses that experience to cut waste in the sessions that follow. It has three
mechanisms:

- **Micro-skills: remember how the repository works.** Sessions keep rediscovering the same
  facts: a build step that must run first, the spelling of a command that works here
  (`python -m pytest` where `pytest` is missing, `npx tsc` where `tsc` is), an environment
  variable a command needs, where generated files live. Micro-skills learns such facts, in any
  language, from two sources: failures that repeat across sessions, and a model that reviews the
  recent sessions every *k* sessions (*k* = 3) and proposes facts. A fact is stated at the start
  of each session once three sessions have confirmed it, and dropped when later sessions
  contradict it.
- **Tool pruning: stop loading tools that go unused.** Every loaded tool's definition is sent with
  every request. A tool that goes unused over *p* prompts spread across at least *m* sessions, or
  over *s* sessions of any length (*p* = 10, *m* = 2, *s* = 5), is left out of the next sessions
  through the agent's own settings. One command brings it back.
- **Failure-loop intervention (the loop guard): step in when a turn is stuck.** When a turn (the
  agent's work on one prompt) keeps hitting the same failures, a judge model checks whether it is
  stuck. If the judge is at least *c* sure (*c* = 0.7), the agent gets a redirect (a short note,
  added to its context, to try another approach), at most *r* times per turn (*r* = 1). If the
  turn is still stuck after that, TokenEconomizer tells you how to stop it; it never stops a turn
  itself.

TokenEconomizer is a plugin for **oh-my-pi (`omp`)** on Windows. Each italic
letter above is a setting, shown with its default ([Settings](#settings)), and each mechanism can be turned on
or off. The plugin announces each action in one line, and if any part of it fails, your session
carries on as if it were not installed. Its model calls (the fact proposer, and the judge with the
redirect it writes) run through omp itself, on the same login, and their cost is
included in its reports. What it learns stays on your machine, in `%USERPROFILE%\.tokeneconomizer`.

## Results

The paper ran five agent–model combinations on the same 277 SWE-Bench tasks from six repositories.
Each task ran as its own session, and each repository's tasks ran in order, starting with nothing
learned. Costs include every model call TokenEconomizer made. Within each repository and
combination, cost fell by 5–40%. The savings held on Web-Bench (16–21% lower) and on two
security-auditing benchmarks where every task is in a new repository (6–20% lower). The
[project page](https://tokeneconomizer.github.io) has interactive charts, per-repository numbers
and nine annotated runs.

## How it works

<p align="center"><img src="docs/assets/framework_overview.png" width="100%" alt="TokenEconomizer: the plugin keeps what it knows about a repository and applies micro-skills, tool pruning and failure-loop intervention to your unchanged coding agent; each finished session's log updates all three"></p>

Coding agents are built to finish tasks, not to keep them cheap. Every turn pays again for the
agent's instructions, every loaded tool, and the conversation so far. Developers use the same
agent on the same repository again and again, so much of that cost repeats: the same facts
rediscovered, the same unused tools sent, the same kinds of failing loops. TokenEconomizer leaves
the agent's model, weights and code alone and learns across a repository's sessions instead of
treating each one on its own, so whatever the plugin spends is paid back over the sessions that
follow. Four principles guide the mechanisms:

- **Work through what the agent already reads.** Facts enter the context at session start, and
  tools are removed through the agent's own settings, so nothing depends on the agent learning a
  new tool.
- **Count cost as it is billed.** Most tokens in a session are cached re-reads, so cost is
  computed with cache pricing, and it includes the plugin's own model calls.
- **Protect task success.** Facts form only from repeated evidence, tools are removed only after
  going unused, and the loop guard leaves alone a turn that is still making progress.
- **Keep every action reversible.** A fact that later sessions contradict is dropped, removed
  tools can be given back, and the loop guard only redirects and tells you: stopping a turn is
  your call.

## Quick start

You need omp, signed in any way it supports, and Python 3.10 or newer on
`PATH`. The plugin needs no API key of its own and never handles credentials, so any login
works.

### Install

```powershell
omp plugin uninstall tokeneconomizer
omp plugin install git:github.com/vadash/TokenEconomizer
```

Start omp in one of your repositories and type `/te status`. To update, run the install command
again. To uninstall, `omp plugin uninstall tokeneconomizer`; what it learned stays in
`%USERPROFILE%\.tokeneconomizer` until you delete it.

### Supported versions

| Agent | Supported | Older versions |
|---|---|---|
| omp | 19.2.0 | not tested |

**[`plugin/README.md`](plugin/README.md)** walks through installing, everyday use, and
troubleshooting in more detail.

### Everyday commands

Work as usual; the plugin needs nothing from you. To see or change what it does, use its
commands:

| Where | Type a command as |
|---|---|
| In omp | `/te <command>` |
| From a terminal, in the repository | the installed plugin's `scripts\te.cmd <command>` |

| Command | What it does |
|---|---|
| `status` | what it learned and did here, and what your sessions cost |
| `microskills` | the facts it injects, and those still forming |
| `toolprune` | the tools it left out, and the evidence |
| `toolprune restore` | give them back; they will not be removed again |
| `config` | every setting in effect |
| `config set microskills=on` | turn a mechanism on (or `=off`) in this repository; add `--global` for every repository |

In omp each command answers in an overlay panel; from a terminal it is free. The other commands
(`repos`, `doctor`, `config unset`, ...) are listed under
[Commands](plugin/README.md#commands).

## Settings

Tool pruning and the loop guard are on by default; with these defaults, the plugin calls a model
only when a turn shows signs of looping: the judge, and the redirect it writes if it finds the
turn stuck. Micro-skills is off by default, because its proposer adds model calls and how much it
saves depends on the repository and the work you do there. What each of these calls reads, how
often it runs and which model it uses are listed under
[Model calls](plugin/README.md#model-calls). Each mechanism has its own switch
(`microskills`, `toolprune`, `loopguard`) that takes `on` or `off`, and the italic letters above
are these settings:

| | Setting | Default | What it sets |
|---|---|---|---|
| *k* | `microskills_propose_every` | 3 | sessions between runs of micro-skills' fact proposer |
| *p* | `toolprune_evidence` | 10 | prompts a tool must go unused before it is left out |
| *m* | `toolprune_min_sessions` | 2 | sessions those prompts must span |
| *s* | `toolprune_sessions` | 5 | or this many sessions, of any length |
| *q* | `toolprune_quiet_minutes` | 60 | minutes your sessions here must be idle before a deny list is written |
| *e* | `loopguard.envelope_min_samples` | 5 | turns here that ended well (no failing check after the last edit) before the loop guard acts |
| *c* | `loopguard.judge_confidence` | 0.7 | how sure the judge must be that a turn is stuck |
| *r* | `loopguard_nudges` | 1 | redirects per turn before the guard only tells you |
| *b* | `loopguard.backstop_mult` | 2.5 | how many times its usual length a churning turn may run before it is redirected without the judge |

Change them with `config set`, shown here in omp's form (`/te` before each):

```text
/te config set microskills=on                 # turn micro-skills on
/te config set microskills_propose_every=5    # k = 5: propose facts less often
/te config set toolprune_evidence=20         # p = 20: wait for twenty unused prompts
/te config set loopguard_nudges=2             # r = 2: redirect twice before telling you
```

Tool pruning counts prompts so that long sessions weigh more, and requires *m* sessions so that a
single session never decides alone. It writes the deny list only once your sessions in the
repository have been idle for *q* minutes (or, at the latest, a day after the list formed): a
conversation resumed after its tools change must write its whole history to the prompt cache again
instead of reading it, and after an idle hour that cache has expired anyway.

Besides `on` and `off`, two modes are useful: `toolprune=propose` only proposes the deny list, and
nothing changes until you run `toolprune apply`; `microskills=envfacts` learns facts only from
repeated failures, with no model calls.

To change a single session, set an environment variable when you start omp:
`TE_DISABLE=1` turns the whole plugin off, and `TE_LOOPGUARD=off` turns off one
mechanism (likewise `TE_MICROSKILLS` and `TE_TOOLPRUNE`). Every setting and its default are
listed under [Settings](plugin/README.md#settings); each one's environment variable is `TE_` plus
its name in capitals.

## What it does on omp

Micro-skills and the loop guard work the same way. What it learns in a repository is used in
that repository, while settings are shared. Tool pruning acts through omp's own levers: the
extension removes the tools the deny list names from the session's active set, from the plugin's
own state, and never writes an omp settings file.

The session start that says what was left out shows once. `toolprune restore` removes the
plugin's entries and nothing else, and tools you give back are not removed again. **[`docs/PLUGIN.md`](docs/PLUGIN.md)**
records how omp's events were verified to behave, and **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**
how the package fits together.

### When a turn gets stuck

The loop guard first learns how long a turn in this repository usually runs: it waits until *e*
turns here have ended well, on their own and with no failing check after the last edit. After
that, a turn that shows signs of looping (the same failures repeating, or errors piling up) goes
to the judge. One case is redirected without the judge: a turn that runs past *b* times the usual
length while churning the same edits through the same failures. If a turn is still stuck after its
redirects, you see one line that ends with how to stop it:

> TokenEconomizer: this turn still looks stuck after a redirect (judge-stuck@0.82, 46 tool
> calls). Press Esc to interrupt it.

The redirect arrives as a steering message on a tool result; the still-stuck notice goes to the
UI as a notification (on stderr under `omp -p`). To stop the turn, press **Esc** (it ends the
turn and its command); under `omp -p`, Ctrl+C ends the run, but a command it started runs on
until it finishes.

In a headless run nobody may be watching, so a flagged turn runs until it ends or reaches omp's
own limits; afterwards, `/te status` counts the redirects and "still stuck" notices.

## Repository

| | |
|---|---|
| [`tokeneconomizer/microskills`](tokeneconomizer/microskills/) | Micro-skill synthesis and injection: facts learned from finished sessions |
| [`tokeneconomizer/toolprune`](tokeneconomizer/toolprune/) | Tool pruning: unused tools and their evidence |
| [`tokeneconomizer/loopguard`](tokeneconomizer/loopguard/) | Failure-loop intervention: the per-call guard and its per-repository envelope |
| [`tokeneconomizer/trace`](tokeneconomizer/trace/) | omp's session log, read into one shape |
| [`tokeneconomizer/plugin`](tokeneconomizer/plugin/) | The runtime the extension and the `te` command run |
| [`plugin`](plugin/) | The installable plugin: the omp extension, the Windows launchers, and the core link |
| [`docs`](docs/) | `PLUGIN.md` (design, verified omp behavior) and `ARCHITECTURE.md` (how the package fits together) |
| [`tests`](tests/) | Offline tests for the package and the plugin |

The package is standard-library only. Run the tests with `COLUMNS=200 python -m pytest tests`
from a checkout (on Windows, with stdin redirected from a file), and build a self-contained
plugin bundle with `python scripts/build_plugin.py --out dist/plugin`.

## Citation

```bibtex
@misc{li2026tokeneconomizer,
  title         = {{TokenEconomizer}: Experience-Driven Black-Box Cost Control for Coding Agents},
  author        = {Li, Aaron J. and Xu, Weijia and Singh, Chandan and Chen, Zixiao and
                   Inala, Jeevana Priya and Siska, Charlotte and Tamersoy, Acar and Yu, Bin and
                   Stoica, Ion and Galley, Michel and Gao, Jianfeng},
  year          = {2026},
  eprint        = {2610.22732},
  archivePrefix = {arXiv},
  primaryClass  = {cs.CL},
  url           = {https://arxiv.org/abs/2610.22732}
}
```

## License

[`MIT LICENSE`](LICENSE).
