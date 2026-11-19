"""One session's turn lifecycle: its lock, its queue, its held decision, its turn close, its save.

One session's hooks run one at a time. A hook that finds its session's lock held leaves its
payload in the session's queue and answers `{}`; the session's next hook applies what queued,
oldest first, each removed before it is applied, so a crash loses an event rather than applies
it twice. A decision reached on a queued call waits for the turn's next live call, since the
queued call's hook already answered; a turn that ends first drops it and logs `undelivered`.
Every applied event is saved once, and a hook that cannot apply anything answers `{}`.

`handle` never raises: whatever goes wrong, the hook answers `{}` (which changes nothing) and
the traceback goes to `<state root>/errors.log`, since a cost optimization must never break
the agent's session. Unknown events and calls without a session id also answer `{}`.
"""
import hashlib
import itertools
import json
import os
import re
import time
import traceback
from .. import loopguard
from . import denylist, fold, repomap, repos, sidecall
from .hosts import merge_outputs
from .paths import ensure_launcher
from .store import Store
# Seconds a hook waits for its session's lock. Each stays short of the shortest timeout any agent
# gives that hook (`plugin/hooks/*.json`), which a test pins: Codex allows SessionEnd 3 s.
WAITS = {"SessionStart": 10.0, "UserPromptSubmit": 5.0, "PostToolUse": 3.0,
         "PostToolUseFailure": 3.0, "Stop": 5.0, "SessionEnd": 1.0}
QUEUED = frozenset({"UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"})
DRAIN_MAX_EVENTS = 200             # per drain, so one hook never replays without end
DRAIN_BUDGET_S = 8.0
IDLE_S = 2 * 86400                 # a lock file or empty queue this old is removed
TURN_HISTORY = 200

_SEQ = itertools.count()


def _safe(text):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(text or "unknown"))[:200] or "unknown"


def _queues(repomap_setting, event):
    """Is this event's answer always `{}`, so that the hook can leave it for another to apply?"""
    if event == "UserPromptSubmit" and repomap_setting == "on":
        return False
    return event in QUEUED


def enqueue(queue, payload):
    """Save one hook's payload to a session's queue; whether it was saved. Names sort by time; the
    pid and a sequence keep two saves in one clock tick apart (Windows' clock can repeat)."""
    try:
        os.makedirs(queue, exist_ok=True)
        name = f"{time.time_ns():020d}-{os.getpid():08d}-{next(_SEQ):06d}"
        tmp = os.path.join(queue, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, os.path.join(queue, name + ".json"))
        return True
    except (OSError, TypeError, ValueError):
        return False


def drain(call, queue):
    """Apply a session's queued events, oldest first, with the caller holding the session's lock.
    Each is removed before it is applied, so a crash loses it rather than applies it twice."""
    done, deadline = 0, time.time() + DRAIN_BUDGET_S
    while done < DRAIN_MAX_EVENTS and time.time() < deadline:
        try:
            names = sorted(n for n in os.listdir(queue) if n.endswith(".json"))
        except OSError:
            return
        if not names:
            return
        for name in names:
            path = os.path.join(queue, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    payload = json.load(fh)
            except (OSError, ValueError):
                payload = None
            try:
                os.remove(path)
            except OSError:
                continue
            if isinstance(payload, dict):
                _replay(call, payload)
                done += 1
            if done >= DRAIN_MAX_EVENTS or time.time() >= deadline:
                return


def _replay(call, payload):
    """Run a queued event's body; its answer goes nowhere, since the hook that queued it has
    answered. An error here is the queued hook's, so it must not fail the hook replaying it."""
    try:
        other = _Call(call.ctx, payload, call.now, replaying=True)
        handler = other.handler(payload.get("hook_event_name") or "")
        if handler and other.sid == call.sid:
            handler()
    except Exception:
        call._record_error()


def _prune(base, now):
    """Remove the lock files and empty queues of long-idle sessions, at most once a day. A queue
    still holding events stays: the session's next hook applies them."""
    marker = os.path.join(base, ".pruned")
    try:
        if now - os.path.getmtime(marker) < 86400:
            return
    except OSError:
        pass
    try:
        for name in os.listdir(base):
            path = os.path.join(base, name)
            try:
                if now - os.path.getmtime(path) <= IDLE_S:
                    continue
                if name.endswith(".lock"):
                    os.remove(path)
                elif name.endswith(".queue") and not os.listdir(path):
                    os.rmdir(path)
            except OSError:
                pass
        open(marker, "w", encoding="utf-8").close()
    except OSError:
        pass


def _record_error(ctx):
    """A traceback in `errors.log` for a step that failed open without failing the hook."""
    try:
        os.makedirs(ctx.root, exist_ok=True)
        with open(os.path.join(ctx.root, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()}\n")
            fh.write(traceback.format_exc())
    except Exception:
        pass


class _Call:
    """One hook call while it applies: the session's documents, its turn, and whether its answer
    reaches anyone."""
    def __init__(self, ctx, p, now, replaying=False):
        self.ctx = ctx
        self.p = p
        self.now = now
        self.host = ctx.host
        self.store = ctx.store
        self.cfg = ctx.config
        self.sid = self.host.session_id(p)
        self.replaying = replaying      # a queued event applied later: no one reads its answer

    # ---- plumbing -----------------------------------------------------------------------
    def _sess_name(self):
        return os.path.join("sessions", f"{self.sid}.json")

    def _session(self):
        s = self.store.read(self._sess_name())
        if not isinstance(s, dict):
            # Where the session started (its first hook's directory): what `repos` resolves
            # to tell which repository the session belongs to, when its log does not say.
            s = {"session_id": self.sid, "host": self.host.name, "started": self.now,
                 "cwd": self.host.cwd(self.p, self.ctx.env), "turns": [], "nudges": 0,
                 "aborts": 0, "judge_tokens": 0.0, "writer_tokens": 0.0}
        return s

    def _save(self, s):
        tp = self.host.transcript(self.p)
        if tp:
            s["transcript"] = tp
        s["updated"] = self.now
        self.store.write(self._sess_name(), s)

    def _cmd(self, name):
        """How a person runs one of the plugin's commands: omp's extension registers `/te`."""
        return f"/te {name}"

    def _log(self, kind, **fields):
        self.store.append("events.jsonl", dict(
            fields, kind=kind, at=self.now, session_id=self.sid, shadow=self.cfg.shadow))

    def _record_error(self):
        _record_error(self.ctx)

    def handler(self, event):
        if event == "SessionStart":
            return self.session_start
        return {"UserPromptSubmit": self.user_prompt, "PostToolUse": self.post_tool,
                "Stop": self.stop, "SessionEnd": self.session_end}.get(event)

    # ---- SessionStart -----------------------------------------------------------------
    def session_start(self):
        return self.inject_session_start()

    def inject_session_start(self):
        """The SessionStart outputs for one call; the session document, saved once here."""
        source = self.p.get("source") or "startup"
        s = self._session()
        s["source"] = source
        s.setdefault("cwd", self.host.cwd(self.p, self.ctx.env))
        outputs = []
        seen = self.store.read("notices.json") or {}
        seen_before = dict(seen)

        if self.cfg.microskills != "off":
            inj = self.store.read("injection.json") or {}
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
                self._log("inject", chars=len(text), facts=n_facts, source=source)
                if not self.cfg.shadow:
                    outputs.append(self.host.context("SessionStart", text))
                    s["injected_chars"] = len(text)
                    s["injected_text"] = text
                    if "rendered_ids" in inj:     # which facts, for this session's fold
                        s["injected_ids"] = list(inj["rendered_ids"])
                    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
                    if seen.get("injection") != digest:
                        outputs.append(self.host.notice(
                            f"TokenEconomizer: added {n_facts} learned fact(s) about "
                            f"{self.ctx.repo.label} to this session's context "
                            f"(review with `{self._cmd('microskills')}`)."))
                        seen["injection"] = digest

        if self.cfg.toolprune != "off" and not self.cfg.shadow:
            outputs.extend(self._toolprune_notices(seen))

        pause = sidecall.paused(self.host.name, self.ctx.root)
        calls_a_model = "distiller" in self.cfg.microskills or self.cfg.loopguard != "off"
        if (pause and calls_a_model and not self.cfg.shadow
                and seen.get("sidecall_paused") != pause["at"]):
            outputs.append(self.host.notice(
                f"TokenEconomizer: the plugin's own model calls go through "
                f"`{sidecall.call_name(self.host.name)}`, "
                f"which answered that it is not signed in, so they are paused; facts that "
                f"need no model call are still learned, and the loop guard redirects only at its "
                f"backstop. `{self._cmd('config')} set microskills=on` (or `loopguard=on`) tries "
                f"again."))
            seen["sidecall_paused"] = pause["at"]

        note = repos.migration_notice(self.ctx.root, self._cmd("repos"))
        if note and not self.cfg.shadow:
            outputs.append(self.host.notice(note))

        if self.cfg.repomap != "off":
            if repomap.stale(self.ctx):
                repomap.spawn_refresh(self.ctx)
            at_start = self.cfg.repomap == "start"
            if at_start and source in ("startup", "new", "clear", "compact"):
                outputs.extend(self._inject_map(s, seen, repomap.cached_block(self.ctx),
                                               "SessionStart", "cached"))

        if seen != seen_before:
            self.store.write("notices.json", seen)
        self._save(s)
        repos.note_repo(self.ctx.root, self.ctx.repo)
        repos.prune_pins(self.ctx.root, self.host.name)
        repos.start_migration(self.ctx)
        fold.spawn_fold(self.ctx, fold.pending_sessions(self.ctx, exclude=(self.sid,)))
        return merge_outputs(*outputs)

    def _toolprune_notices(self, seen):
        """The deny list's lines at session start, each said once: what `auto` removed from this
        checkout's sessions (from this one on), and a proposal still waiting -- under `propose`,
        or when `auto` could not write the settings file."""
        out = []
        cmd, unit = self._cmd("toolprune"), "tool"
        unt = self.store.read("untool.json") or {}
        mine = sorted(denylist.applied_for(self.store))

        def few(items):
            return ", ".join(items[:4]) + ("..." if len(items) > 4 else "")
        if self.cfg.toolprune == "auto" and seen.get("untool_applied", "") != ",".join(mine):
            if mine:
                out.append(self.host.notice(
                    f"TokenEconomizer removed {len(mine)} {unit}(s) this repository's sessions "
                    f"never used ({few(mine)}), from this session on. Review with `{cmd}`; "
                    f"`{cmd} restore` gives them back."))
            seen["untool_applied"] = ",".join(mine)
        # What this checkout already applied, or gave back, is no news here (another clone may
        # not have).
        kept = set(denylist.kept_for(self.store))
        prop = [t for t in unt.get("proposal") or [] if t not in mine and t not in kept]
        refused = unt.get("auto_apply_refused") if self.cfg.toolprune == "auto" else None
        # Under `auto`, a list waiting for the repository to go quiet is no news yet: the
        # session start after it is written says so (above). A refusal is.
        if self.cfg.toolprune == "auto" and not refused:
            held = set(((unt.get("auto_pending") or {}).get("items") or []))
            prop = [t for t in prop if t not in held]
        if prop and seen.get("untool") != ",".join(prop):
            out.append(self.host.notice(
                f"TokenEconomizer: {len(prop)} {unit}(s) went unused in this repository's recent "
                f"sessions ({few(prop)})"
                + f". Review with `{cmd}`; "
                + (f"they could not be applied ({refused}), and `{cmd} apply` tries again."
                   if refused else f"nothing changes until you run `{cmd} apply`.")
            ))
            seen["untool"] = ",".join(prop)
        return out

    def _inject_map(self, s, seen, block, event, how):
        """The outputs that put the repository map in context (and say so, once per repository)."""
        self._log("repomap", chars=len(block), how=how)
        if not block or self.cfg.shadow:
            return []
        s["repomap_chars"] = len(block)
        out = [self.host.context(event, block)]
        if not seen.get("repomap"):
            out.append(self.host.notice(
                f"TokenEconomizer: added a map of {self.ctx.repo.label}'s key files and symbols "
                f"to this session's context (~{len(block) // 4} tokens; `repomap=off` to stop)."))
            seen["repomap"] = True
        return out

    # ---- prompt-turns -------------------------------------------------------------------
    def _close_turn(self, s, stopped_normally):
        data = s.get("turn")
        if not data:
            return None
        g = loopguard.TurnGuard(data=data)
        if g.held:                  # reached on a queued call; the turn ended before the next one
            self._log("undelivered", action=g.held[0], reason=g.held[1])
        summary = g.summary(stopped_normally)
        if g.cum_turns and stopped_normally:
            loopguard.record_turn(self.store, g, level=self.cfg.loopguard_aggr,
                                  overrides=self.cfg.loopguard_thresholds)
        s["turns"] = (s.get("turns") or [])[-(TURN_HISTORY - 1):] + [summary]
        s["judge_tokens"] = float(s.get("judge_tokens", 0) or 0) + g.judge_tokens
        s["writer_tokens"] = float(s.get("writer_tokens", 0) or 0) + g.writer_tokens
        s["turn"] = None
        return summary

    def user_prompt(self):
        if self.cfg.loopguard == "off" and self.cfg.repomap != "on":
            return {}
        s = self._session()
        outputs = []
        if self.cfg.repomap == "on" and not s.get("repomap_done"):
            # Once per session, on its first prompt: a map ranked toward what the prompt names.
            s["repomap_done"] = True
            seen = self.store.read("notices.json") or {}
            block, how = repomap.for_prompt(self.ctx, self.p.get("prompt") or "")
            outputs = self._inject_map(s, seen, block, "UserPromptSubmit", how)
            if outputs:
                self.store.write("notices.json", seen)
        if self.cfg.loopguard != "off":
            # A turn still open here never reached Stop -- the person interrupted it. Its
            # outcome is unknown, so it is recorded but does not feed the envelope.
            self._close_turn(s, stopped_normally=False)
            s["turn"] = loopguard.TurnGuard(turn_key=self.host.turn_key(self.p)).to_dict()
        self._save(s)
        return merge_outputs(*outputs) if outputs else {}

    def stop(self):
        if self.cfg.loopguard == "off" or self.p.get("stop_hook_active"):
            return {}
        s = self._session()
        summary = self._close_turn(s, stopped_normally=True)
        if summary:
            self._log("turn", **summary)
        self._save(s)
        return {}

    # ---- PostToolUse ---------------------------------------------------------------------
    def _judge(self, thr, envelope):
        """The stuck judge, run in the background (`judge.AsyncJudge`): with the guard on, it
        makes every call to redirect but the backstop's. The hook only starts a job, and a later
        call picks up its verdict with the redirect already written, so the agent never waits.

        None before this repository has an envelope -- the guard does not act on a repository
        whose usual turns it has not learned (the rule fallback never acts without one either).
        While this agent's model calls are paused, a judge that never answers: the turn is
        spared, as a missing verdict is, rather than redirected on the rule alone."""
        from .judge import AsyncJudge, NoVerdict
        if envelope is None:
            return None
        if sidecall.paused(self.host.name, self.ctx.root):
            return NoVerdict()
        return AsyncJudge(self.ctx, self.sid, thr.get("judge_confidence"))

    def post_tool(self):
        if self.cfg.loopguard == "off":
            return {}
        event = self.p.get("hook_event_name") or "PostToolUse"
        s = self._session()
        key = self.host.turn_key(self.p)
        data = s.get("turn")
        if data and key and data.get("turn_key") and data["turn_key"] != key:
            self._close_turn(s, stopped_normally=False)     # a turn we never saw end
            data = None
        g = loopguard.TurnGuard(data=data) if data else loopguard.TurnGuard(turn_key=key)
        name, args, text, code = self.host.tool_call(self.p)
        tracker = loopguard.load_envelope(self.store, level=self.cfg.loopguard_aggr,
                                          overrides=self.cfg.loopguard_thresholds)
        envelope = tracker.envelope
        thr = loopguard.thresholds(tracker, self.cfg.loopguard_thresholds)
        # The judge is woken only by a sign of trouble -- a repeated failure, or dense errors --
        # once a turn has run `min_turns` calls; never merely because the turn outran this
        # repository's usual length, which interactive turns do all the time (the experiments'
        # agents ran under a turn cap, hence their envelope gate).
        thr = dict(thr, gate_over_envelope=False,
                   gate_min_turns=max(thr["gate_min_turns"], thr["min_turns"]))
        decision = g.fold(name, args, text, code, envelope=envelope, thr=thr,
                          judge=self._judge(thr, envelope),
                          nudge_budget=self.cfg.loopguard_nudges)
        # A queued call's hook has already answered, so a decision reached on it waits, untouched
        # and uncounted, for the turn's next call: acting on it here would spend the turn's redirect
        # on an answer no one reads.
        if self.replaying:
            g.held = g.held or decision
            decision = None
        elif g.held:
            decision, g.held = g.held, None
        out = {}
        if decision:
            action, reason = decision
            short = reason.split(":")[0]
            if action == "nudge":
                body = g.nudge_body(envelope)
                g.after_nudge()
                s["nudges"] = int(s.get("nudges", 0)) + 1
                self._log("nudge", reason=reason, tool_calls=g.cum_turns, envelope=envelope)
                if not self.cfg.shadow:
                    out = merge_outputs(
                        self.host.context(event, body),
                        self.host.notice(f"TokenEconomizer nudged this turn ({short}): the "
                                         f"same failure kept recurring."))
            elif action == "abort" and not g.abort_delivered:
                # Still stuck after the redirect. The plugin never stops a turn itself, the
                # person decides, so the last rung is a notice that says how to stop it.
                g.abort_delivered = True
                s["aborts"] = int(s.get("aborts", 0)) + 1
                self._log("abort", reason=reason, tool_calls=g.cum_turns, envelope=envelope,
                          delivered="notice")
                if not self.cfg.shadow:
                    headless = self.ctx.is_headless(self.host.transcript(self.p))
                    text = (f"TokenEconomizer: this turn still looks stuck after a redirect "
                            f"({short}, {g.cum_turns} tool calls). {self.host.stop_hint(headless)}")
                    out = self.host.notice(text)
        s["turn"] = g.to_dict()
        self._save(s)
        return out

    # ---- SessionEnd ---------------------------------------------------------------------
    def session_end(self):
        # An end with no start and no session log is no session to learn from, and recording it
        # would only leave an unreadable fold in the ledger.
        tp = self.host.transcript(self.p)
        if not os.path.exists(self.store.path(self._sess_name())) and not (
                tp and os.path.exists(tp)):
            return {}
        s = self._session()
        self._close_turn(s, stopped_normally=False)
        s["ended"] = self.now
        s["end_reason"] = self.p.get("reason")
        self._save(s)
        fold.spawn_fold(self.ctx, [self.sid])
        return {}


def handle(ctx, payload, *, now=None):
    """One hook call's answer, with the session's other hooks kept out; `{}` when it queued.

    Owns the session lock, the queue drain before and after the call, the held decision, the
    turn close, and the save: exactly one save per applied event, each queued payload applied
    exactly once, each held decision delivered at most once. Never raises.
    """
    try:
        return _handle(ctx, payload or {}, time.time() if now is None else now)
    except Exception:
        _record_error(ctx)
        return {}


def _handle(ctx, p, now):
    # A payload without a session id is not a hook call any host makes; recording it would
    # leave an "unknown" session behind.
    if not ctx.config.enabled or not ctx.host.session_id(p):
        return {}
    ctx.settle()
    call = _Call(ctx, p, now)
    event = p.get("hook_event_name") or ""
    if event in ("SessionStart", "UserPromptSubmit"):
        try:
            ensure_launcher(ctx.env)
        except OSError:
            call._record_error()
    handler = call.handler(event)
    if handler is None:
        return {}
    base = os.path.join(ctx.root, "locks", _safe(ctx.host.name))
    name = _safe(call.sid)
    queue = os.path.join(base, name + ".queue")
    with Store(base).locked(name, timeout=WAITS.get(event, 3.0)) as got:
        if got:
            # Taking a lock writes nothing to its file, so its age would be the session's, and
            # `_prune` would remove the lock of a session older than IDLE_S while a hook holds
            # it: on POSIX the session's next hook would lock a new file and run beside it.
            try:
                os.utime(os.path.join(base, f".{name}.lock"))
            except OSError:
                pass
        _prune(base, now)
        if got:
            drain(call, queue)
            try:
                return handler() or {}
            finally:
                drain(call, queue)
    if _queues(call.cfg.repomap, event) and enqueue(queue, call.p):
        return {}
    return handler() or {}
