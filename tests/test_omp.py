"""oh-my-pi (`omp`) as a host: the turn identity and tool result its adapter sends, its headless
modes, its session log read through the Pi trace, the shell it marks, and its side call."""
import io
import json
import os
import subprocess

import pytest

from tokeneconomizer.plugin import (adapter, cli, config as live_config, denylist, hook, hosts,
                                    paths, sidecall)
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.hosts import Omp
from tokeneconomizer.plugin.store import Store

SESSION = {"type": "session", "version": 3, "id": "01a1", "cwd": "C:/repo"}


def omp_session(tmp_path):
    """One omp session on a linear branch: prompt, assistant tool call, failed tool result."""
    path = tmp_path / "omp.jsonl"
    entries = [
        SESSION,
        {"type": "message", "id": "m1", "parentId": "01a1", "message": {
            "role": "user", "content": [{"type": "text", "text": "run the tests"}]}},
        {"type": "message", "id": "m2", "parentId": "m1", "message": {
            "role": "assistant", "model": "anthropic/claude-haiku-4.5",
            "content": [{"type": "toolCall", "id": "call_1", "name": "bash",
                         "arguments": {"command": "pytest"}}],
            "usage": {"input": 100, "output": 20, "cacheRead": 40, "cacheWrite": 0,
                      "cost": {"total": 0.002}}}},
        {"type": "message", "id": "m3", "parentId": "m2", "message": {
            "role": "toolResult", "toolCallId": "call_1", "isError": True,
            "content": [{"type": "text", "text": "1 failed"}]}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def test_the_omp_turn_is_the_prompt_id_and_the_tool_result_carries_its_exit_code():
    h = Omp()
    assert h.turn_key({"prompt_id": "01a1:1"}) == "01a1:1"
    name, args, text, code = h.tool_call({
        "hook_event_name": "PostToolUse", "prompt_id": "01a1:1", "tool_name": "bash",
        "tool_input": {"command": "pytest"},
        "tool_response": {"content": "1 failed", "isError": True, "exit_code": 1}})
    assert (name, args, text, code) == ("bash", {"command": "pytest"}, "1 failed", 1)


def test_omp_is_headless_only_in_its_print_modes():
    h = Omp()
    for mode in ("print", "json", "rpc"):
        assert h.headless({"TE_OMP_MODE": mode}), mode
    assert not h.headless({"TE_OMP_MODE": "tui"})
    assert not h.headless({})


def test_every_omp_session_is_its_own_and_never_a_subagent():
    h = Omp()
    assert h.is_subagent({"agent_id": "sub-1"}) is False
    assert h.prompt_context is True and h.keeps_start_context is False
    assert h.stop_hint(False) == "Press Esc to abort it."
    assert h.stop_hint(True).startswith("Press Ctrl+C to end the run")


def test_the_omp_session_is_read_through_the_pi_trace(tmp_path):
    h = Omp()
    src = omp_session(tmp_path)
    out = h.mechanism_trace(str(src), str(tmp_path))
    assert out == str(tmp_path / "omp_trace.jsonl")
    lines = [json.loads(ln) for ln in open(out, encoding="utf-8")]
    calls = [b for ln in lines if ln.get("type") == "assistant"
             for b in ln["message"]["content"] if b.get("type") == "tool_use"]
    results = [b for ln in lines if ln.get("type") == "user"
               and isinstance(ln["message"]["content"], list)
               for b in ln["message"]["content"] if b.get("type") == "tool_result"]
    assert len(calls) == 1 and calls[0]["name"] == "bash"
    assert len(results) == 1 and results[0]["is_error"] is True
    assert h.prompt_count(str(src)) == 1
    usage = h.session_usage(str(src))
    assert (usage["input_tokens"], usage["cache_read_input_tokens"],
            usage["output_tokens"], usage["requests"]) == (100, 40, 20, 1)
    assert usage["cost_usd_vendor"] == pytest.approx(0.002)
    assert usage["model"] == "anthropic/claude-haiku-4.5"


def test_omp_marks_the_shell_its_bash_tool_starts():
    assert hosts.default_host({"PI_SESSION_ID": "s"}) == "omp"
    assert hosts.default_host({"PI_SESSION_FILE": "f"}) == "omp"
    assert hosts.default_host({"TE_HOST": "codex", "PI_SESSION_ID": "s"}) == "codex"
    assert hosts.detect({}, hint="omp").name == "omp"


@pytest.mark.real_sidecalls
def test_an_omp_side_call_runs_the_omp_cli_headless(monkeypatch):
    calls = []
    monkeypatch.setattr(sidecall, "program", lambda cli: [cli])

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
            "type": "message_end", "message": {
                "role": "assistant", "model": "anthropic/claude-haiku-4.5",
                "content": [{"type": "text", "text": "- use python -m pytest"}],
                "usage": {"input": 100, "output": 20, "cacheRead": 40, "cacheWrite": 0,
                          "cost": {"total": 0.002}}}}), stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.omp_call("propose facts")
    assert text == "- use python -m pytest" and eff and eff > 0
    cmd, kw = calls[0]
    assert cmd[:5] == ["omp", "--print", "--mode", "json", "--no-session"]
    for flag in ("--no-extensions", "--no-skills", "--no-context-files", "--offline"):
        assert flag in cmd
    assert cmd[-2:] == ["--", "propose facts"]
    assert kw["env"]["TE_SIDECALL"] == "1" and kw["stdin"] is subprocess.DEVNULL


@pytest.mark.real_sidecalls
def test_the_omp_runner_uses_the_cli_the_person_names(monkeypatch):
    seen = {}

    def fake_call(prompt, **kw):
        seen.update(kw)
        return "ok", 1

    monkeypatch.setattr(sidecall, "omp_call", fake_call)
    cfg = live_config.LiveConfig()
    cfg.set("omp_cli", "~/bin/omp")
    assert sidecall.runner_for("omp", cfg)("prompt") == ("ok", 1)
    assert seen["cli"] == os.path.expanduser("~/bin/omp")


# ---- tool pruning: the catalog and the deny list live in the plugin's state -----------------
@pytest.fixture
def env(tmp_path):
    return {"TOKENECONOMIZER_HOME": str(tmp_path / "home"),
            "TE_MICROSKILLS": "off", "TE_TOOLPRUNE": "off", "TE_LOOPGUARD": "off",
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


def _start(env, repo, sid="omp-1", **kw):
    payload = {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup",
               "cwd": repo, **kw}
    out = io.StringIO()
    hook.main(["--host", "omp"], io.StringIO(json.dumps(payload)), out, env)
    return json.loads(out.getvalue().splitlines()[-1])


def test_a_session_start_catalog_is_merged_into_the_omp_state(env, repo):
    _start(env, repo, tool_catalog=["bash", "read"])
    _start(env, repo, sid="omp-2", tool_catalog=["read", "edit"])
    root = paths.state_root(env)
    assert adapter.load_catalog(root, "omp") == {"bash": "", "edit": "", "read": ""}
    doc = json.load(open(adapter.catalog_path(root, "omp"), encoding="utf-8"))
    assert doc["tools"] == ["bash", "edit", "read"] and doc["at"]


def test_a_session_start_answer_names_the_tools_to_remove(env, repo):
    ctx = LiveContext.build(Omp(), repo, env)
    denylist.apply(repo, ["lsp", "debug"], ctx.store, host="omp")
    assert _start(env, repo)["omp"] == {"denyTools": ["debug", "lsp"]}
    assert "omp" not in _start(dict(env, TE_SHADOW="1"), repo, sid="omp-2")


def test_the_omp_deny_list_is_plugin_state_and_never_a_settings_file(tmp_path):
    st = Store(str(tmp_path / "st"))
    assert denylist.supports("omp")
    assert denylist.apply(tmp_path, ["lsp", "debug"], st, host="omp") == ["debug", "lsp"]
    assert denylist.applied_for(st, tmp_path, "omp") == ["debug", "lsp"]
    assert denylist.apply(tmp_path, ["lsp", "debug"], st, host="omp") == []
    assert denylist.restore(tmp_path, st, ["lsp"], host="omp") == ["lsp"]
    assert denylist.applied_for(st, tmp_path, "omp") == ["debug"]
    denylist.set_kept(st, tmp_path, "omp", add=["debug"])
    assert denylist.kept_for(st, tmp_path, "omp") == ["debug"]
    denylist.set_kept(st, tmp_path, "omp", remove=["debug"])
    assert denylist.kept_for(st, tmp_path, "omp") == []
    assert not list(tmp_path.rglob("settings.local.json"))
    assert not (tmp_path / ".claude").exists() and not (tmp_path / ".codex").exists()


def test_toolprune_apply_and_restore_for_omp_say_where_they_take_effect(
        tmp_path, monkeypatch, capsys):
    r = tmp_path / "git-repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    (r / "pyproject.toml").write_text("[project]\nname='w'\n")
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    assert cli.main(["--host", "omp", "--cwd", str(r), "toolprune", "apply",
                     "lsp", "debug"]) == 0
    out = capsys.readouterr().out
    assert "lsp" in out and "debug" in out and "next omp session" in out
    for needle in (".claude", ".codex", "settings.local"):
        assert needle not in out
    assert cli.main(["--host", "omp", "--cwd", str(r), "toolprune", "refresh-catalog"]) == 0
    assert "SessionStart" in capsys.readouterr().out
    assert cli.main(["--host", "omp", "--cwd", str(r), "toolprune", "measure"]) == 1
    assert "not measured: omp pruning is applied by its extension" in capsys.readouterr().out
    assert cli.main(["--host", "omp", "--cwd", str(r), "toolprune", "restore"]) == 0
    ctx = LiveContext.build(Omp(), str(r))
    assert denylist.applied_for(ctx.store, str(r), "omp") == []


def test_toolprune_show_names_every_section_and_its_verbs_when_nothing_is_recorded(
        tmp_path, monkeypatch, capsys):
    r = tmp_path / "git-repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    (r / "pyproject.toml").write_text("[project]\nname='w'\n")
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    assert cli.main(["--host", "omp", "--cwd", str(r), "toolprune", "show"]) == 0
    out = capsys.readouterr().out
    assert "applied (blocked by this plugin): ->" in out
    assert "\nproposed: ->\n" in out and "\npending: ->\n" in out
    assert "kept (gave back, never re-applied in auto): ->" in out
    for verb in ("apply:", "restore:", "protect:", "apply-pending:"):
        assert verb in out
    assert "unblock" not in out and "ignore" not in out
