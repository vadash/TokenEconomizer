"""oh-my-pi (`omp`) as a host: the turn identity and tool result its adapter sends, its headless
modes, its session log read through the Pi trace, the shell it marks, and its side call."""
import json
import os
import subprocess

import pytest

from tokeneconomizer.plugin import config as live_config, hosts, sidecall
from tokeneconomizer.plugin.hosts import Omp

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
