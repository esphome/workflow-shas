"""Tests for workflow_shas.commands.lock."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from workflow_shas.commands import lock
from workflow_shas.commands.check import SUB_ISSUE_TITLE
from workflow_shas.commands.lock import (
    BRANCH_NAME,
    PR_TITLE,
    VERIFY_WORKFLOW_PATH,
    VERIFY_WORKFLOW_TEMPLATE,
    commit_changes,
    ensure_repo,
    find_repo_issue,
    get_default_branch,
    get_noncompliant_repos,
    has_changes,
    migrate_repo,
    open_pr,
    pr_body,
    process_repo,
    push_branch,
    render_verify_workflow,
    reset_repo,
    verify_lockfile,
)

from .conftest import GhRouter, cp


class CmdRecorder:
    """Fake ``run_cmd`` recording calls and returning scripted results."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.results: list[tuple[tuple[str, ...], subprocess.CompletedProcess]] = []

    def on(
        self, *prefix: str, result: subprocess.CompletedProcess | None = None
    ) -> CmdRecorder:
        self.results.append((prefix, result or cp()))
        return self

    def __call__(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append((args, kwargs))
        for prefix, result in self.results:
            if args[: len(prefix)] == prefix:
                return result
        return cp()

    def argv(self) -> list[tuple[str, ...]]:
        return [c[0] for c in self.calls]


# --- templates -------------------------------------------------------------


def test_verify_template_content() -> None:
    assert VERIFY_WORKFLOW_TEMPLATE.startswith("name: verify-lockfile\n")
    assert VERIFY_WORKFLOW_TEMPLATE.endswith("run: gh actions-lock --verify\n")
    assert "actions/checkout@v7.0.1" in VERIFY_WORKFLOW_TEMPLATE
    assert "--pin v0.4.0" in VERIFY_WORKFLOW_TEMPLATE
    assert "${{ github.token }}" in VERIFY_WORKFLOW_TEMPLATE
    assert "__DEFAULT_BRANCH__" in VERIFY_WORKFLOW_TEMPLATE
    assert "\u2014" not in VERIFY_WORKFLOW_TEMPLATE


def test_render_verify_workflow_fills_default_branch() -> None:
    rendered = render_verify_workflow("dev")
    assert "__DEFAULT_BRANCH__" not in rendered
    assert "      - dev\n" in rendered
    # The GitHub token expression must survive intact.
    assert "${{ github.token }}" in rendered


def test_pr_body_without_issue() -> None:
    body = pr_body()
    assert "Closes #" not in body
    assert "gh actions-lock" in body
    assert "verify-lockfile.yml" in body
    assert "\u2014" not in body


def test_pr_body_with_issue() -> None:
    assert "Closes #42" in pr_body(42)


# --- local action detection ------------------------------------------------


def write_workflow(work_dir: Path, name: str, content: str) -> None:
    path = work_dir / ".github" / "workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# --- git helpers with a real repo -------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "README").write_text("x")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_has_changes_clean_and_dirty(git_repo: Path) -> None:
    assert has_changes(git_repo) is False
    (git_repo / "new-untracked").write_text("x")
    assert has_changes(git_repo) is True


def test_has_changes_tracked_modification(git_repo: Path) -> None:
    (git_repo / "README").write_text("changed")
    assert has_changes(git_repo) is True


def test_commit_changes_commits_github_dir(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(git_repo, "a.yml", "name: a\n")
    (git_repo / "unrelated").write_text("x")
    commit_changes(git_repo)

    log = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert log.startswith("Adopt gh actions-lock for GitHub Actions pinning")
    assert "\u2014" not in log
    assert "Co-Authored-By" not in log
    assert "Claude" not in log
    files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert files == [".github/workflows/a.yml"]
    assert "Committed changes" in capsys.readouterr().out


def test_commit_changes_nothing_staged(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (git_repo / ".github").mkdir()
    commit_changes(git_repo)
    assert "No staged changes to commit" in capsys.readouterr().out


# --- migrate_repo / verify_lockfile ----------------------------------------


class LockRecorder:
    """Fake ``run_actions_lock`` recording calls."""

    def __init__(self, stdout: str = "") -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.stdout = stdout

    def __call__(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append((args, kwargs))
        return cp(stdout=self.stdout)


def test_migrate_repo_writes_verify_workflow_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rec = LockRecorder()
    monkeypatch.setattr(lock, "run_actions_lock", rec)
    monkeypatch.setattr(lock, "has_changes", lambda wd: True)

    assert migrate_repo(tmp_path, "dev") is True

    written = (tmp_path / VERIFY_WORKFLOW_PATH).read_text()
    assert written == render_verify_workflow("dev")
    assert "      - dev\n" in written
    assert written.endswith("\n")
    expected = (("--no-interactive", "--migrate-local-actions"), {"cwd": tmp_path})
    assert rec.calls == [expected, expected]
    assert f"Added {VERIFY_WORKFLOW_PATH}" in capsys.readouterr().out


def test_migrate_repo_keeps_existing_verify_workflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_workflow(tmp_path, "verify-lockfile.yml", "custom: true\n")
    rec = LockRecorder()
    monkeypatch.setattr(lock, "run_actions_lock", rec)
    monkeypatch.setattr(lock, "has_changes", lambda wd: False)

    assert migrate_repo(tmp_path, "main") is False
    assert (tmp_path / VERIFY_WORKFLOW_PATH).read_text() == "custom: true\n"
    assert len(rec.calls) == 1


def test_migrate_repo_uses_real_change_detection(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    def fake_lock(*args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        (git_repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (git_repo / ".github" / "workflows" / "actions.lock").write_text("v")
        return cp()

    monkeypatch.setattr(lock, "run_actions_lock", fake_lock)
    assert migrate_repo(git_repo, "main") is True


def test_verify_lockfile_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec = LockRecorder(stdout=json.dumps({"valid": True, "findings": []}))
    monkeypatch.setattr(lock, "run_actions_lock", rec)
    assert verify_lockfile(tmp_path) is True
    assert rec.calls == [
        (
            ("--no-fix", "--json", "--no-interactive", "--migrate-local-actions"),
            {"cwd": tmp_path, "check": False},
        )
    ]


@pytest.mark.parametrize(
    "stdout",
    [json.dumps({"valid": False}), "not json", json.dumps(["valid"]), "{}"],
)
def test_verify_lockfile_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str
) -> None:
    monkeypatch.setattr(lock, "run_actions_lock", LockRecorder(stdout=stdout))
    assert verify_lockfile(tmp_path) is False


# --- results loading -------------------------------------------------------


def test_get_noncompliant_repos_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        get_noncompliant_repos(tmp_path / "results.json")
    assert exc.value.code == 1
    assert "Run the check command first" in capsys.readouterr().out


def test_get_noncompliant_repos_filters(tmp_path: Path) -> None:
    path = tmp_path / "results.json"
    path.write_text(
        json.dumps(
            [
                {"name": "a", "compliant": False},
                {"name": "b", "compliant": True},
                {"name": "c", "compliant": None},
                {"name": "d"},
            ]
        )
    )
    assert get_noncompliant_repos(path) == [{"name": "a", "compliant": False}]


# --- ensure_repo / reset_repo ----------------------------------------------


def make_clone(workspace: Path, name: str = "repo") -> Path:
    clone = workspace / name
    (clone / ".git").mkdir(parents=True)
    return clone


def test_get_default_branch(monkeypatch: pytest.MonkeyPatch, router: GhRouter) -> None:
    router.add("api", "repos/o/r", result="main\n")
    monkeypatch.setattr(lock, "run_gh", router)
    assert get_default_branch("o", "r") == "main"


def test_ensure_repo_reuses_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    make_clone(tmp_path)
    worktree = tmp_path / "repo.worktrees" / "actions-lock"
    worktree.mkdir(parents=True)
    rec = CmdRecorder()
    monkeypatch.setattr(lock, "run_cmd", rec)
    assert ensure_repo("o", "repo", tmp_path) == worktree
    assert rec.calls == []


@pytest.mark.parametrize("current_branch", [BRANCH_NAME, "main"])
def test_ensure_repo_creates_worktree(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    tmp_path: Path,
    current_branch: str,
) -> None:
    clone = make_clone(tmp_path)
    router.add("api", result="main\n")
    rec = CmdRecorder().on(
        "git", "branch", "--show-current", result=cp(stdout=current_branch + "\n")
    )
    monkeypatch.setattr(lock, "run_cmd", rec)
    monkeypatch.setattr(lock, "run_gh", router)

    worktree = tmp_path / "repo.worktrees" / "actions-lock"
    assert ensure_repo("o", "repo", tmp_path) == worktree
    assert worktree.parent.is_dir()

    argv = rec.argv()
    assert ("git", "fetch", "origin", "main") in argv
    assert (("git", "checkout", "main") in argv) is (current_branch == BRANCH_NAME)
    assert ("git", "worktree", "prune") in argv
    assert ("git", "branch", "-D", BRANCH_NAME) in argv
    assert (
        "git",
        "worktree",
        "add",
        str(worktree),
        "origin/main",
        "-B",
        BRANCH_NAME,
        "--force",
    ) in argv
    assert (
        "git",
        "remote",
        "set-url",
        "origin",
        "https://github.com/o/repo.git",
    ) in argv
    cwds = {c[1].get("cwd") for c in rec.calls}
    assert cwds == {clone, worktree}


def test_ensure_repo_clones_when_absent(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter, tmp_path: Path
) -> None:
    router.add("api", result="trunk\n")
    rec = CmdRecorder()
    monkeypatch.setattr(lock, "run_cmd", rec)
    monkeypatch.setattr(lock, "run_gh", router)

    assert ensure_repo("o", "repo", tmp_path) == tmp_path / "repo"
    assert rec.argv() == [
        (
            "gh",
            "repo",
            "clone",
            "o/repo",
            str(tmp_path / "repo"),
            "--",
            "--branch",
            "trunk",
            "--single-branch",
        ),
        ("git", "checkout", "-b", BRANCH_NAME),
    ]


def test_reset_repo_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = CmdRecorder()
    monkeypatch.setattr(lock, "run_cmd", rec)
    reset_repo("o", "repo", tmp_path)
    assert rec.calls == []


def test_reset_repo_removes_everything(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_clone(tmp_path)
    worktree = tmp_path / "repo.worktrees" / "actions-lock"
    worktree.mkdir(parents=True)
    router.add("api", result="main\n")
    rec = CmdRecorder().on(
        "git", "branch", "--show-current", result=cp(stdout=BRANCH_NAME + "\n")
    )
    monkeypatch.setattr(lock, "run_cmd", rec)
    monkeypatch.setattr(lock, "run_gh", router)

    reset_repo("o", "repo", tmp_path)

    argv = rec.argv()
    assert ("git", "worktree", "remove", "--force", str(worktree)) in argv
    assert ("git", "checkout", "main") in argv
    assert ("git", "branch", "-D", BRANCH_NAME) in argv
    assert ("git", "push", "origin", "--delete", BRANCH_NAME) in argv
    out = capsys.readouterr().out
    assert "Removed worktree for repo" in out
    assert f"Deleted local branch {BRANCH_NAME}" in out
    assert f"Deleted remote branch {BRANCH_NAME}" in out


def test_reset_repo_branch_commands_fail_quietly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_clone(tmp_path)
    rec = (
        CmdRecorder()
        .on("git", "branch", "--show-current", result=cp(stdout="main\n"))
        .on("git", "branch", "-D", result=cp(1))
        .on("git", "push", result=cp(1))
    )
    monkeypatch.setattr(lock, "run_cmd", rec)
    reset_repo("o", "repo", tmp_path)
    assert ("git", "checkout", "main") not in rec.argv()
    assert capsys.readouterr().out == ""


# --- push / PR --------------------------------------------------------------


def test_push_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec = CmdRecorder()
    monkeypatch.setattr(lock, "run_cmd", rec)
    push_branch(tmp_path)
    assert rec.argv() == [
        ("git", "push", "--set-upstream", "origin", BRANCH_NAME, "--force")
    ]


def test_find_repo_issue_returns_number(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add(
        "issue",
        "list",
        result=json.dumps(
            [{"number": 1, "title": "x"}, {"number": 9, "title": SUB_ISSUE_TITLE}]
        ),
    )
    monkeypatch.setattr(lock, "run_gh", router)
    assert find_repo_issue("o", "r") == 9
    args = router.calls[0]
    assert args[args.index("--search") + 1] == f"in:title {SUB_ISSUE_TITLE}"


def test_find_repo_issue_none_and_error(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("issue", "list", result="[]")
    monkeypatch.setattr(lock, "run_gh", router)
    assert find_repo_issue("o", "r") is None

    router.routes.clear()
    router.add("issue", "list", result=RuntimeError("x"))
    assert find_repo_issue("o", "r") is None


def test_open_pr_creates(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    tmp_path: Path,
) -> None:
    router.add("api", result="main\n")
    router.add("pr", "list", result="[]\n")
    router.add("pr", "create", result="https://github.com/o/r/pull/3\n")
    monkeypatch.setattr(lock, "run_gh", router)

    assert open_pr("o", "r", tmp_path, 12) == "https://github.com/o/r/pull/3"
    create = router.called_with("pr", "create")[0]
    assert create[create.index("--title") + 1] == PR_TITLE
    assert "Closes #12" in create[create.index("--body") + 1]
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--head") + 1] == BRANCH_NAME
    assert create[create.index("--repo") + 1] == "o/r"


@pytest.mark.parametrize("listing", ["", "[]"])
def test_open_pr_empty_listing_creates(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    tmp_path: Path,
    listing: str,
) -> None:
    router.add("api", result="main\n")
    router.add("pr", "list", result=listing)
    router.add("pr", "create", result="https://github.com/o/r/pull/4\n")
    monkeypatch.setattr(lock, "run_gh", router)
    assert open_pr("o", "r", tmp_path) == "https://github.com/o/r/pull/4"


def test_open_pr_updates_existing(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    tmp_path: Path,
) -> None:
    router.add("api", result="main\n")
    router.add(
        "pr", "list", result=json.dumps([{"url": "https://github.com/o/r/pull/8"}])
    )
    router.add("pr", "edit")
    monkeypatch.setattr(lock, "run_gh", router)

    assert open_pr("o", "r", tmp_path, 5) == "https://github.com/o/r/pull/8"
    edit = router.called_with("pr", "edit")[0]
    assert edit[2] == "8"
    assert "Closes #5" in edit[edit.index("--body") + 1]
    assert not router.called_with("pr", "create")


def test_open_pr_nonempty_but_empty_json_list(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    tmp_path: Path,
) -> None:
    router.add("api", result="main\n")
    router.add("pr", "list", result=" [ ] ")
    router.add("pr", "create", result="https://github.com/o/r/pull/6\n")
    monkeypatch.setattr(lock, "run_gh", router)
    assert open_pr("o", "r", tmp_path) == "https://github.com/o/r/pull/6"


# --- process_repo ----------------------------------------------------------


class ProcessHarness:
    """Patch every collaborator of process_repo and record calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.events: list[str] = []
        self.tmp_path = tmp_path
        self.changed = True
        self.valid = True
        self.cmd = CmdRecorder()
        monkeypatch.setattr(lock, "reset_repo", lambda *a: self.events.append("reset"))
        monkeypatch.setattr(lock, "ensure_repo", self._ensure)
        monkeypatch.setattr(lock, "get_default_branch", lambda o, r: "main")
        monkeypatch.setattr(lock, "migrate_repo", lambda wd, db: self.changed)
        monkeypatch.setattr(lock, "verify_lockfile", self._verify)
        monkeypatch.setattr(lock, "run_cmd", self.cmd)
        monkeypatch.setattr(
            lock, "commit_changes", lambda wd: self.events.append("commit")
        )
        monkeypatch.setattr(lock, "push_branch", lambda wd: self.events.append("push"))
        monkeypatch.setattr(lock, "find_repo_issue", lambda o, r: 77)
        monkeypatch.setattr(lock, "open_pr", self._open_pr)

    def _ensure(self, org: str, name: str, ws: Path) -> Path:
        self.events.append("ensure")
        return self.tmp_path

    def _verify(self, wd: Path) -> bool:
        self.events.append("verify")
        return self.valid

    def _open_pr(self, org: str, name: str, wd: Path, issue: int | None = None) -> str:
        self.events.append(f"pr:{issue}")
        return "https://github.com/o/r/pull/1"

    def run(self, **overrides: bool) -> dict:
        kwargs = {"dry_run": False, "no_push": False, "no_pr": False, "reset": False}
        kwargs.update(overrides)
        return process_repo("o", {"name": "r"}, self.tmp_path, **kwargs)


def test_process_repo_no_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = ProcessHarness(monkeypatch, tmp_path)
    h.changed = False
    assert h.run() == {"name": "r", "status": "no_changes", "pr_url": None}
    assert h.events == ["ensure"]


def test_process_repo_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    h = ProcessHarness(monkeypatch, tmp_path)
    h.cmd.on("git", "diff", "--stat", result=cp(stdout=" 1 file changed\n"))
    h.cmd.on("git", "status", "--short", result=cp(stdout="?? new\n"))
    assert h.run(dry_run=True)["status"] == "dry_run"
    out = capsys.readouterr().out
    assert "1 file changed" in out and "?? new" in out
    assert h.events == ["ensure"]


def test_process_repo_reset_and_commit_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = ProcessHarness(monkeypatch, tmp_path)
    assert h.run(reset=True, no_push=True)["status"] == "committed"
    assert h.events == ["reset", "ensure", "verify", "commit"]


def test_process_repo_warns_when_lockfile_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    h = ProcessHarness(monkeypatch, tmp_path)
    h.valid = False
    assert h.run(no_push=True)["status"] == "committed"
    assert "WARNING: gh actions-lock reports the lockfile is not valid" in (
        capsys.readouterr().out
    )


def test_process_repo_pushed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    h = ProcessHarness(monkeypatch, tmp_path)
    assert h.run(no_pr=True)["status"] == "pushed"
    assert h.events == ["ensure", "verify", "commit", "push"]


def test_process_repo_pr_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = ProcessHarness(monkeypatch, tmp_path)
    assert h.run() == {
        "name": "r",
        "status": "pr_opened",
        "pr_url": "https://github.com/o/r/pull/1",
    }
    assert h.events == ["ensure", "verify", "commit", "push", "pr:77"]


def test_process_repo_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    h = ProcessHarness(monkeypatch, tmp_path)

    def boom(*args: Any) -> Path:
        raise RuntimeError("clone failed")

    monkeypatch.setattr(lock, "ensure_repo", boom)
    summary = h.run()
    assert summary["status"] == "error: clone failed"
    assert summary["pr_url"] is None


# --- run() -----------------------------------------------------------------


@pytest.fixture
def results_file(tmp_path: Path) -> Path:
    path = tmp_path / "results.json"
    path.write_text(
        json.dumps(
            [
                {"name": "b", "compliant": False, "external_workflows": ["x", "y"]},
                {"name": "a", "compliant": False},
                {"name": "ok", "compliant": True},
            ]
        )
    )
    return path


class RunHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(lock, "process_repo", self._process)

    def _process(self, org: str, info: dict, ws: Path, **kwargs: Any) -> dict:
        self.calls.append((info["name"], kwargs))
        return {
            "name": info["name"],
            "status": "pr_opened" if info["name"] == "a" else "no_changes",
            "pr_url": "https://x/pull/1" if info["name"] == "a" else None,
        }


@pytest.mark.parametrize(
    ("flags", "mode", "expected"),
    [
        (
            {"dry_run": True},
            "Mode: DRY RUN",
            {"dry_run": True, "no_push": True, "no_pr": True},
        ),
        (
            {"no_push": True},
            "Mode: COMMIT ONLY",
            {"dry_run": False, "no_push": True, "no_pr": True},
        ),
        (
            {"no_pr": True},
            "Mode: COMMIT + PUSH",
            {"dry_run": False, "no_push": False, "no_pr": True},
        ),
        (
            {},
            "Mode: FULL",
            {"dry_run": False, "no_push": False, "no_pr": False},
        ),
    ],
)
def test_run_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    results_file: Path,
    capsys: pytest.CaptureFixture[str],
    flags: dict[str, bool],
    mode: str,
    expected: dict[str, bool],
) -> None:
    h = RunHarness(monkeypatch)
    lock.run("o", tmp_path, results_file, **flags)
    out = capsys.readouterr().out
    assert mode in out
    assert "Found 2 repos that have not adopted gh actions-lock" in out
    assert "[1/2] a (0 workflows with external actions)" in out
    assert "[2/2] b (2 workflows with external actions)" in out
    assert "a: pr_opened -> https://x/pull/1" in out
    assert "b: no_changes" in out
    assert [name for name, _ in h.calls] == ["a", "b"]
    for _, kwargs in h.calls:
        assert {k: kwargs[k] for k in expected} == expected
        assert kwargs["reset"] is False


def test_run_reset_and_repo_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    results_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    h = RunHarness(monkeypatch)
    lock.run("o", tmp_path, results_file, repos=["b"], reset=True)
    assert "Reset: YES" in capsys.readouterr().out
    assert [name for name, _ in h.calls] == ["b"]
    assert h.calls[0][1]["reset"] is True


def test_run_unknown_repo_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    results_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    RunHarness(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        lock.run("o", tmp_path, results_file, repos=["ok", "zzz"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "repos not found in the not-adopted list: ok, zzz" in out
