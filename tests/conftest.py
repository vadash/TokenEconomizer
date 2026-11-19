"""pytest fixtures shared across the offline suite."""
import os
import shutil
import sys

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_TESTS)
for p in (_REPO, _TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)


def git_sh():
    """Git Bash's `sh`: on PATH, or beside the `git` that is. Git for Windows puts only
    `<Git>\\cmd` on the PATH of PowerShell and cmd.exe; its `<Git>\\bin\\sh.exe` adds the
    `usr\\bin` tools the launchers use (`tr`, ...), as the `bash.exe` Claude Code runs hooks with."""
    found = shutil.which("sh")
    git = shutil.which("git")
    if found or not git:
        return found
    sh = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(git))), "bin", "sh.exe")
    return sh if os.path.isfile(sh) else None


def symlink_or_skip(src, dst):
    """`os.symlink`, or skip where this system makes none (Windows without Developer Mode)."""
    try:
        os.symlink(src, dst)
    except OSError as e:
        pytest.skip(f"this system makes no symlinks: {e}")


def pytest_configure(config):
    config.addinivalue_line("markers", "real_sidecalls: test the side-call functions themselves "
                                       "(their subprocess is faked inside the test)")
    config.addinivalue_line("markers", "powershell: launches native Windows PowerShell; CI runs "
                                       "these on one Python of the Windows matrix (tests.yml)")


@pytest.fixture(autouse=True)
def _no_own_home(tmp_path_factory, monkeypatch):
    """No test reads the person's own home directory."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture(autouse=True)
def _no_model_calls(request, monkeypatch):
    """No test reaches a real agent CLI. With the loop guard on, a stuck-looking turn wakes its
    judge: the plugin's side calls answer nothing here, and a judge job runs inline instead of
    as a detached process. A test that wants a verdict fakes the judge (`stuck_judge`)."""
    from tokeneconomizer.plugin import judge, sidecall
    monkeypatch.setattr(judge, "spawn_job", judge.run_job)
    if request.node.get_closest_marker("real_sidecalls"):
        return
    monkeypatch.setattr(sidecall, "omp_call", lambda *a, **k: ("", None))
