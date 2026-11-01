"""Tests for workflow_shas.scan."""

from __future__ import annotations

import base64
import json

import pytest

from workflow_shas import scan
from workflow_shas.scan import (
    analyze_workflow,
    check_repo,
    get_action_files,
    get_file_content,
    get_lockfile,
    get_repos,
    get_sha_pinning_required,
    get_workflow_files,
    parse_lockfile,
)

from .conftest import GhRouter, make_repo_gh

SHA = "a" * 40

EXTERNAL_WF = """\
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
"""

NO_EXTERNAL_WF = """\
jobs:
  build:
    steps:
      - uses: ./local
      - uses: docker://alpine:3
      - run: echo hi
"""

VERIFY_WF = """\
jobs:
  verify:
    steps:
      - uses: actions/checkout@v7.0.1
"""

LOCKFILE = """\
version: 'v0.0.3'
workflows:
    '.github/workflows/ci.yml':
        - 'actions/checkout@v4'
    '.github/workflows/verify-lockfile.yml':
        - 'actions/checkout@v7.0.1'
dependencies: {}
"""


def test_analyze_workflow_classifies_refs() -> None:
    content = f"""\
steps:
  - uses: actions/checkout@{SHA}  # v4.2.2
  - uses: actions/setup-python@v5
  - uses: ./local@v1
  - uses: docker://alpine@sha256:abc
  - uses: org/repo/.github/workflows/x.yml@main
"""
    result = analyze_workflow(content)
    assert result["sha_pinned"] == [f"actions/checkout@{SHA}"]
    assert result["not_pinned"] == [
        "actions/setup-python@v5",
        "org/repo/.github/workflows/x.yml@main",
    ]


def test_analyze_workflow_empty() -> None:
    assert analyze_workflow("name: x\n") == {"sha_pinned": [], "not_pinned": []}


def test_analyze_workflow_strips_whitespace_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pattern stops at whitespace, but guard against a space in the ref."""

    class FakeMatch:
        def group(self, n: int) -> str:
            return {1: "a/b", 2: "v1 # comment"}[n]

    class FakePattern:
        def finditer(self, content: str) -> list[FakeMatch]:
            return [FakeMatch()]

    monkeypatch.setattr(scan, "USES_PATTERN", FakePattern())
    assert analyze_workflow("x")["not_pinned"] == ["a/b@v1"]


def test_parse_lockfile_valid() -> None:
    assert parse_lockfile(LOCKFILE) == {
        ".github/workflows/ci.yml": {"actions/checkout@v4"},
        ".github/workflows/verify-lockfile.yml": {"actions/checkout@v7.0.1"},
    }


@pytest.mark.parametrize(
    "content",
    [
        "",
        "just a string",
        "- a\n- b\n",
        "version: 1\n",
        "workflows: not-a-map\n",
        "workflows: [unclosed\n",
        "key: [unclosed\n  : :",
    ],
)
def test_parse_lockfile_empty_or_malformed(content: str) -> None:
    assert parse_lockfile(content) == {}


def test_parse_lockfile_non_list_deps() -> None:
    assert parse_lockfile("workflows:\n  a.yml:\n") == {"a.yml": set()}


def test_get_repos_filters_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    data = [
        {"name": "a", "isArchived": False},
        {"name": "b", "isArchived": True},
    ]
    seen: list[tuple[str, ...]] = []

    def fake(*args: str) -> str:
        seen.append(args)
        return json.dumps(data)

    monkeypatch.setattr(scan, "run_gh", fake)
    assert get_repos("org") == [{"name": "a", "isArchived": False}]
    assert seen[0][:3] == ("repo", "list", "org")


@pytest.mark.parametrize(
    ("output", "expected"),
    [("true\n", True), ("FALSE\n", False), ("null\n", None)],
)
def test_get_sha_pinning_required(
    monkeypatch: pytest.MonkeyPatch, output: str, expected: bool | None
) -> None:
    monkeypatch.setattr(scan, "run_gh", lambda *a: output)
    assert get_sha_pinning_required("o", "r") is expected


def test_get_sha_pinning_required_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: str) -> str:
        raise RuntimeError("x")

    monkeypatch.setattr(scan, "run_gh", boom)
    assert get_sha_pinning_required("o", "r") is None


def test_get_workflow_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "run_gh", lambda *a: "a.yml\nb.yaml\nREADME.md\n\n")
    assert get_workflow_files("o", "r") == ["a.yml", "b.yaml"]


def test_get_workflow_files_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: str) -> str:
        raise RuntimeError("x")

    monkeypatch.setattr(scan, "run_gh", boom)
    assert get_workflow_files("o", "r") == []


def test_get_action_files(monkeypatch: pytest.MonkeyPatch) -> None:
    out = "action.yml\nsub/action.yaml\nsub/other.yml\nREADME.md\naction.yaml\n"
    monkeypatch.setattr(scan, "run_gh", lambda *a: out)
    assert get_action_files("o", "r") == [
        "action.yml",
        "sub/action.yaml",
        "action.yaml",
    ]


def test_get_action_files_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: str) -> str:
        raise RuntimeError("x")

    monkeypatch.setattr(scan, "run_gh", boom)
    assert get_action_files("o", "r") == []


def test_get_file_content_decodes(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = base64.b64encode(b"hello world").decode()
    chunked = raw[:4] + "\n" + raw[4:]
    monkeypatch.setattr(scan, "run_gh", lambda *a: chunked)
    assert get_file_content("o", "r", "p") == "hello world"


def test_get_file_content_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: str) -> str:
        raise RuntimeError("x")

    monkeypatch.setattr(scan, "run_gh", boom)
    assert get_file_content("o", "r", "p") == ""


def test_get_lockfile_requests_lockfile_path(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    raw = base64.b64encode(b"lock").decode()
    router.add("api", "repos/o/r/contents/.github/workflows/actions.lock", result=raw)
    monkeypatch.setattr(scan, "run_gh", router)
    assert get_lockfile("o", "r") == "lock"


def test_get_lockfile_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "run_gh", make_repo_gh())
    assert get_lockfile("o", "r") == ""


def test_check_repo_no_workflows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "run_gh", make_repo_gh(sha_required="true"))
    result = check_repo("o", "r")
    assert result == {
        "name": "r",
        "has_workflows": False,
        "external_workflows": [],
        "has_lockfile": False,
        "has_verify_workflow": False,
        "locked_workflows": [],
        "uncovered_workflows": [],
        "compliant": None,
        "sha_pinning_required": True,
        "workflow_files": [],
        "action_files": [],
    }


def test_check_repo_workflows_without_external_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = make_repo_gh(workflows={"ci.yml": NO_EXTERNAL_WF, "empty.yml": ""})
    monkeypatch.setattr(scan, "run_gh", gh)
    result = check_repo("o", "r")
    assert result["has_workflows"] is True
    assert result["external_workflows"] == []
    assert result["compliant"] is None
    assert result["sha_pinning_required"] is False


def test_check_repo_compliant(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = make_repo_gh(
        workflows={
            "verify-lockfile.yml": VERIFY_WF,
            "ci.yml": EXTERNAL_WF,
            "none.yml": NO_EXTERNAL_WF,
        },
        lockfile=LOCKFILE,
        action_files=["action.yml"],
    )
    monkeypatch.setattr(scan, "run_gh", gh)
    result = check_repo("o", "r")
    assert result["compliant"] is True
    assert result["has_lockfile"] is True
    assert result["has_verify_workflow"] is True
    assert result["external_workflows"] == [
        ".github/workflows/ci.yml",
        ".github/workflows/verify-lockfile.yml",
    ]
    assert result["locked_workflows"] == [
        ".github/workflows/ci.yml",
        ".github/workflows/verify-lockfile.yml",
    ]
    assert result["uncovered_workflows"] == []
    assert result["action_files"] == ["action.yml"]


def test_check_repo_missing_lockfile(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = make_repo_gh(workflows={"ci.yml": EXTERNAL_WF})
    monkeypatch.setattr(scan, "run_gh", gh)
    result = check_repo("o", "r")
    assert result["compliant"] is False
    assert result["has_lockfile"] is False
    assert result["uncovered_workflows"] == [".github/workflows/ci.yml"]


def test_check_repo_missing_verify_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = (
        "workflows:\n    '.github/workflows/ci.yml':\n        - 'actions/checkout@v4'\n"
    )
    gh = make_repo_gh(workflows={"ci.yml": EXTERNAL_WF}, lockfile=lock)
    monkeypatch.setattr(scan, "run_gh", gh)
    result = check_repo("o", "r")
    assert result["compliant"] is False
    assert result["has_lockfile"] is True
    assert result["has_verify_workflow"] is False
    assert result["uncovered_workflows"] == []


def test_check_repo_uncovered_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = make_repo_gh(
        workflows={
            "verify-lockfile.yml": VERIFY_WF,
            "ci.yml": EXTERNAL_WF,
            "new.yml": EXTERNAL_WF,
        },
        lockfile=LOCKFILE,
    )
    monkeypatch.setattr(scan, "run_gh", gh)
    result = check_repo("o", "r")
    assert result["compliant"] is False
    assert result["uncovered_workflows"] == [".github/workflows/new.yml"]


def test_check_repo_sha_required_error_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scan, "run_gh", make_repo_gh(sha_required="error"))
    assert check_repo("o", "r")["sha_pinning_required"] is None
