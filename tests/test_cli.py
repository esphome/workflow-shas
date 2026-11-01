"""Tests for workflow_shas.cli."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from workflow_shas import cli


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    recorded: dict[str, dict[str, Any]] = {}

    def make(name: str) -> Any:
        def fake(*args: Any, **kwargs: Any) -> None:
            recorded[name] = {"args": args, **kwargs}

        return fake

    monkeypatch.setattr(cli.check, "run", make("check"))
    monkeypatch.setattr(cli.lock, "run", make("lock"))
    monkeypatch.setattr(cli.enforce, "run", make("enforce"))
    return recorded


def test_check_dispatch(calls: dict[str, dict[str, Any]]) -> None:
    cli.main(
        ["--org", "o", "check", "--tracking-repo", "t", "--repo", "a", "--repo", "b"]
    )
    assert calls["check"] == {
        "args": (),
        "org": "o",
        "tracking_repo": "t",
        "repos": ["a", "b"],
        "dry_run": False,
    }


def test_check_dry_run_without_tracking_repo(calls: dict[str, dict[str, Any]]) -> None:
    cli.main(["--org", "o", "check", "--dry-run"])
    assert calls["check"]["tracking_repo"] == ""
    assert calls["check"]["dry_run"] is True
    assert calls["check"]["repos"] is None


def test_check_requires_tracking_repo(
    calls: dict[str, dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--org", "o", "check"])
    assert exc.value.code == 2
    assert "--tracking-repo is required" in capsys.readouterr().err
    assert "check" not in calls


def test_lock_dispatch(calls: dict[str, dict[str, Any]], tmp_path: Path) -> None:
    results = tmp_path / "r.json"
    cli.main(
        [
            "--org",
            "o",
            "lock",
            "--workspace",
            str(tmp_path),
            "--results-file",
            str(results),
            "--repo",
            "a",
            "--dry-run",
            "--no-push",
            "--no-pr",
            "--reset",
        ]
    )
    assert calls["lock"] == {
        "args": (),
        "org": "o",
        "workspace": tmp_path,
        "results_file": results,
        "repos": ["a"],
        "dry_run": True,
        "no_push": True,
        "no_pr": True,
        "reset": True,
    }


def test_lock_defaults(calls: dict[str, dict[str, Any]]) -> None:
    cli.main(["--org", "o", "lock"])
    assert calls["lock"]["workspace"] == Path.cwd()
    assert calls["lock"]["results_file"] == Path.cwd() / "results.json"
    assert calls["lock"]["dry_run"] is False


def test_pin_command_removed(calls: dict[str, dict[str, Any]]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--org", "o", "pin"])


def test_enforce_dispatch(calls: dict[str, dict[str, Any]]) -> None:
    cli.main(["--org", "o", "enforce", "--dry-run", "--repo", "x"])
    assert calls["enforce"] == {
        "args": (),
        "org": "o",
        "repos": ["x"],
        "dry_run": True,
    }
