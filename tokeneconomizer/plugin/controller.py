"""SessionStart's injection: what a session opens with.

`turnsession` owns when this runs serialized (the session lock, the queue, the save); this
module owns what it injects: the repository's micro-skills (cached by the last fold), the
repository map (`repomap`), and the tool-prune deny list. Every handler returns quickly and
fails open. Every visible action is announced in one `systemMessage` line, so a person can see
why the agent changed course; `shadow` computes and logs every decision without acting on any
of them.
"""
import hashlib

from . import denylist, fold, repomap, repos, sidecall
from .hosts import merge_outputs


def inject_session_start(call):
    """The SessionStart outputs for one call; the session document, saved once here."""
    source = call.p.get("source") or "startup"
    s = call._session()
    s["source"] = source
    s.setdefault("cwd", call.host.cwd(call.p, call.ctx.env))
    outputs = []
    seen = call.store.read("notices.json") or {}
    seen_before = dict(seen)

    if call.cfg.microskills != "off":
        inj = call.store.read("injection.json") or {}
        text = inj.get("rendered") or ""
        # A resumed or forked conversation already carries what the first start injected,
        # where the agent replays it; a cleared or compacted one has lost it. ("new" is
        # Copilot's word for "startup".)
        fresh = source in ("startup", "new", "clear", "compact")
        if not fresh:
            # The agent rebuilt the conversation without what its first start added: the
            # same text again puts it back as it was (and as its prompt cache has it), so a
            # session that started without facts gets none either.
            text = s.get("injected_text", text if s.get("injected_chars") else "")
        if text:
            n_facts = sum(1 for ln in text.splitlines() if ln.startswith("- "))
            call._log("inject", chars=len(text), facts=n_facts, source=source)
            if not call.cfg.shadow:
                outputs.append(call.host.context("SessionStart", text))
                s["injected_chars"] = len(text)
                s["injected_text"] = text
                if "rendered_ids" in inj:     # which facts, for this session's fold
                    s["injected_ids"] = list(inj["rendered_ids"])
                digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
                if seen.get("injection") != digest:
                    outputs.append(call.host.notice(
                        f"TokenEconomizer: added {n_facts} learned fact(s) about "
                        f"{call.ctx.repo.label} to this session's context "
                        f"(review with `{call._cmd('microskills')}`)."))
                    seen["injection"] = digest

    if call.cfg.toolprune != "off" and not call.cfg.shadow:
        outputs.extend(_toolprune_notices(call, seen))

    pause = sidecall.paused(call.host.name, call.ctx.root)
    calls_a_model = "distiller" in call.cfg.microskills or call.cfg.loopguard != "off"
    if (pause and calls_a_model and not call.cfg.shadow
            and seen.get("sidecall_paused") != pause["at"]):
        outputs.append(call.host.notice(
            f"TokenEconomizer: the plugin's own model calls go through "
            f"`{sidecall.call_name(call.host.name)}`, "
            f"which answered that it is not signed in, so they are paused; facts that "
            f"need no model call are still learned, and the loop guard redirects only at its "
            f"backstop. `{call._cmd('config')} set microskills=on` (or `loopguard=on`) tries "
            f"again."))
        seen["sidecall_paused"] = pause["at"]

    note = repos.migration_notice(call.ctx.root, call._cmd("repos"))
    if note and not call.cfg.shadow:
        outputs.append(call.host.notice(note))

    if call.cfg.repomap != "off":
        if repomap.stale(call.ctx):
            repomap.spawn_refresh(call.ctx)
        at_start = call.cfg.repomap == "start"
        if at_start and source in ("startup", "new", "clear", "compact"):
            outputs.extend(call._inject_map(s, seen, repomap.cached_block(call.ctx),
                                           "SessionStart", "cached"))

    if seen != seen_before:
        call.store.write("notices.json", seen)
    call._save(s)
    repos.note_repo(call.ctx.root, call.ctx.repo)
    repos.prune_pins(call.ctx.root, call.host.name)
    repos.start_migration(call.ctx)
    fold.spawn_fold(call.ctx, fold.pending_sessions(call.ctx, exclude=(call.sid,)))
    return merge_outputs(*outputs)


def _toolprune_notices(call, seen):
    """The deny list's lines at session start, each said once: what `auto` removed from this
    checkout's sessions (from this one on), and a proposal still waiting -- under `propose`,
    or when `auto` could not write the settings file."""
    out = []
    cmd, unit = call._cmd("toolprune"), "tool"
    unt = call.store.read("untool.json") or {}
    mine = sorted(denylist.applied_for(call.store))

    def few(items):
        return ", ".join(items[:4]) + ("..." if len(items) > 4 else "")
    if call.cfg.toolprune == "auto" and seen.get("untool_applied", "") != ",".join(mine):
        if mine:
            out.append(call.host.notice(
                f"TokenEconomizer removed {len(mine)} {unit}(s) this repository's sessions "
                f"never used ({few(mine)}), from this session on. Review with `{cmd}`; "
                f"`{cmd} restore` gives them back."))
        seen["untool_applied"] = ",".join(mine)
    # What this checkout already applied, or gave back, is no news here (another clone may
    # not have).
    kept = set(denylist.kept_for(call.store))
    prop = [t for t in unt.get("proposal") or [] if t not in mine and t not in kept]
    refused = unt.get("auto_apply_refused") if call.cfg.toolprune == "auto" else None
    # Under `auto`, a list waiting for the repository to go quiet is no news yet: the
    # session start after it is written says so (above). A refusal is.
    if call.cfg.toolprune == "auto" and not refused:
        held = set(((unt.get("auto_pending") or {}).get("items") or []))
        prop = [t for t in prop if t not in held]
    if prop and seen.get("untool") != ",".join(prop):
        out.append(call.host.notice(
            f"TokenEconomizer: {len(prop)} {unit}(s) went unused in this repository's recent "
            f"sessions ({few(prop)})"
            + f". Review with `{cmd}`; "
            + (f"they could not be applied ({refused}), and `{cmd} apply` tries again."
               if refused else f"nothing changes until you run `{cmd} apply`.")
        ))
        seen["untool"] = ",".join(prop)
    return out
