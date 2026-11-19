"""`te` -- inspect and steer what TokenEconomizer does in your sessions.

    te help                           this list of subcommands, one line each
    te status                         what it has learned for this repository, and what it did
    te status --explain               explain the current settings and saved learning state
    te microskills                    the facts it injects, and the ones still forming
    te microskills forget ID ...      stop injecting the facts with these ids (as listed by
                                      `te microskills`); they are not learned again
                                      under that id or from the same evidence
    te microskills unforget ID ...    bring forgotten facts back, as they were
    te toolprune show                 the proposed deny list, and the evidence behind it
    te toolprune apply [ITEM ...]     write the proposal (or the named items): the omp session's
                                      tool list
    te toolprune restore [ITEM ...]   remove entries this plugin added
    te toolprune refresh-catalog      record the tool list a fresh session loads (omp reports it
                                      at each SessionStart)
    te toolprune measure              measure the request with and without the proposal
    te toolprune apply-pending        write a pending `auto` deny list once the repository has
                                      been idle long enough (the fold starts it, detached)
    te config show                    each mechanism on or off, its settings, the guard's thresholds
    te config set KEY=VALUE ...       e.g. `loopguard=off`, `microskills=on`,
                                      `loopguard.min_turns=12` (this repository; `--global` for
                                      every repository)
    te config unset KEY ...           back to the next source down (the global file, then default)
    te repomap [show | refresh]       the repository map (`repomap=on`): what is cached, or
                                      make it now
    te history [import --status]      what an import of earlier sessions did here (omp keeps no
                                      session log the plugin reads, so an import is refused)
    te repos                          every repository with state here: how it was identified,
                                      its agents and sessions, the checkouts seen
    te repos migrate                  send state recorded under earlier keys where it belongs
                                      now (runs by itself, once, after an upgrade)
    te repos merge KEY [INTO]         merge the state directory KEY into INTO (default: this
                                      repository's), for state whose checkouts are gone
    te doctor                         check the installation
    te --host omp probe             not available: the session probe needs another agent's
                                      session logs

With no subcommand, `te` prints the same list as `te help`.
`--cwd` picks the repository (default: the current directory) and `--host` the agent (omp,
the only one).
"""
import argparse
import glob
import json
import os
import shlex
import shutil
import sys
import textwrap
import time

from .. import loopguard
from . import (accounting, denylist, fold, history, hosts, paths, repomap, repos, sidecall)
from .config import LiveConfig, unset_config, write_config
from .context import LiveContext
from .paths import state_root


def _ctx(args, env=None, session_id=None):
    return LiveContext.build(hosts.Omp(), args.cwd, env, session_id=session_id)


def _unwritable(path):
    """Why the plugin cannot write its state at `path`, or None when it can. A command that
    changes something checks first: a sandboxed agent must not get half a change -- a deny list
    recorded, and no state root to record it in, which `restore` needs."""
    probe = os.path.join(path, f".write-test-{os.getpid()}")
    try:
        os.makedirs(path, exist_ok=True)
        open(probe, "w", encoding="utf-8").close()
        os.remove(probe)
        return None
    except OSError as e:
        return e.strerror or str(e)


def _refuse_unwritable(path):
    """Print why a change cannot be made here, and return the exit code; 0 when it can."""
    why = _unwritable(path)
    if why is None:
        return 0
    print(f"not changed: TokenEconomizer cannot write its state in {path} ({why}). Inside a "
          f"sandboxed agent, run this command with permission to write outside the workspace "
          f"(the agent may ask you to approve it), or run it from a terminal as "
          f"`~/.tokeneconomizer/bin/te {' '.join(sys.argv[1:]) or '...'}`.")
    return 1


def _session_ctx(args, session_id):
    """The context of a recorded session: the repository it is pinned to, when that repository
    holds its record (a fold handed off at SessionEnd), else the one `--cwd` names. Only a
    session's own hooks pin it; this never does."""
    ctx = _ctx(args)
    if session_id and repos.pinned(ctx.root, ctx.host.name, session_id) is not None:
        pinned = _ctx(args, session_id=session_id)
        if os.path.exists(pinned.store.path("sessions", f"{session_id}.json")):
            return pinned
    return ctx


def _session_note(ctx, env=None):
    """A line when this command runs inside an agent session recorded under another repository
    (the one it started in), else None."""
    env = os.environ if env is None else env
    sid = ctx.host.env_session_id(env)
    pin = repos.pinned(ctx.root, ctx.host.name, sid) if sid else None
    if pin is None or pin.key == ctx.repo.key:
        return None
    return (f"this {ctx.host.name} session started in {pin.root} and is recorded under "
            f"{pin.label} (`te --cwd {pin.root} status`)")


# ---- status / microskills ------------------------------------------------------------------
def _status_explanations(ctx, inj, unt, tracker, applied):
    """Describe the latest saved state, never infer why a past turn did not intervene.

    This deliberately reuses the CLI snapshots, not mechanism internals or fresh model calls.
    The detailed commands already show individual facts and tool evidence.
    """
    cfg = ctx.config
    if not cfg.enabled:
        return {"plugin": "plugin is disabled; hooks record nothing"}
    notes = {}
    if cfg.shadow:
        notes["plugin"] = "shadow mode: new decisions are recorded, not applied"
    host = ctx.host.name
    facts_detail = "see microskills with the same --host/--cwd"
    tools_detail = "see toolprune with the same --host/--cwd"
    paused = sidecall.paused(host, ctx.root)

    if cfg.microskills == "off":
        notes["microskills"] = "off; cached facts are not injected"
    elif inj.get("rendered"):
        notes["microskills"] = ("cached facts present; injection suppressed in shadow mode"
                                if cfg.shadow else "cached facts are available for session start")
        if "distiller" in cfg.microskills and paused:
            notes["microskills"] += "; model proposer paused"
    elif "distiller" in cfg.microskills and paused:
        notes["microskills"] = f"model proposer paused after a sign-in failure; {facts_detail}"
    elif not inj:
        notes["microskills"] = "no learning snapshot recorded for this agent/repository yet"
    else:
        facts = inj.get("facts")
        if not isinstance(facts, dict):
            why = "no fact details recorded in the last learning snapshot"
        else:
            # Metadata is not a fact, and an unfamiliar state is not evidence of zero facts.
            rows = [v for k, v in facts.items()
                    if k not in ("_profile", "_vocab", "_task", "_extractors")]
            states = ("CANDIDATE", "ACTIVE", "RETIRED")
            if any(not isinstance(v, dict) or v.get("state") not in states for v in rows):
                why = "fact lifecycle details unavailable in the last learning snapshot"
            elif rows:
                counts = [sum(v["state"] == state for v in rows) for state in states]
                why = (f"{counts[0]} candidate, {counts[1]} active, "
                       f"{counts[2]} retired; no facts rendered")
            else:
                why = "no facts tracked in the last learning snapshot"
        notes["microskills"] = f"{why}; {facts_detail}"

    if cfg.toolprune == "off":
        notes["toolprune"] = "off; previously applied rules are not restored automatically"
    else:
        proposal = _proposal(ctx, unt)
        held = unt.get("auto_pending")
        pending = held.get("items") if isinstance(held, dict) else None
        pending = pending if isinstance(pending, list) else []
        if proposal:
            if cfg.shadow:
                why = "proposal recorded in shadow mode"
            elif cfg.toolprune == "propose":
                why = "proposal awaiting approval"
            elif unt.get("auto_apply_refused"):
                why = "automatic apply was refused"
            elif any(t in proposal for t in pending):
                why = "proposal held for the idle wait"
            else:
                why = "unapplied proposal recorded"
        elif applied:
            why = "deny rules recorded as applied for this checkout"
        elif not unt:
            why = "no tool-usage snapshot recorded for this agent/repository yet"
        else:
            # A zero proposal can mean used/protected/kept items, not just too little evidence.
            why = "no eligible proposal recorded"
        notes["toolprune"] = f"{why}; {tools_detail}"

    if cfg.loopguard == "off":
        notes["loopguard"] = "off"
    elif tracker.envelope is None:
        notes["loopguard"] = (f"learning baseline: {len(tracker.resolved_turns)}/"
                              f"{tracker.min_samples} ended-green turns")
    elif paused:
        notes["loopguard"] = "judge calls paused after a sign-in failure; backstop remains active"
    else:
        notes["loopguard"] = "baseline ready; per-turn reasons cannot be inferred from these totals"
    return notes


def cmd_status(args):
    ctx = _ctx(args)
    cfg = ctx.config
    out = {"repo": ctx.repo.label, "repo_root": ctx.repo.root, "repo_key": ctx.repo.key,
           "identified_by": ctx.repo.how, "host": ctx.host.name,
           "state": ctx.store.base, "config": cfg.as_dict(), "warnings": cfg.warnings,
           "session_note": _session_note(ctx)}
    inj = ctx.store.read("injection.json") or {}
    unt = ctx.store.read("untool.json") or {}
    env_tracker = loopguard.load_envelope(ctx.store, cfg.loopguard_aggr,
                                          cfg.loopguard_thresholds)
    ledger = accounting.summarize(ctx.store.read_lines("ledger.jsonl"))
    # Sessions imported from the agent's history are counted apart from the plugin's own.
    inj_imported = int(inj.get("imported_sessions", 0) or 0)
    unt_imported = int(unt.get("imported_sessions", 0) or 0)
    out.update({
        "microskills": {"profile": cfg.microskills,
                        "injected_lines": sum(1 for ln in (inj.get("rendered") or "").splitlines()
                                              if ln.startswith("- ")),
                        "sessions_folded": int(inj.get("sessions", 0) or 0) - inj_imported,
                        "imported_sessions": inj_imported},
        "toolprune": {"mode": cfg.toolprune,
                      "sessions": int(unt.get("sessions", 0) or 0) - unt_imported,
                      "prompts": (int(unt.get("prompts", 0) or 0)
                                  - int(unt.get("imported_prompts", 0) or 0)),
                      "imported_sessions": unt_imported,
                      "imported_prompts": int(unt.get("imported_prompts", 0) or 0),
                      "catalog_source": unt.get("catalog_source"),
                      "proposal": unt.get("proposal", []),
                      "applied": denylist.applied_for(ctx.store)},
        "loopguard": {"mode": cfg.loopguard, "envelope": env_tracker.envelope,
                      "ended_green_turns": len(env_tracker.resolved_turns)},
        "history": history.summary(ctx),
        "ledger": ledger})
    if getattr(args, "explain", False):
        out["explanations"] = _status_explanations(ctx, inj, unt, env_tracker,
                                                 out["toolprune"]["applied"])
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    print(f"TokenEconomizer -- {out['repo']} ({ctx.host.name})")
    print(f"  repository   {ctx.repo.root}, {repos.describe(ctx.repo)}")
    print(f"  state        {out['state']}")
    print(f"  config       microskills={cfg.microskills} toolprune={cfg.toolprune} "
          f"loopguard={cfg.loopguard} repomap={cfg.repomap} mode={cfg.mode} "
          f"shadow={cfg.shadow}")
    if cfg.repomap != "off":
        rm = repomap.read_state(ctx)
        why = rm.get("error") or "made after the first session"
        print("  repomap      " + (f"{rm.get('chars', 0) // 4} tokens cached for "
                                    f"{(rm.get('commit') or '?')[:10]}" if rm.get("map") else
                                    f"none yet ({why})"))
    for w in cfg.warnings:
        print(f"  warning      {w}")
    if out["session_note"]:
        print(f"  note         {out['session_note']}")
    def earlier(n):
        return f" (+{n} from earlier history)" if n else ""
    print(f"  sessions     {ledger['sessions']} folded{earlier(ledger['imported_sessions'])}, "
          f"{accounting.fmt_tokens(ledger['effective_tokens'])} effective tokens, plugin "
          f"overhead {accounting.fmt_tokens(ledger['overhead_tokens'])} "
          f"({ledger['overhead_frac']:.1%})")
    hist = history.describe(out["history"], ctx.host.name)
    if hist:
        print(f"  history      {hist}")
    i = out["microskills"]
    print(f"  microskills  {i['injected_lines']} fact(s) injected at session start "
          f"({i['sessions_folded']} sessions learned from"
          + (f", +{i['imported_sessions']} from earlier history" if i["imported_sessions"]
             else "") + ")")
    u = out["toolprune"]
    src = u["catalog_source"] or "none yet"
    print(f"  toolprune    {u['sessions']} sessions ({u['prompts']} prompts) observed"
          + (f" (+{u['imported_sessions']} from earlier history, {u['imported_prompts']} "
             f"prompts)" if u["imported_sessions"] else "")
          + f", catalog: {src}; "
          f"proposed tools: "
          f"{', '.join(u['proposal']) or '-'}; applied: {', '.join(u['applied']) or '-'}")
    sp = out["loopguard"]
    env_s = f"{sp['envelope']:.0f} tool calls" if sp["envelope"] else "not learned yet"
    print(f"  loopguard    envelope {env_s} ({sp['ended_green_turns']} ended-green turns); "
          f"{ledger['nudges']} redirect(s), {ledger['aborts']} still-stuck notice(s) over "
          f"{ledger['turns']} turns")
    for name, why in out.get("explanations", {}).items():
        print(f"  explain      {name}: {why}")
    return 0


def _config_names(path, key):
    """Whether the config file at `path` names `key`."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and key in doc


def _microskills_origin(ctx):
    """Where this repository's effective microskills value comes from, in config merge order:
    the environment, then the repository's config, then the global one, else the default."""
    if ctx.env.get("TE_MICROSKILLS"):
        return "environment TE_MICROSKILLS"
    for label, path in (("repo config", os.path.join(os.path.dirname(ctx.store.base),
                                                     "config.json")),
                        ("global config", os.path.join(ctx.root, "config.json"))):
        if _config_names(path, "microskills"):
            return label
    return ""


def cmd_microskills(args):
    if args.action in ("forget", "unforget"):
        return _microskills_forget(args)
    if args.ids:
        print(f"`microskills {args.action}` takes no ids; did you mean `microskills forget "
              f"{' '.join(args.ids)}`?")
        return 2
    ctx = _ctx(args)
    inj = ctx.store.read("injection.json") or {}
    value = ctx.config.microskills
    origin = _microskills_origin(ctx)
    print(f"micro-skills is {'off' if value == 'off' else 'on'} here "
          + (f"({origin}: {value}; default off)" if origin else "(the default)"))
    if value == "off":
        print(f"`config set microskills=on` turns it on: facts are then learned from finished "
              f"sessions, and a model proposes more every "
              f"{ctx.config.microskills_propose_every} sessions, through this agent")
    text = inj.get("rendered") or ""
    if text:
        print(text.rstrip())
    else:
        # Every fact counts its own exercised sessions, so this is the shortest wait any new
        # fact faces; sessions imported from before the plugin were not folded here.
        from tokeneconomizer.microskills.lifecycle import FIRE_GATE, FIRE_MIN_N, K_PROMOTE
        folded = max(0, int(inj.get("sessions", 0) or 0)
                     - int(inj.get("imported_sessions", 0) or 0))
        print(f"{min(folded, FIRE_MIN_N)}/{FIRE_MIN_N} sessions toward firing gate")
        print(f"a fact is promoted after {K_PROMOTE} confirmations and injected only once it "
              f"fired in {FIRE_GATE:.0%} of at least {FIRE_MIN_N} sessions that exercised it; "
              f"only sessions folded while micro-skills was on here count")
        print("(nothing injected yet: no fact has cleared its firing gate)")
    if not inj:
        return 0
    facts = inj.get("facts") or {}
    rows = [(k, v) for k, v in facts.items() if isinstance(v, dict) and "state" in v]
    if rows:
        print("\nfacts tracked:")
        for k, v in sorted(rows):
            print(f"  {v.get('state', '?'):9s} fire={v.get('fire', '?'):>5s} "
                  f"{'INJECTED' if v.get('inject') else '        '} {k}")
    from tokeneconomizer.microskills import forgotten_facts
    gone = forgotten_facts(ctx.repo.label, os.path.join(ctx.store.base, "mechanisms"))
    if gone:
        print("\nforgotten (`microskills unforget ID` brings one back):")
        for k, claim in sorted(gone.items()):
            print(f"  {k}" + (f"  ({_short(claim)})" if claim else ""))
    return 0


def _short(text, n=80):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "\u2026"


def _microskills_forget(args):
    from tokeneconomizer.microskills.lifecycle import NO_RECORD
    undo = args.action == "unforget"
    if not args.ids:
        print(f"`microskills {args.action}` needs the id of at least one fact; "
              f"`microskills` lists them")
        return 2
    ctx = _ctx(args)
    refused = _refuse_unwritable(ctx.root)
    if refused:
        return refused
    try:
        done = fold.forget_microskills(ctx, args.ids, undo=undo)
    except KeyError as e:
        unknown = e.args[0] if e.args and isinstance(e.args[0], list) else list(e.args)
        print(f"not changed: no learned fact here has the id {', '.join(unknown)} "
              f"({ctx.host.name}, {ctx.repo.label}); `microskills` lists the ids")
        return 1
    except fold.Busy as e:
        print(f"not changed: {e}; try again in a moment")
        return 1
    for fid, changed, claim in done:
        if changed == NO_RECORD:
            print(f"no longer forgotten, but nothing to bring back: {fid} had no saved record; "
                  f"a later fold may learn it again")
            continue
        what = (("brought back" if changed else "not forgotten, nothing to bring back") if undo
                else ("forgotten" if changed else "already forgotten"))
        print(f"{what}: {fid}" + (f"  ({_short(claim)})" if claim else ""))
    if any(changed for _, changed, _ in done):
        print("from the next session, " + (
            "these facts are kept and injected under the same rules as any other fact" if undo
            else "these facts are not injected, and are not learned again under the same id "
                 "or from the same evidence "
                 "(`microskills unforget ID` undoes this)"))
    return 0


# ---- toolprune -----------------------------------------------------------------------------
def _wrap(line, indent=""):
    """Fill one long line to the terminal, without splitting a tool name or a pattern."""
    return textwrap.fill(line, width=max(shutil.get_terminal_size().columns, 40),
                         initial_indent=indent, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False)


def _toolprune_show(ctx, unt):
    host = ctx.host.name
    applied = denylist.applied_for(ctx.store)
    proposal = _proposal(ctx, unt)
    kept = denylist.kept_for(ctx.store)
    waiting = [t for t in unt.get("needs_apply") or [] if t in proposal]
    held = unt.get("auto_pending")
    pending = [t for t in (held.get("items") if isinstance(held, dict) else None) or []
               if t in proposal]
    print(f"sessions observed: {unt.get('sessions', 0)} ({unt.get('prompts', 0)} prompts)  "
          f"catalog: {unt.get('catalog_source') or 'none yet'}")
    print(f"applied (blocked by this plugin): {', '.join(applied) or '->'}")
    print(f"proposed: {', '.join(proposal) or '->'}")
    print(f"pending: {', '.join(pending) or '->'}")
    print(f"kept (gave back, never re-applied in auto): {', '.join(kept) or '->'}")
    if unt.get("imported_sessions"):
        print(_wrap(f"of which {unt['imported_sessions']} from before the plugin "
                    f"({unt.get('imported_prompts', 0)} prompts), read from the agent's history"))
    if waiting and ctx.config.toolprune == "auto":
        print(_wrap(f"waiting for `toolprune apply`: {', '.join(waiting)} (the evidence for them "
                    f"includes sessions from before the plugin, so `auto` does not write them)"))
    if pending and ctx.config.toolprune == "auto":
        print(_wrap(f"written once this repository's sessions have been idle "
                    f"{ctx.config.toolprune_quiet_minutes} minutes, so that no conversation you "
                    f"resume re-reads its cached prompt (`toolprune apply` writes it now)",
                    indent="  "))
    for tip in ("apply: writes the proposed list now; it takes effect from the next session",
                "restore: gives entries back; what you give back is kept, and `auto` never "
                "re-applies it (`toolprune apply NAME` removes one again)",
                "protect: never proposes a tool by name or pattern (`config set "
                "toolprune_protect=NAME`)",
                "apply-pending: writes a pending auto list once this repository's sessions have "
                "been idle"):
        print(_wrap(tip))
    streaks = unt.get("streaks") or {}
    if streaks:
        cfg = ctx.config
        prompts = unt.get("prompt_streaks") or {}
        print(f"how long each tool has gone unused (a tool never used is left out after "
              f"{cfg.toolprune_evidence} prompts over {cfg.toolprune_min_sessions}+ sessions, "
              f"or after {cfg.toolprune_sessions} sessions):")
        for t, n in sorted(streaks.items(), key=lambda kv: (-kv[1], -prompts.get(kv[0], 0)))[:30]:
            used = "used" if t in set(unt.get("used_tools", [])) else ""
            print(f"  {n:4d} sessions {prompts.get(t, n):5d} prompts  {t} {used}")
    if not unt.get("catalog_source") or unt.get("catalog_source") == "observed":
        if os.path.exists(os.path.join(ctx.root, "hosts", host, "catalog.json")):
            # Recorded, but a repository reads the catalog when its next session is folded.
            print(_wrap("note: the catalog omp reported joins this repository's evidence from "
                        "its next session."))
        else:
            print(_wrap("note: omp reports its tool catalog at each SessionStart; until one is "
                        "recorded only observed tools are known, which leaves the always-loaded "
                        "tools out of the evidence."))
    return 0


def _wait_and_apply(args):
    """`toolprune apply-pending`, which the fold leaves running, detached: wait until the pending
    `auto` deny list can be written without costing a warm prompt cache (`fold.apply_pending`),
    looking again as sessions come and go, then write it. One waiter per repository and agent:
    a second finds the first one's heartbeat fresh and leaves."""
    ctx = _ctx(args)
    beat = ctx.store.path("toolprune-waiter")
    try:
        if time.time() - os.path.getmtime(beat) < 2 * fold.WAITER_POLL_S:
            return 0
    except OSError:
        pass
    ctx.store.ensure()
    with open(beat, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    try:
        while True:
            wait = fold.apply_pending(ctx)
            if wait is None:
                return 0
            time.sleep(min(max(wait, 1.0), fold.WAITER_POLL_S))
            os.utime(beat)
            ctx = _ctx(args)            # the settings may have changed meanwhile
    finally:
        try:
            os.remove(beat)
        except OSError:
            pass


def _proposal(ctx, unt):
    """The proposal, less what this checkout already applied or gave back: the clones of one
    repository share its evidence, and one of them may have applied the list before another."""
    mine = set(denylist.applied_for(ctx.store))
    mine |= set(denylist.kept_for(ctx.store))
    return fold.unprotected(ctx, [t for t in unt.get("proposal", []) if t not in mine])


def cmd_toolprune(args):
    ctx = _ctx(args)
    unt = ctx.store.read("untool.json") or {}
    action = args.action
    if action == "show":
        return _toolprune_show(ctx, unt)
    if action in ("apply", "restore") and _refuse_unwritable(ctx.store.base):
        return 1
    if action == "apply":
        tools = args.tools or _proposal(ctx, unt)
        if not tools:
            print("nothing to apply")
            return 0
        try:
            added = denylist.apply(ctx.store, tools)
        except RuntimeError as e:
            print(f"not applied: {e}")
            return 1
        # Named by the person: no longer one they gave back, whatever `restore` said before.
        denylist.set_kept(ctx.store, remove=tools)
        print(f"added: {', '.join(added) or '(already present)'}\n"
              "the omp session tool list (applied by the extension at session start) -- "
              "takes effect from the next omp session.")
        return 0
    if action == "restore":
        try:
            removed = denylist.restore(ctx.store, args.tools or None)
        except RuntimeError as e:
            print(f"not restored: {e}")
            return 1
        # Given back by the person: `toolprune: auto` must not take them away again at the next
        # fold, and a proposal does not ask for them again.
        denylist.set_kept(ctx.store, add=removed)
        print(f"removed: {', '.join(removed) or '-'} (from the next session)"
              + (f"; kept from now on (`toolprune apply {removed[0]}` removes one again)"
                 if removed else ""))
        return 0
    if action == "apply-pending":
        return _wait_and_apply(args)
    if action == "refresh-catalog":
        print("omp reports its tool catalog at each SessionStart; the plugin records it then.")
        return 0
    if action == "measure":
        print("not measured: omp pruning is applied by its extension")
        return 1
    return 2


def cmd_repo_key(args):
    print(paths.repo_identity(args.cwd).key)
    return 0


# ---- repomap ------------------------------------------------------------------------------
def cmd_repomap(args):
    ctx = _ctx(args)
    if args.action == "refresh":
        st = repomap.refresh(ctx)
    else:
        st = repomap.read_state(ctx)
    if not st.get("map"):
        print(f"no map cached for {ctx.repo.label}" + (f": {st['error']}" if st.get("error")
                                                      else ""))
        if ctx.config.repomap == "off":
            print("the map is off; turn it on with `te config set repomap=on`")
        return 1 if args.action == "refresh" else 0
    current = st.get("commit") == repomap.head_commit(ctx.repo.root)
    print(f"{ctx.repo.label}: ~{st['chars'] // 4} tokens (budget {st.get('tokens_budget')}), "
          f"commit {(st.get('commit') or '?')[:10]}{'' if current else ' (an older commit)'}, "
          f"made in {st.get('secs')}s with {st.get('python')}")
    if st.get("error"):
        print(f"last refresh failed: {st['error']}")
    print(repomap.HEADER + st["map"][:2000] + ("\n..." if len(st["map"]) > 2000 else ""))
    return 0


# ---- config / doctor / fold ----------------------------------------------------------------
def _config_view(cfg):
    """The settings as a person reads them: each mechanism on or off, its settings, and the
    guard's thresholds with the numbers a `loopguard.<name>` setting would replace."""
    d = cfg.as_dict()
    pinned = d.pop("loopguard_thresholds")
    return {
        "enabled": d.pop("enabled"),
        "microskills": {"state": "off" if cfg.microskills == "off" else "on",
                        "microskills": d.pop("microskills"),
                        "microskills_max_facts": d.pop("microskills_max_facts"),
                        "microskills_max_tokens": d.pop("microskills_max_tokens"),
                        "microskills_propose_every": d.pop("microskills_propose_every")},
        "toolprune": {"state": "off" if cfg.toolprune == "off" else "on",
                      "toolprune": d.pop("toolprune"),
                      "toolprune_evidence": d.pop("toolprune_evidence"),
                      "toolprune_min_sessions": d.pop("toolprune_min_sessions"),
                      "toolprune_sessions": d.pop("toolprune_sessions"),
                      "toolprune_quiet_minutes": d.pop("toolprune_quiet_minutes"),
                      "toolprune_protect": d.pop("toolprune_protect")},
        "loopguard": {
            "state": "off" if cfg.loopguard == "off" else "on",
            "loopguard": d.pop("loopguard"), "loopguard_aggr": d.pop("loopguard_aggr"),
            "loopguard_nudges": d.pop("loopguard_nudges"),
            "thresholds": loopguard.base_thresholds(cfg.loopguard_aggr, pinned),
            "pinned": sorted(pinned)},
        "other": d,
        "warnings": cfg.warnings,
    }


def _seed_global_config(root):
    """Create the global config file with every setting at its default, when it is not there.

    `show` is where a person looks for the file to edit, so it must exist by the time they open
    it. A file already there is never rewritten, and a home that cannot hold one is ignored:
    showing the settings must not fail with it."""
    path = os.path.join(root, "config.json")
    if os.path.exists(path):
        return
    # Tmp-plus-rename like config._write: the file either appears complete or not at all,
    # so a failed dump can never strand a truncated config.json that blocks later seeds.
    tmp = path + ".tmp"
    try:
        os.makedirs(root, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(LiveConfig().as_dict(), fh, indent=2, sort_keys=True)
        if os.path.exists(path):
            os.remove(tmp)
        else:
            os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def cmd_config(args):
    ctx = _ctx(args)
    if args.action == "show":
        _seed_global_config(ctx.root)
        print(json.dumps(_config_view(ctx.config), indent=2))
        return 0
    path = (os.path.join(ctx.root, "config.json") if args.globally
            else os.path.join(os.path.dirname(ctx.store.base), "config.json"))
    if _refuse_unwritable(os.path.dirname(path)):
        return 1
    if args.action == "unset":
        try:
            cur = unset_config(path, [k.strip() for k in args.pairs])
        except ValueError as e:
            print(f"rejected: {e}")
            return 2
        print(f"{path}: {json.dumps(cur, sort_keys=True)}")
        return 0
    updates = {}
    for kv in args.pairs:
        if "=" not in kv:
            print(f"expected KEY=VALUE, got {kv!r}")
            return 2
        k, v = kv.split("=", 1)
        updates[k.strip()] = v.strip()
    if not updates:
        print("nothing to set: give KEY=VALUE (see `te config show` for the keys)")
        return 2
    try:
        cur = write_config(path, updates)
    except ValueError as e:
        print(f"rejected: {e}")
        return 2
    print(f"{path}: {json.dumps(cur, sort_keys=True)}")
    print("applies from the next hook call; a new fact-size cap applies once the next session "
          "has been folded")
    if set(updates) & {"microskills", "loopguard"} and sidecall.resume(
            ctx.host.name, ctx.root):
        print(f"the plugin's model calls through `{sidecall.call_name(ctx.host.name)}` will be "
              f"tried again")
    if "distiller" in str(cur.get("microskills", "")):
        every = cur.get("microskills_propose_every") or ctx.config.microskills_propose_every
        print(f"note: micro-skills calls a model through this agent every {every} sessions to "
              "propose facts, charged to the session's cost; `microskills=envfacts` learns "
              "without model calls")
    if cur.get("loopguard") == "stop":
        print("note: `stop` acts as `nudge`: the plugin never stops a turn; a turn still stuck "
              "after the redirect gets a notice saying how to stop it")
    return 0


def cmd_fold(args):
    # A session is folded into the repository it is pinned to, wherever `--cwd` points.
    ctx = _session_ctx(args, (args.session or [None])[0])
    sids = args.session or fold.pending_sessions(ctx)
    for sid in sids:
        rec = fold.fold_session(ctx, sid)
        if rec is not None and args.verbose:
            print(json.dumps(rec, default=str))
    return 0


def _print_history(ctx, doc):
    if not doc:
        print(f"no import of earlier sessions recorded for {ctx.repo.label}")
        return
    print(f"{ctx.repo.label}: {history.describe(doc, ctx.host.name) or doc.get('state')}")
    skipped = doc.get("skipped") if isinstance(doc.get("skipped"), dict) else {}
    if doc.get("over_limit"):
        skipped = dict(skipped, older_than_the_newest=doc["over_limit"])
    if skipped:
        print("left out: " + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())))


def cmd_history(args):
    """`te history [import]`: what an import of earlier sessions did here."""
    ctx = _ctx(args)
    if args.action == "status" or args.status:
        _print_history(ctx, history.summary(ctx))
        return 0
    print(f"{ctx.host.name}: no session history the plugin reads")
    return 1


def cmd_judge(args):
    """The detached half of the stuck judge (see plugin/judge.py); not for people."""
    from . import judge
    judge.run_job(_session_ctx(args, args.session), args.session, args.job)
    return 0


# ---- repos ----------------------------------------------------------------------------------
def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _print_moves(done):
    """One line per move a migration made; returns how many there were."""
    n = 0
    for r in done or ():
        for m in r.get("moves") or ():
            n += 1
            what = "all of its state" if r.get("into") == m["into"] else f"{m['moved']} session(s)"
            print(f"  {r['key']} -> {m['into']} ({m['host']}): {what}"
                  + (f", {m['refold']} to be folded there again" if m.get("refold") else ""))
    return n


def cmd_repos(args):
    root = state_root()
    if args.action in ("migrate", "merge") and _refuse_unwritable(root):
        return 1
    if args.action == "migrate":
        done = repos.run_migration(root)
        if done is None:
            print("another migration holds the state root; try again in a minute")
            return 1
        if not args.quiet:
            if not _print_moves(done):
                print("nothing to move: every session is filed under the repository it ran in")
        return 0
    if args.action == "merge":
        if not args.keys or len(args.keys) > 2:
            print("usage: te repos merge KEY [INTO] (keys as `te repos` lists them)")
            return 2
        # A key names one directory in `repos/`: `.`, `""` or a path would name `repos/`
        # itself, or somewhere else, and the merge would set all of it aside.
        bad = [k for k in args.keys if k in ("", ".", "..") or os.path.basename(k) != k
               or "/" in k or "\\" in k]
        if bad:
            print(f"not a state directory key: {bad[0]!r} (keys as `te repos` lists them)")
            return 2
        src = args.keys[0]
        if not os.path.isdir(os.path.join(repos.repos_dir(root), src)):
            print(f"no state directory {src!r} in {repos.repos_dir(root)}")
            return 2
        into = args.keys[1] if len(args.keys) > 1 else _ctx(args).repo
        into_key = into if isinstance(into, str) else into.key
        if into_key == src:
            print(f"{src} is this repository's own state; name the one to merge into it")
            return 2
        res = repos.merge(root, src, into)
        if res is None:
            print("the state root or one of the two directories is busy; try again in a minute")
            return 1
        print(f"merged {src} into {into_key}: " + (", ".join(
            f"{h} {c['moved']} session(s)" + (f" ({c['refold']} to be folded again)"
                                             if c["refold"] else "")
            for h, c in sorted(res.items())) or "no sessions"))
        print(f"the old directory is kept under {os.path.join(root, 'merged')}")
        if not isinstance(into, str):
            repos.refold_moved([{"moves": [dict(c, host=h, cwd=into.root)
                                           for h, c in res.items()]}])
        return 0
    current = _ctx(args).repo
    rows, merged = repos.listing(root, current=current)
    if args.json:
        print(json.dumps({"repos": rows, "merged": merged}, indent=2, default=str))
        return 0
    if not rows:
        print(f"no state yet in {repos.repos_dir(root)}")
    for row in rows:
        print(f"{row['label']}  [{row['key']}]" + ("  <- this directory" if row["current"]
                                                  else ""))
        print("  identified   " + (repos.how_words(row["how"], row["remote"]) if row["how"]
                                   else "recorded by an earlier version"))
        imported = row.get("imported") or {}
        print("  sessions     " + (", ".join(
            f"{h} {n}" + (f" (+{imported[h]} from earlier history)" if imported.get(h) else "")
            for h, n in row["agents"].items()) or "none") + f"; last {_when(row['last'])}")
        if row["checkouts"]:
            print("  checkouts    " + ", ".join(row["checkouts"]))
        elif row["cwds"]:
            print("  ran in       " + ", ".join(row["cwds"][:3])
                  + (f" (+{len(row['cwds']) - 3})" if len(row["cwds"]) > 3 else ""))
        for n in row["notes"]:
            print(f"  note         {n}")
    if merged:
        print(f"\n{merged} earlier state director{'y' if merged == 1 else 'ies'} merged into "
              f"the above, kept under {os.path.join(root, 'merged')}")
    return 0


def cmd_probe(args):
    print("the session probe is not available for omp: it needs another agent's session logs")
    return 2


def cmd_doctor(args):
    ok = sys.version_info >= (3, 10)
    print(f"python      {sys.version.split()[0]} {'ok' if ok else '(needs >= 3.10)'}")
    root = state_root()
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".write-test")
        open(probe, "w", encoding="utf-8").close()
        os.remove(probe)
        print(f"state root  {root} (writable)")
    except OSError as e:
        print(f"state root  {root} NOT writable: {e}")
        ok = False
    import shutil as _sh
    on_path = _sh.which("omp")
    print(f"omp         {'on PATH: ' + on_path if on_path else 'not on PATH'}")
    ctx = _ctx(args)          # the lines below read the settings in effect
    pause = sidecall.paused("omp", root)
    if pause:
        print(f"omp         model calls paused since "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(pause['at']))}: "
              f"`{sidecall.call_name('omp')}` said it is not signed in; "
              f"`config set microskills=on` (or `loopguard=on`) tries again")
    cat = os.path.join(root, "hosts", "omp", "catalog.json")
    print(f"omp         catalog {'present' if os.path.exists(cat) else 'not reported yet'}")
    print(f"repository  {ctx.repo.root}: {repos.describe(ctx.repo)}; state {ctx.repo.key}")
    home_git = os.path.join(paths.home_dir(), ".git")
    for path in repos.ignored_git_entries(args.cwd):
        if os.path.realpath(path) != home_git:          # the home line below says it
            print(f"repository  ignored {path}: not a git repository (no HEAD, or a dangling "
                  f"gitdir)")
    note = repos.home_git_note()
    if note:
        print(f"home        {note}")
    if repos.needs_migration(root):
        print("state       recorded by an earlier version and not yet migrated: `te repos "
              "migrate` (runs by itself at the next session start)")
    py = repomap.find_python(ctx)
    print(f"repomap     {ctx.config.repomap}; " + (f"aider found ({py})" if py else
          "no Python with aider found (pip install aider-chat, or set repomap_python)"))
    rtk = _sh.which("rtk")
    if rtk:
        print(f"rtk         {rtk}. Compatible. Note: `rtk pytest` prints nothing when pytest is "
              f"missing, so the plugin cannot learn that fact from it")
    log = os.path.join(root, "errors.log")
    if os.path.exists(log):
        tail = open(log, encoding="utf-8", errors="replace").read().splitlines()[-6:]
        print("errors.log (tail):\n  " + "\n  ".join(tail))
    else:
        print("errors.log  none")
    return 0 if ok else 1


# ---- help ----------------------------------------------------------------------------------
# One line per user-facing subcommand: name, key argument, what it does. The plumbing a person
# never types (fold, judge, probe) stays out; a test checks every name against the parser, so
# this list cannot name a command that `te` does not accept.
_HELP = (
    ("status", "", "what TokenEconomizer learned here and did"),
    ("microskills", "", "the facts it injects"),
    ("toolprune", "[show|apply|restore]", "the proposed deny list"),
    ("config", "[show|set|unset]", "turn mechanisms on/off"),
    ("repos", "[list|migrate|merge]", "every repository with state here"),
    ("history", "[status|import]", "sessions from before the plugin"),
    ("repomap", "[show|refresh]", "the cached repository map"),
    ("doctor", "", "check the installation"),
)


def help_lines():
    return [f"{name}{f' {arg}' if arg else ''} \u2014 {about}" for name, arg, about in _HELP]


def cmd_help(args):
    print("\n".join(help_lines()))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="te", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--host", default=None)  # accepted but ignored: omp is the only host
    # The same two options are accepted after the subcommand too (`te fold --host omp ...`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", default=argparse.SUPPRESS)
    common.add_argument("--host", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    ap.set_defaults(fn=cmd_help)  # bare `te` prints the list, as `te help` does

    def add(name, *aliases):
        return sub.add_parser(name, aliases=list(aliases), parents=[common])

    add("help").set_defaults(fn=cmd_help)
    p = add("status")
    p.add_argument("--json", action="store_true")
    p.add_argument("--explain", action="store_true",
                   help="briefly explain current settings and saved state (no model calls)")
    p.set_defaults(fn=cmd_status)
    p = add("microskills")
    p.add_argument("action", nargs="?", default="show", choices=("show", "forget", "unforget"),
                   help="show the facts (default); forget: stop injecting them and never learn "
                        "them again; unforget: bring forgotten facts back")
    p.add_argument("ids", nargs="*", metavar="ID", help="fact ids, as `te microskills` lists them")
    p.set_defaults(fn=cmd_microskills)
    p = add("toolprune")
    p.add_argument("action", nargs="?", default="show",
                   choices=("show", "apply", "restore", "refresh-catalog", "measure",
                            "apply-pending"))
    p.add_argument("tools", nargs="*")
    p.set_defaults(fn=cmd_toolprune)
    p = add("config")
    p.add_argument("action", nargs="?", default="show", choices=("show", "set", "unset"))
    p.add_argument("pairs", nargs="*")
    p.add_argument("--global", dest="globally", action="store_true")
    p.set_defaults(fn=cmd_config)
    p = add("fold")
    p.add_argument("--session", action="append")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_fold)
    p = add("judge")
    p.add_argument("--session", required=True)
    p.add_argument("--job", required=True)
    p.set_defaults(fn=cmd_judge)
    add("doctor").set_defaults(fn=cmd_doctor)
    p = add("repos")
    p.add_argument("action", nargs="?", default="list", choices=("list", "migrate", "merge"))
    p.add_argument("keys", nargs="*")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_repos)
    p = add("history")
    p.add_argument("action", nargs="?", default="status", choices=("status", "import"))
    p.add_argument("--dry-run", action="store_true",
                   help="say what would be imported, change nothing")
    p.add_argument("--status", action="store_true", help="what the import did here")
    p.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_history)
    p = add("repo-key")
    p.set_defaults(fn=cmd_repo_key)
    p = add("repomap")
    p.add_argument("action", nargs="?", default="show", choices=("show", "refresh"))
    p.set_defaults(fn=cmd_repomap)
    p = add("probe")
    p.add_argument("--sessions", type=int, default=6)
    p.add_argument("--model", default=None)
    p.add_argument("--keep", action="store_true")
    p.set_defaults(fn=cmd_probe)
    return ap



def _split_argline(line):
    """Split on whitespace and quotes as a POSIX shell would, but keep a backslash as typed:
    on Windows `C:\\Python312\\python.exe` is a path, not two escapes. An unclosed quote
    raises ValueError."""
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    lex.escape = ""
    lex.commenters = ""
    return list(lex)


def _expand_argline(argv):
    """`--argline TEXT` stands for TEXT split by `_split_argline`: a host's slash command
    (Pi's `/te ...`) hands over the line as typed, and a fact id may hold spaces and quotes."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(argv):
        if a == "--argline" and i + 1 < len(argv):
            return argv[:i] + _split_argline(argv[i + 1]) + argv[i + 2:]
        if a.startswith("--argline="):
            return argv[:i] + _split_argline(a.split("=", 1)[1]) + argv[i + 1:]
    return argv


def main(argv=None):
    try:
        argv = _expand_argline(argv)
    except ValueError as e:
        print(f"te: could not read the arguments: {e}")
        return 2
    args = build_parser().parse_args(argv)
    args.host = "omp"
    try:
        return args.fn(args) or 0
    finally:
        from .paths import secure_root
        secure_root()


if __name__ == "__main__":
    sys.exit(main())
