"""One session's hooks run one at a time (`plugin/turnsession.py`).

The first test is the report in #142: real hook processes started together, against a throwaway
state root where no agent CLI is reachable, so nothing calls a model. The rest drive the turn seam
(`turnsession.handle`) with a scripted lock and a stepped clock: a lock miss queues and answers
`{}`, the next live call applies what queued exactly once, a redirect reached on a queued call
goes out with the next live one, and a turn that ends first drops it with an `undelivered` log.
"""
import contextlib
import glob
import json
import os
import subprocess
import sys

import pytest

from tokeneconomizer.plugin import turnsession
from tokeneconomizer.plugin.store import Store

from test_live_hooks import (GREEN, _first, _post, _turn, call, env, repo,  # noqa: F401
                             store_for, stuck_calls)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ = ("Read", {"file_path": "/r/a.py"}, "ok")
NOW = 1700000000.0


def base(repo, sid="s", key="k"):
    return {"session_id": sid, "transcript_path": "", "cwd": repo, "prompt_id": key}


def tcall(env, repo, payload, now):
    """One hook call through the turn seam with an explicit clock (what `hook.main` runs)."""
    from tokeneconomizer.plugin.context import LiveContext
    from tokeneconomizer.plugin.hosts import Omp
    host = Omp()
    p = host.prepare(dict(payload), env)
    ctx = LiveContext.build(host, host.cwd(p, env), env,
                            session_id=host.session_id(p) or None, settle=False)
    if not ctx.config.enabled:
        return {}
    return host.render(p, turnsession.handle(ctx, p, now=now), ctx)


def start_turn(env, repo, sid="s", key="k", now=NOW):
    return tcall(env, repo, dict(base(repo, sid, key), hook_event_name="UserPromptSubmit",
                                prompt="go"), now)


def stop_turn(env, repo, sid="s", key="k", now=NOW):
    return tcall(env, repo, dict(base(repo, sid, key), hook_event_name="Stop",
                                stop_hook_active=False), now)


def locks(env):
    return os.path.join(env["TOKENECONOMIZER_HOME"], "locks", "omp")


def queue_of(env, sid="s"):
    return os.path.join(locks(env), f"{sid}.queue")


def waiting(env, sid="s"):
    return len(glob.glob(os.path.join(queue_of(env, sid), "*.json")))


@pytest.fixture
def grants(monkeypatch):
    """The session lock's scripted answers: each hook call takes the next, else takes it."""
    script = []

    class FakeStore(Store):
        @contextlib.contextmanager
        def locked(self, name="state", timeout=10.0):
            yield script.pop(0) if script else True

    monkeypatch.setattr(turnsession, "Store", FakeStore)
    return script


def doc(env, repo, sid="s"):
    return store_for(env, repo).read(os.path.join("sessions", f"{sid}.json"))


def events(env, repo, kind, sid="s"):
    return [e for e in store_for(env, repo).read_lines("events.jsonl")
            if e.get("kind") == kind and e.get("session_id") == sid]


# ---- the report -----------------------------------------------------------------------------
def test_tool_calls_made_in_parallel_are_all_recorded(tmp_path):
    home, state, cwd = tmp_path / "home", tmp_path / "state", tmp_path / "work"
    for d in (home, state, cwd):
        d.mkdir()
    env = dict(os.environ, TOKENECONOMIZER_HOME=str(state), HOME=str(home), USERPROFILE=str(home),
               PYTHONUTF8="1", PYTHONPATH=REPO)
    env.pop("TE_SIDECALL", None)
    hook = [sys.executable, os.path.join(REPO, "plugin", "scripts", "te_bootstrap.py"), "hook"]

    def payload(i):
        return json.dumps({"hook_event_name": "PostToolUse", "session_id": "race", "cwd": str(cwd),
                           "transcript_path": str(cwd / "t.jsonl"), "tool_name": "Read",
                           "tool_input": {"file_path": str(cwd / f"f{i}")},
                           "tool_response": {"type": "text"}})

    subprocess.run(hook, input=payload(0), text=True, capture_output=True, env=env, cwd=str(cwd),
                   timeout=60)
    hooks = [subprocess.Popen(hook, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env, cwd=str(cwd))
             for _ in range(8)]
    for i, p in enumerate(hooks, 1):
        p.stdin.write(payload(i))
        p.stdin.close()
    for p in hooks:
        p.wait(timeout=60)
        assert p.returncode == 0 and p.stdout.read().strip() == "{}", p.stderr.read()
    for p in hooks:
        p.stdout.close()
        p.stderr.close()
    docs = glob.glob(str(state / "repos" / "*" / "*" / "sessions" / "race.json"))
    assert len(docs) == 1
    with open(docs[0], encoding="utf-8") as fh:
        assert json.load(fh)["turn"]["cum_turns"] == 9
    assert not (state / "errors.log").exists()


# ---- queueing -------------------------------------------------------------------------------
def test_a_hook_without_the_lock_queues_and_the_next_one_applies_it(env, repo, grants):
    start_turn(env, repo, now=NOW)
    grants.append(False)
    assert tcall(env, repo, _post(base(repo), *READ), NOW + 1) == {}
    assert waiting(env) == 1 and doc(env, repo)["turn"]["cum_turns"] == 0
    tcall(env, repo, _post(base(repo), *READ), NOW + 2)
    assert waiting(env) == 0 and doc(env, repo)["turn"]["cum_turns"] == 2
    assert doc(env, repo)["updated"] == NOW + 2


def test_queued_events_apply_oldest_first(env, repo, grants):
    prompt = dict(base(repo), hook_event_name="UserPromptSubmit", prompt="go")
    grants.extend([False, False])
    assert tcall(env, repo, prompt, NOW + 1) == {}
    assert tcall(env, repo, _post(base(repo), *READ), NOW + 2) == {}
    tcall(env, repo, _post(base(repo), *READ), NOW + 3)
    turn = doc(env, repo)["turn"]
    assert turn["turn_key"] == "k" and turn["cum_turns"] == 2


def test_session_start_answers_itself(env, repo, grants):
    start = dict(base(repo), hook_event_name="SessionStart", source="startup")
    grants.append(False)
    tcall(env, repo, start, NOW)
    assert waiting(env) == 0 and doc(env, repo)["source"] == "startup"


def test_an_unreadable_queue_entry_is_dropped(env, repo):
    start_turn(env, repo, now=NOW)
    os.makedirs(queue_of(env))
    with open(os.path.join(queue_of(env), "0.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    tcall(env, repo, _post(base(repo), *READ), NOW + 1)
    assert waiting(env) == 0 and doc(env, repo)["turn"]["cum_turns"] == 1


def test_a_call_without_a_session_and_an_unknown_event_answer_nothing(env, repo):
    assert tcall(env, repo, {"hook_event_name": "Stop", "cwd": repo}, NOW) == {}
    assert tcall(env, repo, dict(base(repo), hook_event_name="Nope"), NOW) == {}
    assert not os.path.exists(store_for(env, repo).base)


def test_an_internal_error_answers_nothing_and_is_logged(env, repo, monkeypatch):
    start_turn(env, repo, now=NOW)

    class BrokenStore(Store):
        @contextlib.contextmanager
        def locked(self, name="state", timeout=10.0):
            raise RuntimeError("broken lock")

    monkeypatch.setattr(turnsession, "Store", BrokenStore)
    assert tcall(env, repo, _post(base(repo), *READ), NOW + 1) == {}
    with open(os.path.join(env["TOKENECONOMIZER_HOME"], "errors.log"), encoding="utf-8") as fh:
        assert "RuntimeError: broken lock" in fh.read()


# ---- what a queued hook would have said -----------------------------------------------------
def stuck_turn(env, repo):
    """A repository the guard has learned, the calls of a stuck turn, and at which of them the
    redirect and the stop notice come when every hook answers."""
    for k in range(5):
        _turn(env, repo, "warm", f"g{k}", GREEN)
    calls = stuck_calls(40)
    outs = _turn(env, repo, "probe", "p", calls)
    nudge = _first(outs, lambda o: "hookSpecificOutput" in o)
    notice = nudge + 1 + _first(outs[nudge + 1:], lambda o: o != {})
    return calls, nudge, notice


def run_until(env, repo, calls, end):
    start_turn(env, repo, now=NOW)
    return [tcall(env, repo, _post(base(repo), *c), NOW + 1 + i) for i, c in enumerate(calls[:end])]


@pytest.mark.parametrize("ahead", [1, 2], ids=["next call", "the call after"])
def test_a_redirect_reached_on_a_queued_call_goes_out_with_the_next_one(env, repo, grants, ahead):
    """`stuck_calls` alternates success and failure, so the two cases are the two events the
    answer can be labelled with; a failed call's answer labelled PostToolUse would be dropped."""
    calls, nudge, _ = stuck_turn(env, repo)
    assert all(o == {} for o in run_until(env, repo, calls, nudge))
    grants.append(False)
    assert tcall(env, repo, _post(base(repo), *calls[nudge]), NOW + 100) == {}
    assert not events(env, repo, "nudge")                     # queued: nothing decided or spent
    payload = _post(base(repo), *calls[nudge + ahead])
    out = tcall(env, repo, payload, NOW + 101)
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == payload["hook_event_name"]
    assert "Process check (automated)" in specific["additionalContext"]
    assert "nudged" in out["systemMessage"]
    assert len(events(env, repo, "nudge")) == 1 and doc(env, repo)["nudges"] == 1


def test_a_redirect_held_for_the_next_call_is_said_once(env, repo, grants):
    calls, nudge, _ = stuck_turn(env, repo)
    run_until(env, repo, calls, nudge)
    grants.extend([False, False])
    tcall(env, repo, _post(base(repo), *calls[nudge]), NOW + 100)
    tcall(env, repo, _post(base(repo), *calls[nudge + 1]), NOW + 101)     # a second one behind it
    assert waiting(env) == 2
    outs = [tcall(env, repo, _post(base(repo), *c), NOW + 102 + i)
            for i, c in enumerate(calls[nudge + 2:nudge + 4])]
    assert "hookSpecificOutput" in outs[0] and outs[1] == {}
    assert len(events(env, repo, "nudge")) == 1


def test_a_turn_that_ends_first_drops_the_redirect_uncounted(env, repo, grants):
    calls, nudge, _ = stuck_turn(env, repo)
    run_until(env, repo, calls, nudge)
    grants.append(False)
    tcall(env, repo, _post(base(repo), *calls[nudge]), NOW + 100)
    stop_turn(env, repo, now=NOW + 101)
    session = doc(env, repo)
    assert session["nudges"] == 0 and session["turns"][-1]["nudges"] == 0
    assert not events(env, repo, "nudge")
    assert [e["action"] for e in events(env, repo, "undelivered")] == ["nudge"]
    start_turn(env, repo, key="k2", now=NOW + 102)                           # nothing leaks on
    assert tcall(env, repo, _post(base(repo, key="k2"), *READ), NOW + 103) == {}


def test_the_stop_notice_reached_on_a_queued_call_goes_out_too(env, repo, grants):
    calls, _, notice = stuck_turn(env, repo)
    run_until(env, repo, calls, notice)
    grants.append(False)
    assert tcall(env, repo, _post(base(repo), *calls[notice]), NOW + 100) == {}
    out = tcall(env, repo, _post(base(repo), *calls[notice + 1]), NOW + 101)
    assert "still looks stuck" in out["systemMessage"]
    assert doc(env, repo)["aborts"] == 1
    assert tcall(env, repo, _post(base(repo), *calls[notice + 2]), NOW + 102) == {}
