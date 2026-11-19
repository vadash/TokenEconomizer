"""The plugin's hooks, end to end, through `live.hook.main` with synthetic payloads.

Each test drives a whole stretch of a repository's life -- sessions starting, prompt-turns, tool
calls, sessions ending and being folded -- and pins one property the live layer must have:
learned facts reach the next session's context, the loop guard is silent until it has learned
the repository and then redirects before it tells the person, a deny list is written only once
the repository's sessions are idle and touches only its own deny rules, every session is folded
exactly once, and nothing here can break the agent's session.

Folds run inline (`TE_FOLD_SYNC=1`); in the plugin they are detached processes.
"""
import glob
import io
import json
import os
import time

import pytest

from tokeneconomizer.plugin import denylist, fold, hook, hosts
from tokeneconomizer.plugin import turnsession
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
from tokeneconomizer.plugin.store import Store



@pytest.fixture
def env(tmp_path):
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "home"), "TE_FOLD_SYNC": "1",
            # A deny list is written at the fold here; the wait before it has its own tests.
            "TE_TOOLPRUNE_QUIET_MINUTES": "0",
            # Facts are learned here without a model call. Micro-skills is off by default, and
            # its `on` adds the proposer; both have their own tests.
            "TE_MICROSKILLS": "envfacts",
            "GIT_CEILING_DIRECTORIES": str(tmp_path), "PATH": os.environ.get("PATH", "")}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".git" / "info").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (r / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n')
    (r / "pyproject.toml").write_text("[project]\nname='w'\n")
    return str(r)


def call(env, payload, host=None):
    """One hook call: its final JSON object."""
    out = io.StringIO()
    hook.main(["--host", host] if host else [], io.StringIO(json.dumps(payload)), out, env)
    lines = out.getvalue().splitlines()
    res = json.loads(lines[-1])
    progress = [json.loads(ln) for ln in lines[:-1]]
    assert all(p["type"] == "progress" for p in progress)
    if progress:
        res["progress"] = [p["message"] for p in progress]
    return res


# A quiet SessionStart answers only the extension's lever: no manual, no notice, nothing
# removed yet. The dispatcher always names the tools to remove, even when there are none.
BARE_START = {"omp": {"denyTools": []}}


def store_for(env, repo, host="omp"):
    return Store(repo_state_dir(repo_identity(repo, env), host, env))


def write_session(path, prompts, model="gpt-5.6-sol"):
    """An omp session file: a header, then per prompt a user message, and per tool call an
    assistant message (toolCall, with usage) and its toolResult."""
    lines = [{"type": "session", "version": 3, "id": os.path.basename(path),
              "timestamp": "2026-09-30T00:00:00Z"}]
    parent, n = None, 0

    def add(message):
        nonlocal parent, n
        n += 1
        eid = f"e{n}"
        lines.append({"type": "message", "id": eid, "parentId": parent,
                      "timestamp": "2026-09-30T00:00:01Z", "message": message})
        parent = eid

    for text, calls in prompts:
        add({"role": "user", "content": text})
        for name, inp, result, is_error in calls:
            n += 1
            tid = f"tu{n}"
            add({"role": "assistant", "model": model, "usage": {
                "input": 10, "cacheRead": 1000, "cacheWrite": 100, "output": 50,
                "cost": {"total": 0.002}},
                "content": [{"type": "toolCall", "id": tid, "name": name, "arguments": inp}]})
            add({"role": "toolResult", "toolCallId": tid, "toolName": name,
                 "isError": is_error, "content": [{"type": "text", "text": result}]})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def trap_session(path, repo, i):
    """An environment trap every session falls into: bare `pytest` is missing here."""
    return write_session(path, [(f"fix bug {i}", [
        ("edit", {"path": f"{repo}/pkg/mod{i}.py"}, "ok", False),
        ("bash", {"command": "pytest -q tests/test_a.py"}, "bash: pytest: command not found", True),
        ("bash", {"command": "python -m pytest -q tests/test_a.py"}, "1 passed in 0.1s", False),
    ])])


def run_session(env, repo, sid, transcript, host=None, source="startup"):
    base = {"session_id": sid, "transcript_path": transcript, "cwd": repo}
    start = call(env, dict(base, hook_event_name="SessionStart", source=source), host)
    call(env, dict(base, hook_event_name="SessionEnd", reason="other"), host)
    return start


# ---- injection ---------------------------------------------------------------------------
def prevented_session(path, repo, i):
    """The agent was told: it runs the working form at once and never meets the trap."""
    return write_session(path, [(f"fix bug {i}", [
        ("edit", {"path": f"{repo}/pkg/mod{i}.py"}, "ok", False),
        ("bash", {"command": "python -m pytest -q tests/test_a.py"}, "1 passed in 0.1s", False),
    ])])


def test_an_injected_fact_that_prevents_its_failure_stays_injected(env, repo, tmp_path):
    """The session-start hook injects; a fold in another process learns afterwards. The fold is
    told which facts the session was given, so a fact that prevented its failure counts as having
    fired, and stays in the block."""
    for i in range(5):
        run_session(env, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
    starts = [run_session(env, repo, f"p{i}", prevented_session(str(tmp_path / f"p{i}.jsonl"),
                                                                repo, i + 5))
              for i in range(8)]
    claim = "`pytest …` fails here with command not found; `python -m pytest …` works."
    assert all(claim in s["hookSpecificOutput"]["additionalContext"] for s in starts)
    (path,) = glob.glob(store_for(env, repo).path("mechanisms", "envfacts_state", "*.json"))
    life = json.load(open(path))["facts"]["form_repair:run_tests:pytest"]["life"]
    assert life["fire_num"] == life["fire_den"]          # every session it was given counted
    assert store_for(env, repo).read("sessions/p7.json")["injected_ids"] == [
        "form_repair:run_tests:pytest"]


def test_a_learned_fact_reaches_the_next_session_and_is_announced_once(env, repo, tmp_path):
    starts = [run_session(env, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
              for i in range(6)]
    assert all(s == BARE_START for s in starts[:4])  # the fact must clear its firing gate first
    ctx = starts[4]["hookSpecificOutput"]["additionalContext"]
    assert "`pytest …` fails here with command not found; `python -m pytest …` works." in ctx
    assert "learned fact" in starts[4]["systemMessage"]
    assert "systemMessage" not in starts[5]           # unchanged manual: no second notice
    assert starts[5]["hookSpecificOutput"]["additionalContext"] == ctx


def test_micro_skills_is_off_by_default_and_learns_nothing(env, repo, tmp_path, monkeypatch,
                                                           capsys):
    """The sessions that teach the fact above leave nothing behind unless it is turned on."""
    from tokeneconomizer.plugin import cli
    default = {k: v for k, v in env.items() if k != "TE_MICROSKILLS"}
    starts = [run_session(default, repo, f"s{i}",
                          trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
              for i in range(6)]
    assert all(s == BARE_START for s in starts)
    assert store_for(default, repo).read("injection.json") is None
    monkeypatch.delenv("TE_MICROSKILLS", raising=False)
    for k, v in default.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["--host", "omp", "--cwd", repo, "microskills"]) == 0
    assert "`config set microskills=on` turns it on" in capsys.readouterr().out


def test_micro_skills_on_runs_the_proposer_without_paid_and_charges_it(
        env, repo, tmp_path, monkeypatch):
    """`on` is the experiments' micro-skills: the free facts plus the proposer, here every
    third session (after the 2nd, 5th, ...), as a session carries more work than a benchmark
    task. Turning it on is what allows the proposer's model call; `paid` is the loop guard's."""
    from tokeneconomizer.plugin import sidecall
    models = []

    def fake_call(prompt, model="", cli="omp", **kw):
        models.append(model)
        return "{}", 120.0
    monkeypatch.setattr(sidecall, "omp_call", fake_call)
    on = dict(env, TE_MICROSKILLS="on")
    for i in range(5):
        run_session(on, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
    ledger = store_for(on, repo).read_lines("ledger.jsonl")
    assert models == ["", ""]
    assert [r["overhead"]["optimizer_tokens"] for r in ledger] == [0, 120.0, 0, 0, 120.0]
    assert store_for(on, repo).read("injection.json")["profile"] == "envfacts+distiller"


def multi_prompt_trap_session(path, repo, i, prompts=4, every=False):
    """A session of `prompts` prompts that meets the missing `pytest` at its first test run (or,
    with `every`, at each): later prompts use the working form, as the conversation shows it."""
    def prompt(k):
        edit = ("edit", {"path": f"{repo}/pkg/m{i}_{k}.py"}, "ok", False)
        known = ("bash", {"command": "python -m pytest -q tests/test_a.py"}, "1 passed in 0.1s",
                 False)
        trap = ("bash", {"command": "pytest -q tests/test_a.py"},
                "bash: pytest: command not found", True)
        return (f"fix bug {i}.{k}", [edit, trap, known] if every or k == 0 else [edit, known])
    return write_session(path, [prompt(k) for k in range(prompts)])


def test_facts_learn_per_session_however_many_prompts_it_ran(env, repo, tmp_path):
    """Unlike tool pruning, the facts count sessions, not prompts. Within a session the agent
    already knows the fix from its own earlier failure, so its later prompts would count as
    exercising the fact without the failure: per prompt, a trap met once a session fires in a
    quarter of them and never clears the firing gate. Four-prompt sessions reach the fact at the
    fifth start, as one-prompt sessions do (above)."""
    claim = "`pytest …` fails here with command not found; `python -m pytest …` works."
    starts = [run_session(env, repo, f"s{i}",
                          multi_prompt_trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
              for i in range(6)]
    assert all(s == BARE_START for s in starts[:4])
    assert claim in starts[4]["hookSpecificOutput"]["additionalContext"]


def test_resume_resends_what_the_session_started_with_and_shadow_never_injects(
        env, repo, tmp_path):
    """omp keeps only a digest of its prompt, so a resumed conversation no longer carries what
    its first start added: the session's own manual is sent again, as it was (which also keeps
    the prompt cache). Shadow computes and logs it."""
    for i in range(5):
        run_session(env, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
    t = str(tmp_path / "later.jsonl")
    open(t, "w").close()
    resumed = call(env, {"hook_event_name": "SessionStart", "session_id": "s4",
                         "source": "resume", "transcript_path": t, "cwd": repo})
    assert "learned fact" not in resumed.get("systemMessage", "")  # unchanged: no new notice
    assert "`python -m pytest" in resumed["hookSpecificOutput"]["additionalContext"]
    shadow = dict(env, TE_SHADOW="1")
    assert call(shadow, {"hook_event_name": "SessionStart", "session_id": "z", "source": "startup",
                         "transcript_path": t, "cwd": repo}) == {}
    events = store_for(env, repo).read_lines("events.jsonl")
    assert any(e["kind"] == "inject" and e["shadow"] for e in events)


def test_a_resumed_session_gets_back_the_facts_it_started_with(env, repo, tmp_path):
    """Resume re-sends what the session started with, not the manual as it is now; a session
    that started without facts gets none."""
    st = store_for(env, repo)
    st.write("injection.json", {"rendered": "Facts:\n- one\n- two", "rendered_ids": ["a", "b"]})

    def start(sid, source):
        return json.dumps(call(env, {"hook_event_name": "SessionStart", "session_id": sid,
                                     "source": source, "cwd": repo}, "omp"))
    assert "- two" in start("c1", "new")
    st.write("injection.json", {"rendered": "Facts:\n- one", "rendered_ids": ["a"]})
    assert "- two" in start("c1", "resume")                     # as it started, not as now
    st.write("injection.json", {"rendered": "", "rendered_ids": []})
    start("c4", "new")
    st.write("injection.json", {"rendered": "Facts:\n- one", "rendered_ids": ["a"]})
    assert "- one" not in start("c4", "resume")                 # started without facts


# ---- the spiral guard --------------------------------------------------------------------
def _post(base, name, args, result):
    return dict(base, hook_event_name="PostToolUse", tool_name=name, tool_input=args,
                tool_response=result)


def _turn(env, repo, sid, key, calls, host="omp"):
    """One prompt-turn through UserPromptSubmit -> PostToolUse* -> Stop; outputs per call."""
    base = {"session_id": sid, "transcript_path": "", "cwd": repo, "prompt_id": key}
    call(env, dict(base, hook_event_name="UserPromptSubmit", prompt="go"), host)
    outs = [call(env, _post(base, n, i, r), host) for n, i, r in calls]
    call(env, dict(base, hook_event_name="Stop", stop_hook_active=False), host)
    return outs


GREEN = [("read", {"path": "/r/a.py"}, {"content": "ok"})] * 8


def stuck_calls(n, host=None):
    fail = "FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2"
    return [("edit", {"path": "/r/pkg/a.py"}, {"content": "ok"}) if k % 2 == 0 else
            ("bash", {"command": "pytest -q"},
             {"content": fail, "isError": True, "exit_code": 1})
            for k in range(n)]


def _first(outs, pred):
    return next((i for i, o in enumerate(outs) if pred(o)), None)


def test_cold_start_never_intervenes(env, repo):
    outs = _turn(env, repo, "s", "p0", stuck_calls(40))
    assert all(o == {} for o in outs)                 # no envelope learned: resolve-safe


def test_warm_guard_nudges_then_only_notifies_in_an_interactive_session(env, repo):
    for k in range(5):
        _turn(env, repo, "s", f"g{k}", GREEN)          # five ended-green turns of 8 calls
    outs = _turn(env, repo, "s", "stuck", stuck_calls(40))
    nudge = _first(outs, lambda o: "hookSpecificOutput" in o)
    assert nudge is not None
    assert "Process check (automated)" in outs[nudge]["hookSpecificOutput"]["additionalContext"]
    assert "nudged" in outs[nudge]["systemMessage"]
    later = _first(outs[nudge + 1:], lambda o: o != {})
    assert later is not None
    after = outs[nudge + 1 + later]
    assert "continue" not in after and "still looks stuck" in after["systemMessage"]
    assert all(o == {} for o in outs[nudge + 2 + later:])   # the notice is delivered once


def _guard_turn(env, repo, sid, key, calls, host="omp"):
    """One prompt-turn: each call in omp's payload shape."""
    base = {"session_id": sid, "transcript_path": "", "cwd": repo, "prompt_id": key}
    call(env, dict(base, hook_event_name="UserPromptSubmit", prompt="go"), host)
    outs = [call(env, _post(base, n, i, r), host) for n, i, r in calls]
    call(env, dict(base, hook_event_name="Stop", stop_hook_active=False), host)
    return outs


def _stuck(host="omp", n=40):
    return stuck_calls(n)


@pytest.mark.parametrize("mode", ["interactive", "headless"])
@pytest.mark.parametrize("setting", ["nudge", "stop"])
def test_the_guard_redirects_then_tells_the_person_how_to_stop(env, repo, mode, setting):
    """One ladder: a redirect to the model, then one notice to the person with omp's way to
    stop the turn. The plugin never stops it -- `stop` is read as `nudge`."""
    env = dict(env, TE_MODE=mode, TE_LOOPGUARD=setting)
    for k in range(5):
        _guard_turn(env, repo, "s", f"g{k}", GREEN)
    outs = _guard_turn(env, repo, "s", "stuck", _stuck())
    ctx_key = lambda o: (o.get("hookSpecificOutput") or {}).get("additionalContext")
    shown = lambda o: o.get("systemMessage") or ""
    to_model = [ctx_key(o) for o in outs if ctx_key(o)]
    stuck = [shown(o) for o in outs if "still looks stuck" in shown(o)]
    assert not any("continue" in o or "stopReason" in o for o in outs)  # never a stop
    assert len(stuck) == 1                                             # one notice
    assert stuck[0].endswith(hosts.Omp().stop_hint(mode == "headless"))
    assert len(to_model) == 1                                          # the redirect alone


def _fake_judges(monkeypatch, seen, stuck=True):
    from tokeneconomizer.loopguard import judge as reflect

    def judge(digest, runner, model="agent"):
        seen.append(digest)
        return {"stuck": stuck, "confidence": 0.95, "reason": "loop", "_tokens": 1000.0}

    def writer(digest, runner, model="agent"):
        return {"text": "Read the failing assertion before editing again.", "_tokens": 300.0}
    monkeypatch.setattr(reflect, "llm_stuck_judge", judge)
    monkeypatch.setattr(reflect, "llm_nudge_writer", writer)


def test_the_judge_answers_from_a_job_and_its_spend_is_charged_as_it_happens(
        env, repo, monkeypatch):
    """The job runs inline here (see conftest); in the plugin it is a detached process."""
    from tokeneconomizer.plugin import accounting
    seen = []
    _fake_judges(monkeypatch, seen)
    paid = env
    for k in range(5):
        _turn(paid, repo, "s", f"g{k}", GREEN)
    outs = _turn(paid, repo, "s", "stuck", stuck_calls(40))
    nudge = _first(outs, lambda o: "hookSpecificOutput" in o)
    assert nudge is not None and seen
    assert outs[nudge]["hookSpecificOutput"]["additionalContext"] == (
        "Read the failing assertion before editing again.")
    # The call that started the job answered nothing; its verdict arrived on a later call.
    started_at = int(seen[0].split()[0].split("=")[1])       # the digest opens "turns=N"
    assert nudge + 1 > started_at
    assert all("NOT green" not in d for d in seen)
    ledger = store_for(env, repo).read_lines("ledger.jsonl")
    charged = [r for r in ledger if r.get("kind") == "overhead"]
    assert charged and charged[0]["session_id"] == "s" and charged[0]["overhead"] == {
        "spiral_judge_tokens": 1000.0, "spiral_nudge_writer_tokens": 300.0}
    assert accounting.summarize(ledger)["overhead_tokens"] == pytest.approx(
        1300.0 * len(charged))


def test_a_hook_only_starts_the_judge_and_never_waits_for_it(env, repo, monkeypatch):
    from tokeneconomizer.plugin import judge as live_judge
    seen, spawned = [], []
    _fake_judges(monkeypatch, seen)
    monkeypatch.setattr(live_judge, "spawn_job", lambda ctx, sid, job: spawned.append(job))
    paid = env
    for k in range(5):
        _turn(paid, repo, "s", f"g{k}", GREEN)
    _turn(paid, repo, "s", "stuck", stuck_calls(12))
    assert len(spawned) == 1 and seen == []           # started once, never run in the hook
    req = store_for(env, repo).read("judge/s.request.json")
    assert req["id"] == spawned[0] and "NOT green" not in req["digest"]


def _redirects(outs):
    return [i for i, o in enumerate(outs) if "hookSpecificOutput" in o]


def test_the_judge_decides_every_redirect_but_the_backstops(env, repo, monkeypatch):
    """With the guard on, a turn that shows signs of looping goes to the judge, and only a
    confident `stuck` redirects it; a `productive` verdict spares it, and only the backstop --
    far past the usual turn AND the strict stall pattern, which the judge cannot veto -- still
    acts. The rule alone, as the guard ran without a judge, redirected this turn at call 12."""
    _warm(env, repo)                                   # usual turn: 8 calls, envelope 10
    seen = []
    _fake_judges(monkeypatch, seen, stuck=False)
    spared = _turn(env, repo, "s", "spared", stuck_calls(40))
    first = _redirects(spared)
    assert seen and first and first[0] + 1 >= 25       # the backstop: 2.5 x the envelope
    _fake_judges(monkeypatch, seen, stuck=True)
    judged = _turn(env, repo, "s", "judged", stuck_calls(40))
    first = _redirects(judged)
    assert first and first[0] + 1 < 25
    assert judged[first[0]]["hookSpecificOutput"]["additionalContext"] == (
        "Read the failing assertion before editing again.")


def test_the_judge_is_not_asked_before_the_repository_is_learned(env, repo, monkeypatch):
    seen = []
    _fake_judges(monkeypatch, seen)
    assert all(o == {} for o in _turn(env, repo, "s", "cold", stuck_calls(40)))
    assert seen == []


def test_a_long_turn_with_no_sign_of_trouble_never_wakes_the_judge(env, repo, monkeypatch):
    """Interactive turns outrun a small envelope all the time: that alone asks nothing."""
    _warm(env, repo)
    seen = []
    _fake_judges(monkeypatch, seen)
    long_and_clean = [("Read", {"file_path": f"/r/m{k}.py"}, "ok") for k in range(60)]
    assert all(o == {} for o in _turn(env, repo, "s", "long", long_and_clean))
    assert seen == []


def test_while_model_calls_are_paused_only_the_backstop_redirects(env, repo, monkeypatch):
    from tokeneconomizer.plugin import sidecall
    _warm(env, repo)
    seen = []
    _fake_judges(monkeypatch, seen)
    monkeypatch.setattr(sidecall, "paused", lambda host, root=None: {"at": 1.0})
    outs = _turn(env, repo, "s", "stuck", stuck_calls(40))
    first = _redirects(outs)
    assert seen == [] and first and first[0] + 1 >= 25


def _warm(env, repo):
    for k in range(5):
        _turn(env, repo, "s", f"g{k}", GREEN)


def test_only_turns_that_end_green_feed_the_envelope(env, repo):
    fail = {"content": "FAILED tests/test_a.py::t - AssertionError", "isError": True,
            "exit_code": 1}
    _turn(env, repo, "s", "red", [("edit", {"path": "/r/a.py"}, {"content": "ok"}),
                                  ("bash", {"command": "pytest -q"}, fail)])
    _turn(env, repo, "s", "green", [("edit", {"path": "/r/a.py"}, {"content": "ok"}),
                                    ("bash", {"command": "pytest -q"},
                                     {"content": "3 passed", "exit_code": 0})])
    assert store_for(env, repo).read("envelope.json")["resolved_turns"] == [2]


# ---- untooling -----------------------------------------------------------------------------
def _catalog(env, tools):
    d = os.path.join(env["TOKENECONOMIZER_HOME"], "hosts", "omp")
    os.makedirs(d, exist_ok=True)
    json.dump({"tools": tools}, open(os.path.join(d, "catalog.json"), "w"))


def _deny(env, repo):
    return sorted(denylist.applied_for(store_for(env, repo), repo))


def test_with_propose_unused_tools_are_proposed_but_nothing_changes_until_applied(
        env, repo, tmp_path):
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["bash", "read", "edit", "write", "AskUserQuestion", "Workflow",
                   "CronCreate", "ShareOnboardingGuide", "WebFetch"])
    for i in range(5):
        t = write_session(str(tmp_path / f"u{i}.jsonl"), [("look", [
            ("read", {"path": f"{repo}/a.py"}, "x", False),
            ("bash", {"command": "ls"}, "a.py", False)]), ("and again", [])])
        run_session(env, repo, f"u{i}", t)
    st = store_for(env, repo)
    prop = st.read("untool.json")["proposal"]
    # protected tools (incl. a person's AskUserQuestion, and the web) are never proposed
    assert sorted(prop) == ["CronCreate", "ShareOnboardingGuide", "Workflow"]
    assert _deny(env, repo) == []                                # proposing writes nothing
    t = str(tmp_path / "next.jsonl")
    open(t, "w").close()
    notice = call(env, {"hook_event_name": "SessionStart", "session_id": "n", "source": "startup",
                        "transcript_path": t, "cwd": repo})["systemMessage"]
    assert "3 tool(s) went unused" in notice and "/te toolprune" in notice
    assert "nothing changes until you run `/te toolprune apply`" in notice


def test_by_default_unused_tools_are_removed_from_the_next_session_and_it_says_so(
        env, repo, tmp_path):
    """`toolprune=auto` is the default: the fold that forms the proposal writes it, the next
    session start says so once, and `restore` gives the tools back."""
    _catalog(env, ["bash", "read", "edit", "write", "AskUserQuestion", "Workflow",
                   "CronCreate", "ShareOnboardingGuide", "WebFetch"])
    for i in range(5):
        t = write_session(str(tmp_path / f"u{i}.jsonl"), [("look", [
            ("read", {"path": f"{repo}/a.py"}, "x", False),
            ("bash", {"command": "ls"}, "a.py", False)]), ("and again", [])])
        run_session(env, repo, f"u{i}", t)
    st = store_for(env, repo)
    assert _deny(env, repo) == ["CronCreate", "ShareOnboardingGuide", "Workflow"]
    assert st.read("untool.json")["proposal"] == []
    t = str(tmp_path / "next.jsonl")
    open(t, "w").close()

    def start(sid):
        return call(env, {"hook_event_name": "SessionStart", "session_id": sid,
                          "source": "startup", "transcript_path": t, "cwd": repo})
    notice = start("n1")["systemMessage"]
    assert "removed 3 tool(s) this repository's sessions never used" in notice
    assert "`/te toolprune restore` gives them back" in notice
    assert "systemMessage" not in start("n2")                   # said once


def test_tools_given_back_stay_back_until_named_again(env, repo, tmp_path, monkeypatch, capsys):
    """Under the default `auto`, `restore` must stick: the evidence still says the tools go
    unused, and the next fold would otherwise take them away again."""
    from tokeneconomizer.plugin import cli
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _catalog(env, ["bash", "read", "Workflow", "CronCreate"])

    def sessions(prefix, n):
        for i in range(n):
            t = write_session(str(tmp_path / f"{prefix}{i}.jsonl"), [("look", [
                ("bash", {"command": "ls"}, "a.py", False)]), ("and again", [])])
            run_session(env, repo, f"{prefix}{i}", t)
    sessions("a", 5)
    assert _deny(env, repo) == ["CronCreate", "Workflow"]
    assert cli.main(["--host", "omp", "--cwd", repo, "toolprune", "restore"]) == 0
    assert "kept from now on" in capsys.readouterr().out and _deny(env, repo) == []
    sessions("b", 6)                                   # still unused, still kept
    assert _deny(env, repo) == []
    assert cli.main(["--host", "omp", "--cwd", repo, "toolprune", "show"]) == 0
    out = capsys.readouterr().out
    assert "proposed: ->" in out
    assert "kept (gave back, never re-applied in auto): CronCreate" in out
    assert cli.main(["--host", "omp", "--cwd", repo, "toolprune", "apply", "Workflow"]) == 0
    assert _deny(env, repo) == ["Workflow"]
    assert denylist.kept_for(store_for(env, repo), repo) == ["CronCreate"]


def test_a_tool_that_appears_after_a_deny_list_is_written_joins_the_catalog(
        env, repo, tmp_path):
    """A deny list written by `auto` must not freeze the catalog: an MCP server added later
    (reported at a later SessionStart) gets its own streak and is left out once it has gone
    unused long enough."""
    _catalog(env, ["bash", "read", "Workflow", "CronCreate"])

    def sessions(prefix, n):
        for i in range(n):
            t = write_session(str(tmp_path / f"{prefix}{i}.jsonl"), [("look", [
                ("bash", {"command": "ls"}, "a.py", False)]), ("and again", [])])
            run_session(env, repo, f"{prefix}{i}", t)
    sessions("a", 5)
    assert _deny(env, repo) == ["CronCreate", "Workflow"]
    new = "mcp__echosvr__echo"
    _catalog(env, ["bash", "read", "Workflow", "CronCreate", new])
    sessions("b", 1)
    unt = store_for(env, repo).read("untool.json")
    assert new in unt["catalog_tools"] and unt["streaks"][new] == 1
    sessions("c", 4)
    assert _deny(env, repo) == ["CronCreate", "Workflow", new]


def _prompts(env, repo, tmp_path, sid, n):
    """One omp session of `n` prompts; only the first calls a tool (bash)."""
    t = write_session(str(tmp_path / f"{sid}.jsonl"),
                      [(f"prompt {k}", [("bash", {"command": "ls"}, "a.py", False)] if k == 0
                        else []) for k in range(n)])
    run_session(env, repo, sid, t)


def _another_repo(tmp_path, name):
    r = tmp_path / name
    (r / ".git").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return str(r)


def test_ten_prompts_over_two_sessions_are_enough_and_one_sessions_are_not(env, repo, tmp_path):
    """The evidence counts prompts as well as sessions: a never-used tool is left out after ten
    prompts over at least two sessions. One session's prompts are one piece of work, so ten of
    them in a single session are not enough -- unless `toolprune_min_sessions=1`."""
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["bash", "read", "Workflow", "CronCreate"])
    _prompts(env, repo, tmp_path, "long", 10)
    st = store_for(env, repo)
    unt = st.read("untool.json")
    assert unt["proposal"] == []
    assert (unt["streaks"]["Workflow"], unt["prompt_streaks"]["Workflow"]) == (1, 10)
    _prompts(env, repo, tmp_path, "short", 1)
    assert sorted(st.read("untool.json")["proposal"]) == ["CronCreate", "Workflow"]   # not read

    one = dict(env, TE_TOOLPRUNE_MIN_SESSIONS="1")
    nine, ten = _another_repo(tmp_path, "nine"), _another_repo(tmp_path, "ten")
    _prompts(one, nine, tmp_path, "s9", 9)
    _prompts(one, ten, tmp_path, "s10", 10)
    assert store_for(one, nine).read("untool.json")["proposal"] == []
    assert sorted(store_for(one, ten).read("untool.json")["proposal"]) == ["CronCreate", "Workflow"]


def test_five_sessions_are_enough_however_short(env, repo, tmp_path):
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["bash", "Workflow"])
    for i in range(4):
        _prompts(env, repo, tmp_path, f"s{i}", 1)
    assert store_for(env, repo).read("untool.json")["proposal"] == []
    _prompts(env, repo, tmp_path, "s4", 1)
    assert store_for(env, repo).read("untool.json")["proposal"] == ["Workflow"]


def test_a_session_without_prompts_is_no_evidence(env, repo, tmp_path):
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["bash", "Workflow"])
    _prompts(env, repo, tmp_path, "some", 4)
    _prompts(env, repo, tmp_path, "none", 0)
    unt = store_for(env, repo).read("untool.json")
    assert (unt["sessions"], unt["streaks"]["Workflow"], unt["prompt_streaks"]["Workflow"]) == (
        1, 1, 4)


def test_a_high_risk_tool_needs_three_times_the_prompts(env, repo, tmp_path):
    env = dict(env, TE_TOOLPRUNE="propose")
    _catalog(env, ["bash", "Workflow", "NotebookEdit"])
    _prompts(env, repo, tmp_path, "a", 20)
    _prompts(env, repo, tmp_path, "b", 9)
    assert store_for(env, repo).read("untool.json")["proposal"] == ["Workflow"]
    _prompts(env, repo, tmp_path, "c", 1)
    assert sorted(store_for(env, repo).read("untool.json")["proposal"]) == [
        "NotebookEdit", "Workflow"]


def test_a_protected_tool_is_never_left_out_and_a_rule_already_written_comes_back(
        env, repo, tmp_path):
    """`toolprune_protect` takes names and patterns. A list entry the plugin recorded before the
    tool was protected is given back at the next fold."""
    _catalog(env, ["bash", "mcp__headroom__headroom_retrieve", "Workflow"])
    st = store_for(env, repo)
    denylist.apply(repo, ["mcp__headroom__headroom_retrieve", "Workflow"], st)
    for i in range(5):
        _prompts(env, repo, tmp_path, f"a{i}", 3)
    assert _deny(env, repo) == ["Workflow", "mcp__headroom__headroom_retrieve"]
    env = dict(env, TE_TOOLPRUNE_PROTECT="mcp__headroom__*, Nothing")
    _prompts(env, repo, tmp_path, "b", 3)
    assert _deny(env, repo) == ["Workflow"]
    assert "mcp__headroom__headroom_retrieve" not in store_for(env, repo).read(
        "untool.json")["proposal"]


def test_tooling_stage_fold_session_folds_one_session_into_the_next_doc(tmp_path):
    """`ToolingStage.fold_session` takes the prior untool.json doc and one session, and
    returns the next doc plus the outcome -- one session is not enough evidence."""
    from tokeneconomizer.config import ToolingConfig
    from tokeneconomizer.plugin.adapter import LiveAdapter
    from tokeneconomizer.toolprune import ToolingStage

    class Host:
        def tool_usage(self, path):
            return {"bash": {}}

    class Cfg:
        toolprune_evidence = 10
        toolprune_min_sessions = 2
        toolprune_sessions = 5

    adapter = LiveAdapter(Host(), str(tmp_path), catalog={"bash": "", "Workflow": ""})
    stage = ToolingStage(ToolingConfig(evidence=1), "omp", "agent", adapter=adapter)
    run = tmp_path / "run"
    run.mkdir()
    next_doc, outcome = stage.fold_session(
        adapter, str(run), "s1", 3, {}, config=Cfg(), applied=(), kept=(),
        high_risk=frozenset(), high_risk_factor=3)
    assert next_doc["sessions"] == 1 and next_doc["prompts"] == 3
    assert next_doc["streaks"] == {"bash": 0, "Workflow": 1}
    assert next_doc["prompt_streaks"] == {"bash": 0, "Workflow": 3}
    assert next_doc["catalog_source"] == "probe+observed"
    assert next_doc["proposal"] == [] and next_doc["new_in_proposal"] == []
    assert outcome == {"proposal": [], "pending": False, "restored": []}


def _pending_repo(env, repo, tmp_path, quiet="60"):
    """Five sessions that leave three tools unused, under the default wait before writing."""
    env = dict(env, TE_TOOLPRUNE_QUIET_MINUTES=quiet)
    _catalog(env, ["bash", "read", "edit", "write", "AskUserQuestion", "Workflow",
                   "CronCreate", "ShareOnboardingGuide", "WebFetch"])
    for i in range(5):
        t = write_session(str(tmp_path / f"u{i}.jsonl"), [("look", [
            ("read", {"path": f"{repo}/a.py"}, "x", False),
            ("bash", {"command": "ls"}, "a.py", False)]), ("and again", [])])
        run_session(env, repo, f"u{i}", t)
    return env, LiveContext.build(hosts.Omp(), repo, env)


def test_by_default_a_deny_list_waits_until_the_repositorys_sessions_have_been_idle(
        env, repo, tmp_path, monkeypatch, capsys):
    """A conversation resumed after its tools changed re-writes its whole history to the prompt
    cache, so `auto` writes a new list only once every session has been idle longer than a
    cache lives -- silently until then, and announced once written."""
    from tokeneconomizer.plugin import cli
    env, ctx = _pending_repo(env, repo, tmp_path)
    assert _deny(env, repo) == []
    pend = ctx.store.read("untool.json")["auto_pending"]
    assert sorted(pend["items"]) == ["CronCreate", "ShareOnboardingGuide", "Workflow"]
    t = str(tmp_path / "next.jsonl")
    open(t, "w").close()

    def start(sid):
        return call(env, {"hook_event_name": "SessionStart", "session_id": sid,
                          "source": "startup", "transcript_path": t, "cwd": repo})
    assert "went unused" not in start("n1").get("systemMessage", "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["--host", "omp", "--cwd", repo, "toolprune", "show"]) == 0
    assert "sessions have been idle 60 minutes" in (capsys.readouterr().out)
    now = time.time()
    wait = fold.apply_pending(ctx, now=now + 600)                   # ten idle minutes: not yet
    assert 2900 < wait <= 3000 and _deny(env, repo) == []
    assert fold.apply_pending(ctx, now=now + 3700) is None           # an idle hour: written
    assert _deny(env, repo) == ["CronCreate", "ShareOnboardingGuide", "Workflow"]
    assert "auto_pending" not in ctx.store.read("untool.json")
    assert "removed 3 tool(s)" in start("n2")["systemMessage"]


def test_a_tool_protected_after_the_list_formed_is_not_written(env, repo, tmp_path):
    """A pending list, and the proposal `toolprune apply` reads, are
    checked against the protection in force when they are used, not when they formed."""
    from tokeneconomizer.plugin import cli
    env, ctx = _pending_repo(env, repo, tmp_path)
    assert ctx.config.set("toolprune_protect", "Workflow,Share*")
    assert cli._proposal(ctx, {"proposal": ["CronCreate", "Workflow"]}) == ["CronCreate"]
    assert fold.apply_pending(ctx, now=time.time() + 3700) is None
    assert _deny(env, repo) == ["CronCreate"]
    assert ctx.store.read("untool.json")["proposal"] == []      # applied, or protected now


def test_a_session_still_writing_its_log_keeps_the_deny_list_waiting(env, repo, tmp_path):
    """Activity is a hook call or a line in the session's own log: a session can go an hour
    without a hook (one long answer, or the loop guard off) and still have a warm cache."""
    env, ctx = _pending_repo(env, repo, tmp_path)
    old = time.time() - 7200
    logs = glob.glob(str(tmp_path / "u*.jsonl"))
    for f in glob.glob(ctx.store.path("sessions", "*.json")) + logs:
        os.utime(f, (old, old))
    os.utime(str(tmp_path / "u4.jsonl"))          # no hook for two hours, but its log moved now
    assert fold.apply_pending(ctx) > 3500 and _deny(env, repo) == []
    os.utime(str(tmp_path / "u4.jsonl"), (old, old))
    assert fold.apply_pending(ctx) is None and len(_deny(env, repo)) == 3


def test_a_deny_list_that_waited_a_day_is_written_even_if_sessions_never_pause(
        env, repo, tmp_path):
    env, ctx = _pending_repo(env, repo, tmp_path)
    st = ctx.store.read("untool.json")
    st["auto_pending"]["since"] = time.time() - fold.PENDING_MAX_S - 1
    ctx.store.write("untool.json", st)
    assert fold.apply_pending(ctx) is None and len(_deny(env, repo)) == 3


def test_a_pending_deny_list_leaves_out_what_was_given_back_meanwhile(env, repo, tmp_path):
    env, ctx = _pending_repo(env, repo, tmp_path)
    denylist.set_kept(ctx.store, repo, "omp", add=["Workflow"])        # `restore`d earlier
    assert fold.apply_pending(ctx, now=time.time() + 3700) is None
    assert _deny(env, repo) == ["CronCreate", "ShareOnboardingGuide"]


def test_a_pending_deny_list_is_dropped_once_toolprune_is_no_longer_auto(env, repo, tmp_path):
    env, ctx = _pending_repo(env, repo, tmp_path)
    ctx = LiveContext.build(hosts.Omp(), repo, dict(env, TE_TOOLPRUNE="propose"))
    assert fold.apply_pending(ctx, now=time.time() + 3700) is None
    unt = ctx.store.read("untool.json")
    assert _deny(env, repo) == [] and "auto_pending" not in unt and len(unt["proposal"]) == 3


def test_the_fold_leaves_one_detached_waiter_and_a_second_one_leaves(
        env, repo, tmp_path, monkeypatch):
    from tokeneconomizer.plugin import cli
    env, ctx = _pending_repo(env, repo, tmp_path)
    spawned = []
    monkeypatch.setattr(LiveContext, "spawn", lambda self, argv, log: spawned.append(argv))
    detached = LiveContext.build(hosts.Omp(), repo,
                                 {k: v for k, v in env.items() if k != "TE_FOLD_SYNC"})
    assert fold.schedule_pending(detached) > 0
    assert spawned == [["toolprune", "apply-pending"]]

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError(s)))
    beat = ctx.store.path("toolprune-waiter")
    open(beat, "w").close()                                          # another waiter is alive
    assert cli.main(["--host", "omp", "--cwd", repo, "toolprune", "apply-pending"]) == 0
    assert _deny(env, repo) == [] and os.path.exists(beat)
    os.remove(beat)
    monkeypatch.setenv("TE_TOOLPRUNE_QUIET_MINUTES", "0")            # quiet enough at once
    assert cli.main(["--host", "omp", "--cwd", repo, "toolprune", "apply-pending"]) == 0
    assert len(_deny(env, repo)) == 3 and not os.path.exists(beat)


def test_the_web_is_never_proposed_and_an_old_deny_rule_for_it_is_given_back(
        env, repo, tmp_path):
    _catalog(env, ["bash", "read", "CronCreate", "WebFetch", "WebSearch"])
    st = store_for(env, repo)
    # A deny list accepted under an earlier version, which still proposed the web.
    denylist.apply(repo, ["CronCreate", "WebFetch", "WebSearch"], st)
    for i in range(20):                       # far past the old 3x evidence bar
        t = write_session(str(tmp_path / f"w{i}.jsonl"), [("look", [
            ("bash", {"command": "ls"}, "a.py", False)])])
        run_session(env, repo, f"w{i}", t)
    assert denylist.applied(st) == ["CronCreate"]
    assert _deny(env, repo) == ["CronCreate"]
    assert not {"WebFetch", "WebSearch"} & set(st.read("untool.json")["proposal"])


def test_apply_and_restore_touch_only_the_plugins_own_entries(repo, tmp_path):
    st = Store(str(tmp_path / "st"))
    assert denylist.apply(repo, ["Workflow", "CronCreate"], st) == ["CronCreate", "Workflow"]
    assert denylist.apply(repo, ["Workflow"], st) == []            # already present
    assert denylist.restore(repo, st, ["CronCreate"]) == ["CronCreate"]
    assert denylist.restore(repo, st) == ["Workflow"]
    assert denylist.applied_for(st, repo) == []


def test_restore_takes_back_an_entry_whichever_write_of_apply_failed(repo, tmp_path, monkeypatch):
    st = Store(str(tmp_path / "st"))
    write = Store.write

    def record_fails(self, name, obj):
        if name == denylist.APPLIED:
            raise PermissionError(13, "the record is open in another process")
        return write(self, name, obj)
    with monkeypatch.context() as m:
        m.setattr(Store, "write", record_fails)
        with pytest.raises(PermissionError):
            denylist.apply(repo, ["Workflow"], st)
    denylist.restore(repo, st)
    assert denylist.applied_for(st, repo) == []


# ---- folding and the ledger ----------------------------------------------------------------
def test_each_session_is_folded_once_and_priced(env, repo, tmp_path):
    t = trap_session(str(tmp_path / "s.jsonl"), repo, 0)
    run_session(env, repo, "s", t)
    call(env, {"hook_event_name": "SessionEnd", "session_id": "s", "transcript_path": t,
               "cwd": repo, "reason": "other"})
    ledger = store_for(env, repo).read_lines("ledger.jsonl")
    assert len(ledger) == 1
    rec = ledger[0]
    # 3 requests x (10 fresh + 0.10 x 1000 cached + 1.25 x 100 written + 6 x 50 output);
    # the session's model, gpt-5.6-sol, bills output at 6x input
    assert rec["requests"] == 3 and rec["effective_tokens"] == pytest.approx(3 * 535.0)
    assert rec["priced_by"] == "model" and set(rec["overhead"]) == {
        "optimizer_tokens", "spiral_judge_tokens", "spiral_nudge_writer_tokens"}


def test_a_fold_that_cannot_take_the_lock_skips_and_the_session_is_still_folded_once(
        env, repo, tmp_path, monkeypatch):
    """A paid fold holds the lock through its side call; a second fold of the same session that
    cannot take it skips, leaving the session for the holder or a later start: it is folded once."""
    from tokeneconomizer.plugin import fold, hosts
    from tokeneconomizer.plugin.store import _try_lock, _unlock
    from tokeneconomizer.plugin.context import LiveContext
    t = trap_session(str(tmp_path / "s.jsonl"), repo, 0)
    base = {"session_id": "s", "transcript_path": t, "cwd": repo}
    call(env, dict(base, hook_event_name="SessionStart", source="startup"))
    st = store_for(env, repo)
    monkeypatch.setattr(fold, "FOLD_LOCK_WAIT_S", 0.2)
    holder = open(st.path(".fold.lock"), "a+")          # another fold, mid side call
    _try_lock(holder)
    try:
        call(env, dict(base, hook_event_name="SessionEnd", reason="other"))   # folds inline
        assert st.read_lines("ledger.jsonl") == []
        assert "s" not in (st.read("folded.json") or {})
        ctx = LiveContext.build(hosts.Omp(), repo, env)
        assert "s" in fold.pending_sessions(ctx)        # left for the holder, or a later start
    finally:
        _unlock(holder)
        holder.close()
    assert fold.fold_session(ctx, "s") is not None
    assert fold.fold_session(ctx, "s") is None
    assert len(st.read_lines("ledger.jsonl")) == 1


def test_a_session_whose_end_was_missed_is_folded_by_the_next_start(env, repo, tmp_path):
    t = trap_session(str(tmp_path / "lost.jsonl"), repo, 0)
    call(env, {"hook_event_name": "SessionStart", "session_id": "lost", "source": "startup",
               "transcript_path": t, "cwd": repo})               # ... then the terminal died
    old = os.path.getmtime(t) - 3600
    os.utime(t, (old, old))
    t2 = str(tmp_path / "next.jsonl")
    open(t2, "w").close()
    call(env, {"hook_event_name": "SessionStart", "session_id": "next", "source": "startup",
               "transcript_path": t2, "cwd": repo})
    assert "lost" in store_for(env, repo).read("folded.json")


# ---- never break the session ---------------------------------------------------------------
def test_garbage_input_and_internal_errors_answer_no_change(env, repo, monkeypatch):
    out = io.StringIO()
    hook.main([], io.StringIO("not json"), out, env)
    assert json.loads(out.getvalue()) == {}

    def boom(ctx, payload, now=None):
        raise RuntimeError("bug")
    monkeypatch.setattr(turnsession, "handle", boom)
    assert call(env, {"hook_event_name": "Stop", "session_id": "s", "cwd": repo}) == {}
    log = open(os.path.join(env["TOKENECONOMIZER_HOME"], "errors.log")).read()
    assert "RuntimeError: bug" in log


def test_a_non_ascii_checkout_is_found_whatever_the_locale(env, tmp_path):
    """The agents send UTF-8. A text stdin decodes with the locale's code page -- on a Russian
    Windows, cp1251 turns `Программирование` into `РџСЂРѕРіСЂ...`, a directory that does not
    exist -- so the hook reads the bytes, and the session is the checkout's."""
    repo = tmp_path / "Программирование" / "agentsync"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    raw = json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup",
                      "transcript_path": "", "cwd": str(repo)}, ensure_ascii=False)
    stdin = io.TextIOWrapper(io.BytesIO(raw.encode("utf-8")), encoding="cp1251")
    hook.main([], stdin, io.StringIO(), env)
    assert store_for(env, str(repo)).read("sessions/s1.json")
    assert not os.path.exists(os.path.join(env["TOKENECONOMIZER_HOME"], "errors.log"))


def test_side_calls_a_disabled_plugin_and_a_payload_without_a_session_do_nothing(env, repo):
    for flag in ("TE_SIDECALL", "TE_DISABLE"):
        assert call(dict(env, **{flag: "1"}), {"hook_event_name": "SessionStart",
                                                "session_id": "s", "cwd": repo}) == {}
    assert call(env, {"hook_event_name": "SessionStart", "cwd": repo}) == BARE_START
    assert not os.path.exists(env["TOKENECONOMIZER_HOME"])


def test_mechanism_prints_cannot_corrupt_the_hook_output(env, repo, tmp_path, capsys):
    """ToolingStage prints a warning when it parses no catalog; inline folds must not leak it."""
    t = str(tmp_path / "empty.jsonl")
    open(t, "w").close()
    out = io.StringIO()
    hook.main([], io.StringIO(json.dumps({"hook_event_name": "SessionEnd", "session_id": "e",
                                          "transcript_path": t, "cwd": repo})), out, env)
    assert json.loads(out.getvalue()) == {}


# ---- omp sessions --------------------------------------------------------------------------
def omp_trap_session(path, repo, i):
    """The trap session, as an omp session file."""
    lines = [{"type": "session", "version": 3, "id": f"omp{i}",
              "timestamp": "2026-09-30T00:00:00Z", "cwd": repo}]
    calls = [("edit", {"path": f"{repo}/pkg/mod{i}.py"}, "ok", False),
             ("bash", {"command": "pytest -q tests/test_a.py"}, "bash: pytest: command not found",
              True),
             ("bash", {"command": "python -m pytest -q tests/test_a.py"}, "1 passed in 0.1s",
              False)]
    parent, n = None, 0
    entries = [{"role": "user", "content": f"fix bug {i}"}]
    for name, args, res, err in calls:
        entries.append({"role": "assistant", "model": "gpt-5.6-sol", "usage": {
            "input": 10, "output": 50, "cacheRead": 1000, "cacheWrite": 100, "totalTokens": 1160,
            "cost": {"total": 0.001}}, "content": [{"type": "toolCall", "id": f"c{name}{n}",
                                                    "name": name, "arguments": args}]})
        entries.append({"role": "toolResult", "toolCallId": f"c{name}{n}", "toolName": name,
                        "isError": err, "content": [{"type": "text", "text": res}]})
        n += 1
    for k, m in enumerate(entries):
        eid = f"{i}-{k}"
        lines.append({"type": "message", "id": eid, "parentId": parent,
                      "timestamp": "2026-09-30T00:00:01Z", "message": m})
        parent = eid
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_omp_learns_from_its_own_session_files_and_injects_at_the_next_start(env, repo, tmp_path):
    starts = [run_session(env, repo, f"omp{i}",
                          omp_trap_session(str(tmp_path / f"p{i}.jsonl"), repo, i))
              for i in range(5)]
    assert all(s == BARE_START for s in starts[:4])
    ctx = starts[4]["hookSpecificOutput"]["additionalContext"]
    assert "`python -m pytest …` works." in ctx
    assert "/te microskills" in starts[4]["systemMessage"]
    rec = store_for(env, repo).read_lines("ledger.jsonl")[0]
    assert rec["requests"] == 3 and rec["priced_by"] == "model"


def test_omp_print_mode_counts_as_headless_for_the_stop_hint(env, repo):
    """omp says its mode through the extension (`TE_OMP_MODE`); a print-mode notice names the
    headless way to stop."""
    penv = dict(env, TE_OMP_MODE="print")
    for k in range(5):
        _guard_turn(penv, repo, "p", f"g{k}", GREEN)
    outs = _guard_turn(penv, repo, "p", "stuck", _stuck())
    notice = next(o["systemMessage"] for o in outs
                  if "still looks stuck" in o.get("systemMessage", ""))
    assert notice.endswith(hosts.Omp().stop_hint(True))
