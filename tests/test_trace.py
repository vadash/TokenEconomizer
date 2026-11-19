"""Trace seam: load_trajectory / tool_usage / call_cwds over omp session files."""
import json

import pytest

from tokeneconomizer import trace
from tests.test_live_core import _omp_session


def test_load_trajectory_reads_the_active_branch(tmp_path):
    src = _omp_session(str(tmp_path / "s.jsonl"), abandoned=True)
    traj = trace.load_trajectory(src)
    assert [(n, a.get("command") or a.get("path"), ok) for n, a, _, ok in traj] == [
        ("bash", "pytest -q", False), ("edit", "pkg/a.py", True),
        ("bash", "python -m pytest -q", True)]
    assert "rm -rf build" not in " ".join(r for _, _, r, _ in traj)


def test_tool_usage_counts(tmp_path):
    assert trace.tool_usage(_omp_session(str(tmp_path / "s.jsonl"))) == {"bash": 2, "edit": 1}


def test_call_cwds_names_the_session_directory(tmp_path):
    assert trace.call_cwds(_omp_session(str(tmp_path / "s.jsonl"))) == ["/w/repo"] * 3


def test_strip_rtk_prefix_unwraps_the_command_wrapper():
    assert trace.strip_rtk_prefix("rtk pytest -q") == "pytest -q"
    assert trace.strip_rtk_prefix("rtk proxy git status") == "git status"
    assert trace.strip_rtk_prefix("rtk") == "rtk"
    assert trace.strip_rtk_prefix("pytest -q") == "pytest -q"


def test_missing_files_read_as_no_calls(tmp_path):
    missing = str(tmp_path / "gone.jsonl")
    assert trace.load_trajectory(missing) == []
    assert trace.tool_usage(missing) == {}
    assert trace.call_cwds(missing) == []
    assert trace.omp_write_session(missing, str(tmp_path / "out.jsonl")) is None

def test_omp_prompt_count_counts_user_messages_on_the_active_branch(tmp_path):
    src = _omp_session(str(tmp_path / "s.jsonl"), abandoned=True)
    assert trace.omp_prompt_count(src) == 1
    lines = [json.loads(line) for line in open(src, encoding="utf-8")]
    lines.insert(3, {"type": "message", "id": "zzz", "parentId": lines[1]["id"],
                     "timestamp": "2026-09-30T00:00:02Z",
                     "message": {"role": "user", "content": "a prompt on a branch left behind"}})
    open(src, "w", encoding="utf-8").write("\n".join(json.dumps(e) for e in lines) + "\n")
    assert trace.omp_prompt_count(src) == 1


def test_omp_session_events_stamp_the_session_directory_on_each_call(tmp_path):
    events = trace.omp_session_events(_omp_session(str(tmp_path / "s.jsonl")))
    assistants = [e for e in events if e.get("type") == "assistant"]
    assert len(assistants) == 3
    assert all(e.get("cwd") == "/w/repo" for e in assistants)


def test_omp_write_session_converts_a_session_for_the_mechanisms(tmp_path):
    src = _omp_session(str(tmp_path / "s.jsonl"), abandoned=True)
    out = str(tmp_path / "trace.jsonl")
    assert trace.omp_write_session(src, out) == 3
    traj = trace.load_trajectory(out)
    assert [n for n, _, _, _ in traj] == ["bash", "edit", "bash"]
    assert "rm -rf build" not in open(out, encoding="utf-8").read()


def test_omp_session_usage_maps_directly_and_keeps_its_own_cost(tmp_path):
    usage = trace.omp_session_usage(_omp_session(str(tmp_path / "s.jsonl"), abandoned=True))
    assert usage["requests"] == 3
    assert (usage["input_tokens"], usage["cache_read_input_tokens"],
            usage["cache_creation_input_tokens"], usage["output_tokens"]) == (30, 3000, 300, 150)
    assert usage["cost_usd_vendor"] == pytest.approx(0.006)
    assert usage["model"] == "gpt-5.6-sol"
    assert trace.omp_session_header(str(tmp_path / "s.jsonl"))["cwd"] == "/w/repo"


def test_omp_json_reply_reads_the_last_answer_and_sums_usage():
    def end(text, total):
        return {"type": "message_end", "message": {
            "role": "assistant", "model": "m", "content": [{"type": "text", "text": text}],
            "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0,
                      "cost": {"total": total}}}}
    stdout = "\n".join(json.dumps(e) for e in [end("first", 0.001), end("second", 0.002),
                                               {"type": "other"}])
    text, usage = trace.omp_json_reply(stdout + "\nnot json\n")
    assert text == "second"
    assert (usage["input_tokens"], usage["output_tokens"]) == (20, 10)
    assert usage["cost_usd"] == pytest.approx(0.003)


# Valid JSON that is not an object. `records` yields only objects, so a stray line never
# makes a reader call `.get` on an array or a number.
NON_OBJECT_LINES = [
    "null", "42", '"text"',
    '["usage", "session_meta", "turn_context", "token_count", "response_item", '
    '"session.start", "type", "system"]']


def test_a_non_object_line_does_not_stop_an_omp_session_from_being_read(tmp_path):
    src = _omp_session(str(tmp_path / "s.jsonl"))
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    junk = "\n".join(NON_OBJECT_LINES + ["{not json", ""]) + "\n"
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(junk + body + junk)
    assert [n for n, _, _, _ in trace.load_trajectory(src)] == ["bash", "edit", "bash"]
    assert trace.omp_prompt_count(src) == 1
    assert trace.omp_session_usage(src)["requests"] == 3


# ---- Web-Bench pass@1 vs pass@N ------------------------------------------------------
#
# Web-Bench grades each task with up to `retry` attempts, so a task has TWO defensible
# resolve metrics, and they differ materially (measured on prisma: 1-4 tasks per arm).
# Web-Bench's own headline is pass@1. The runner breaks out of the attempt loop on the
# first pass, which makes pass@1 recoverable from older rows that predate the explicit
# fields -- these pin that derivation, since getting it wrong reports pass@N under the
# pass@1 column.
def _p1p2(row):
    """The aggregator's per-row pass@1/pass@N resolution, mirrored for test."""
    p1, p2 = row.get("resolved_p1", ""), row.get("resolved_p2", "")
    att = row.get("webbench_attempts", "")
    if p1 == "" and p2 == "":
        if att != "":
            att = int(att or 0)
            if att == 0:
                return None, None
            p2 = row["resolved"] is True
            p1 = p2 and att == 1
        else:
            p1 = p2 = row["resolved"]
    return p1, p2


def test_explicit_attempt_fields_win():
    assert _p1p2({"resolved": True, "resolved_p1": False,
                  "resolved_p2": True, "webbench_attempts": 2}) == (False, True)


def test_legacy_row_second_attempt_pass_is_not_pass_at_1():
    """resolved=True after 2 attempts means attempt 1 FAILED."""
    assert _p1p2({"resolved": True, "webbench_attempts": 2}) == (False, True)


def test_legacy_row_first_attempt_pass():
    assert _p1p2({"resolved": True, "webbench_attempts": 1}) == (True, True)


def test_legacy_row_failed_every_attempt():
    assert _p1p2({"resolved": False, "webbench_attempts": 2}) == (False, False)


def test_unreached_task_counts_in_neither_metric():
    """Never attempted is not a miss -- it must not enter numerator or denominator."""
    assert _p1p2({"resolved": False, "webbench_attempts": 0}) == (None, None)


def test_single_attempt_domain_collapses_to_resolved():
    """SWE-bench rows carry no attempt fields; pass@1 and pass@N are the same number."""
    assert _p1p2({"resolved": True}) == (True, True)
    assert _p1p2({"resolved": False}) == (False, False)
