"""`te repo-key`: the repository key for a directory, alone on stdout."""
import os
import re

from tokeneconomizer.plugin import cli, paths

REMOTE = "git@github.com:Acme/Rocket.git"
KEY = "rocket-09f97b1cd2"


def make_env(tmp_path, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return dict({"TOKENECONOMIZER_HOME": str(tmp_path / "te"), "HOME": str(home)}, **extra)


def git_repo(root, remote):
    os.makedirs(os.path.join(root, ".git"), exist_ok=True)
    with open(os.path.join(root, ".git", "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\n")
    with open(os.path.join(root, ".git", "config"), "w") as fh:
        fh.write("[core]\n\trepositoryformatversion = 0\n"
                 + (f'[remote "origin"]\n\turl = {remote}\n' if remote else ""))
    return root


def link_worktree(clone, name):
    wt_gitdir = os.path.join(clone, ".git", "worktrees", name)
    os.makedirs(wt_gitdir, exist_ok=True)
    with open(os.path.join(wt_gitdir, "commondir"), "w") as fh:
        fh.write("../..\n")
    wt = clone + "-" + name
    os.makedirs(wt, exist_ok=True)
    with open(os.path.join(wt, ".git"), "w") as fh:
        fh.write(f"gitdir: {wt_gitdir}\n")
    return wt


def test_remote_clone_key_is_a_fixed_literal(tmp_path):
    env = make_env(tmp_path)
    assert re.fullmatch(r"[a-z0-9-]{1,40}-[0-9a-f]{10}", KEY)
    first = git_repo(str(tmp_path / "one"), REMOTE)
    second = git_repo(str(tmp_path / "two"), REMOTE)
    for clone in (first, second):
        ident = paths.repo_identity(clone, env)
        assert (ident.key, ident.how) == (KEY, paths.BY_REMOTE)


def test_linked_worktree_shares_its_clone_key(tmp_path):
    env = make_env(tmp_path)
    clone = git_repo(str(tmp_path / "clone"), REMOTE)
    assert paths.repo_identity(link_worktree(clone, "wt1"), env).key == KEY


def test_named_export_shares_its_clone_key(tmp_path):
    clone = git_repo(str(tmp_path / "clone"), REMOTE)
    env = make_env(tmp_path, TOKENECONOMIZER_REPO=REMOTE)
    export = tmp_path / "runs" / "job-1"
    export.mkdir(parents=True)
    ident = paths.repo_identity(str(export), env)
    assert ident.key == paths.repo_identity(clone, make_env(tmp_path)).key == KEY
    assert (ident.how, ident.root) == (paths.BY_REMOTE, str(export))


def test_checkout_without_remote_keys_by_real_path(tmp_path):
    env = make_env(tmp_path)
    first = git_repo(str(tmp_path / "fresh-a"), None)
    second = git_repo(str(tmp_path / "fresh-b"), None)
    a, b = paths.repo_identity(first, env), paths.repo_identity(second, env)
    assert a.how == b.how == paths.BY_PATH
    assert a.key != b.key
    assert paths.repo_identity(first, env).key == a.key


def test_dir_without_checkout_keys_by_dir(tmp_path):
    env = make_env(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    ident = paths.repo_identity(str(plain), env)
    assert ident.how == paths.BY_DIR
    assert paths.repo_identity(str(plain), env).key == ident.key
    assert paths.repo_identity(str(other), env).key != ident.key


def test_cli_prints_only_the_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "te"))
    clone = git_repo(str(tmp_path / "clone"), REMOTE)
    assert cli.main(["--cwd", clone, "repo-key"]) == 0
    out, err = capsys.readouterr()
    assert out == KEY + "\n"
    assert err == ""
