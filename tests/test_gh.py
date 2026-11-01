"""Tests for workflow_shas.gh."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from workflow_shas import gh
from workflow_shas.gh import gh_api_json, run_actions_lock, run_cmd, run_gh

from .conftest import cp


def test_run_cmd_success() -> None:
    result = run_cmd(sys.executable, "-c", "print('hi')")
    assert result.stdout.strip() == "hi"


def test_run_cmd_failure_raises_with_stderr() -> None:
    with pytest.raises(RuntimeError, match="Command failed"):
        run_cmd(sys.executable, "-c", "import sys; sys.exit(3)")


def test_run_cmd_failure_without_capture() -> None:
    with pytest.raises(RuntimeError, match="Command failed"):
        run_cmd(sys.executable, "-c", "import sys; sys.exit(1)", capture=False)


def test_run_cmd_check_false_returns_result() -> None:
    result = run_cmd(sys.executable, "-c", "import sys; sys.exit(2)", check=False)
    assert result.returncode == 2


def test_run_cmd_cwd(tmp_path: Path) -> None:
    result = run_cmd(
        sys.executable, "-c", "import os; print(os.getcwd())", cwd=tmp_path
    )
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_run_gh_prefixes_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def fake(*args: str, **kwargs: object) -> object:
        seen.append(args)
        return cp(stdout="out")

    monkeypatch.setattr(gh, "run_cmd", fake)
    assert run_gh("api", "x") == "out"
    assert seen == [("gh", "api", "x")]


def test_gh_api_json_builds_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def fake(*args: str) -> str:
        seen.append(args)
        return '{"id": 5}'

    monkeypatch.setattr(gh, "run_gh", fake)
    assert gh_api_json("repos/o/r", jq=".id") == {"id": 5}
    assert seen == [("api", "repos/o/r", "--jq", ".id")]


def test_run_actions_lock_builds_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake(*args: str, **kwargs: object) -> object:
        seen.append((args, kwargs))
        return cp()

    monkeypatch.setattr(gh, "run_cmd", fake)
    run_actions_lock("--no-fix", cwd=Path("/x"), check=False)
    assert seen == [
        (("gh", "actions-lock", "--no-fix"), {"cwd": Path("/x"), "check": False})
    ]
