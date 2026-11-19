"""A long, productive turn is not a loop (#128): what the loop guard reads as a failure, an edit
and a verification, on the commands that report's turn ran."""
import pytest

from tokeneconomizer.loopguard.shell_edits import shell_edit_targets, shell_revert_targets
from tokeneconomizer.loopguard.turn import (TurnGuard, exit_status_covers, runner_verdict,
                                            status_of_tests)
from tokeneconomizer.plugin import hosts

# Printed by Playwright on Windows as it exits, after a passing run as after a failing one.
LIBUV = r"Assertion failed: !(handle->flags & UV_HANDLE_CLOSING), file src\win\async.c, line 76"


@pytest.mark.parametrize("cmd, covers", [
    ("npx playwright test spec.ts", True),
    ("cd apps/web && npx vitest run a.test.ts", True),
    ("cd apps/web; npx vitest run a.test.ts", True),
    ("cat apps/web/booking.ts", True),
    ("grep -rn BookingRequestError apps | head -20", True),
    ("git show HEAD:apps/web/booking.ts | sed -n '1,80p'", True),
    ("set -o pipefail; python repro.py | tail -5", True),
    ("python repro.py 2>&1 | tail -20", False),
    ("npm test; echo \"exit=$?\"", False),
    ("sed -i 's/a/b/' x.ts; npm test", False),
    ("make || true", False),
    ("SP=/tmp/x; cd $SP && python3 - <<'EOF'\nimport json\nprint('error outputs: 3')\nEOF",
     True),
    ("python3 - <<'EOF'\nprint(1)\nEOF\npython -m pytest -q | tail -3", False),
    ("python repro.py <<< y", True),
    ("python repro.py <<< \"y\"\npython -m pytest -q | tail -3", False),
    ("", False),
])
def test_an_exit_status_covers_a_command_unless_its_work_could_fail_unseen(cmd, covers):
    assert exit_status_covers(cmd) is covers


def _omp_result(cmd, stdout, exit_code=0, **response):
    return {"hook_event_name": "PostToolUse", "tool_name": "bash",
            "tool_input": {"command": cmd},
            "tool_response": dict({"content": stdout, "isError": False,
                                   "exit_code": exit_code}, **response)}


def test_an_omp_shell_result_carries_the_reported_exit_code():
    h = hosts.Omp()
    assert h.tool_call(_omp_result("npx playwright test spec.ts", LIBUV))[3] == 0
    # a pipe's status is its last command's; the guard reads the failure from the output
    assert h.tool_call(_omp_result("python repro.py 2>&1 | tail", "Traceback"))[3] == 0
    assert h.tool_call(_omp_result("npm test", "", isError=True,
                                   exit_code=None))[3] is None
    assert h.tool_call({"hook_event_name": "PostToolUse", "tool_name": "read",
                        "tool_input": {"file_path": "/r/a.ts"},
                        "tool_response": "x"})[3] is None


def test_green_playwright_runs_are_not_failures_whatever_libuv_prints():
    g = TurnGuard()
    for n in (7, 5):
        out = f"{LIBUV}\n  {n} passed (23.6s)"
        g.fold("Edit", {"file_path": "/r/apps/web/a.ts"}, "ok")
        g.fold("Bash", {"command": "npx playwright test spec.ts"}, out, 0)
        # Piped, the exit status is `tail`'s, and Playwright's own summary decides.
        g.fold("Bash", {"command": "npx playwright test spec.ts 2>&1 | tail -5"}, out, None)
    assert g.whole.error_outputs == 0 and g.whole.repeated_fail_sigs == 0
    assert g.resolved_proxy() and g.whole.test_runs == 4
    assert runner_verdict("  1 failed\n    [chromium] a.spec.ts:3:1\n  6 passed (20.1s)") is True

def test_source_read_through_the_shell_is_not_an_error_output_and_a_piped_crash_still_is():
    g = TurnGuard()
    src = '      if (!service) throw new BookingRequestError("unavailable", "Service not found");'
    for cmd in ("cat apps/web/booking.ts", "sed -n '1,80p' apps/web/booking.ts",
                "grep -rn BookingRequestError apps | head -20"):
        g.fold("bash", {"command": cmd}, src,
               0 if exit_status_covers(cmd) else None)
    assert g.whole.error_outputs == 0
    cmd = "python repro.py 2>&1 | tail -3"
    g.fold("bash", {"command": cmd}, "Traceback (most recent call last):\nValueError: x",
           0 if exit_status_covers(cmd) else None)
    assert g.whole.error_outputs == 1


def test_putting_back_a_file_the_turn_never_edited_is_not_an_edit():
    revert = "git checkout -q -- apps/web/AGENTS.md"
    assert shell_edit_targets(revert) == shell_revert_targets(revert) == ["AGENTS.md"]
    g = TurnGuard()
    for _ in range(4):                    # `next dev` keeps rewriting it; the agent puts it back
        g.fold("Bash", {"command": revert}, "", 0)
    assert g.whole.edit_churn == 0 and g.edits == 0
    g.fold("Edit", {"file_path": "/r/apps/web/page.tsx"}, "ok")
    g.fold("Bash", {"command": "git restore apps/web/page.tsx"}, "", 0)
    assert g.whole.edit_counts["page.tsx"] == 2 and g.edits == 2


def test_a_vitest_run_behind_dotenv_is_a_verification_and_a_test_run():
    cmd = "dotenv -e .env.local -- npx vitest run a.test.ts"
    assert status_of_tests(cmd) == "own"
    g = TurnGuard()
    g.fold("Bash", {"command": cmd}, " Tests  3 passed (3)", 0)
    g.fold("Bash", {"command": "npx vitest run b.test.ts"}, " Tests  2 passed (2)", 0)
    assert g.verifications == 2 and g.st.test_runs == 2


def test_successful_omp_edits_of_files_containing_assert_or_failed_are_not_errors():
    h = hosts.Omp()
    edit = {"hook_event_name": "PostToolUse", "tool_name": "edit",
            "tool_input": {"file_path": "/r/README.md"},
            "tool_response": {"isError": False, "content": "the failed case asserts the value",
                              "structuredPatch": [{"lines": ["+the failed case asserts the value"]}]}}
    write = {"hook_event_name": "PostToolUse", "tool_name": "write",
             "tool_input": {"file_path": "/r/test_a.py"},
             "tool_response": {"isError": False,
                               "content": "def test_x():\n    assert False, 'failed'\n",
                               "structuredPatch": []}}
    g = TurnGuard()
    for p in (edit, write, edit):
        name, args, text, code = h.tool_call(p)
        assert text == ""
        g.fold(name, args, text, code)
    assert g.whole.error_outputs == 0 and g.whole.repeated_fail_sigs == 0
    failure = {"hook_event_name": "PostToolUse", "tool_name": "edit",
               "tool_input": {"file_path": "/r/README.md"},
               "tool_response": {"isError": True,
                                 "error": "Error: String to replace not found in file."}}
    name, args, text, code = h.tool_call(failure)
    g.fold(name, args, text, code)
    assert g.whole.error_outputs == 1
