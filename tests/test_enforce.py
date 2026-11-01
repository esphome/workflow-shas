"""Tests for workflow_shas.commands.enforce."""

from __future__ import annotations

from typing import Any

import pytest

from workflow_shas.commands import enforce
from workflow_shas.commands.enforce import enable_sha_pinning

from .conftest import GhRouter, make_repo_gh
from .test_check import make_result

EXTERNAL_WF = "steps:\n  - uses: actions/checkout@v4\n"


def test_enable_sha_pinning_builds_request(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("api")
    monkeypatch.setattr(enforce, "run_gh", router)
    enable_sha_pinning("o", "r")
    assert router.calls == [
        (
            "api",
            "--method",
            "PUT",
            "repos/o/r/actions/permissions",
            "-F",
            "enabled=true",
            "-f",
            "allowed_actions=all",
            "-F",
            "sha_pinning_required=true",
        )
    ]


def patch_results(
    monkeypatch: pytest.MonkeyPatch, results: dict[str, dict[str, Any]]
) -> list[str]:
    enabled: list[str] = []
    monkeypatch.setattr(enforce, "check_repo", lambda org, name: results[name])
    monkeypatch.setattr(
        enforce, "enable_sha_pinning", lambda org, name: enabled.append(name)
    )
    return enabled


def test_run_classifies_repos(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = {
        "enforced": make_result("enforced", sha_pinning_required=True),
        "nowf": make_result(
            "nowf", has_workflows=False, compliant=None, sha_pinning_required=False
        ),
        "noext": make_result("noext", compliant=None, sha_pinning_required=False),
        "adopted": make_result("adopted", compliant=True, sha_pinning_required=None),
        "todo": make_result("todo", compliant=False, sha_pinning_required=False),
    }
    enabled = patch_results(monkeypatch, results)
    monkeypatch.setattr(
        enforce, "get_repos", lambda org: [{"name": n} for n in results]
    )

    enforce.run("org")
    out = capsys.readouterr().out

    assert "Found 5 active repositories in org" in out
    assert "already enforced" in out
    assert "no workflows -> safe" in out
    assert "no external actions -> safe" in out
    assert "lockfile adopted -> safe" in out
    assert "not yet adopted -> skip" in out
    assert "3 repos safe to enforce" in out
    assert sorted(enabled) == ["adopted", "noext", "nowf"]
    assert "Enabled enforcement on 3/3 repos." in out


def test_run_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = {"a": make_result("a", sha_pinning_required=False)}
    enabled = patch_results(monkeypatch, results)
    enforce.run("org", repos=["a"], dry_run=True)
    out = capsys.readouterr().out
    assert "Checking 1 specified repos" in out
    assert "[dry-run] a (lockfile adopted)" in out
    assert "[dry-run] Would enable enforcement on 1 repos." in out
    assert enabled == []


def test_run_nothing_to_enforce(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = {"a": make_result("a", compliant=False, sha_pinning_required=False)}
    patch_results(monkeypatch, results)
    enforce.run("org", repos=["a"])
    assert "No repos need enforcement enabled." in capsys.readouterr().out


def test_run_counts_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = {
        "good": make_result("good", sha_pinning_required=False),
        "bad": make_result("bad", sha_pinning_required=False),
    }
    patch_results(monkeypatch, results)

    def flaky(org: str, name: str) -> None:
        if name == "bad":
            raise RuntimeError("denied")

    monkeypatch.setattr(enforce, "enable_sha_pinning", flaky)
    enforce.run("org", repos=["good", "bad"])
    out = capsys.readouterr().out
    assert "bad (lockfile adopted) -- ERROR: denied" in out
    assert "good (lockfile adopted) -- enabled" in out
    assert "Enabled enforcement on 1/2 repos." in out


def test_run_end_to_end_through_check_repo(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Drive the real check_repo and enable_sha_pinning via a fake gh."""
    from workflow_shas import scan

    repo_gh = make_repo_gh(workflows={"ci.yml": EXTERNAL_WF})
    router.add("api", "--method", "PUT")
    monkeypatch.setattr(scan, "run_gh", repo_gh)
    monkeypatch.setattr(enforce, "run_gh", router)

    enforce.run("org", repos=["r"])
    out = capsys.readouterr().out
    assert "not yet adopted -> skip" in out
    assert not router.calls
