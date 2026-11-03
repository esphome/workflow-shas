"""Tests for workflow_shas.commands.check."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from workflow_shas.commands import check
from workflow_shas.commands.check import (
    SUB_ISSUE_BODY_TEMPLATE,
    SUB_ISSUE_TITLE,
    TRACKING_ISSUE_TITLE,
    close_sub_issue,
    create_sub_issue,
    create_tracking_issue,
    find_repo_issue,
    find_tracking_issue,
    format_missing,
    format_table,
    generate_tracking_body,
    get_existing_sub_issues,
    get_issue_numeric_id,
    link_sub_issue,
    update_sub_issue,
    update_tracking_issue,
)

from .conftest import GhRouter


def make_result(name: str, **overrides: Any) -> dict:
    result: dict[str, Any] = {
        "name": name,
        "has_workflows": True,
        "external_workflows": [".github/workflows/ci.yml"],
        "has_lockfile": True,
        "has_verify_workflow": True,
        "locked_workflows": [".github/workflows/ci.yml"],
        "uncovered_workflows": [],
        "compliant": True,
        "sha_pinning_required": True,
        "workflow_files": ["ci.yml"],
        "action_files": [],
    }
    result.update(overrides)
    return result


def test_format_table_equal_widths() -> None:
    lines = format_table(["A", "Bee"], [["long cell", "x"], ["y", "z"]])
    assert lines == [
        "| A         | Bee |",
        "|-----------|-----|",
        "| long cell | x   |",
        "| y         | z   |",
    ]
    assert len({len(line) for line in lines}) == 1


def test_format_missing_no_lockfile_and_no_verify() -> None:
    text = format_missing(
        make_result("r", has_lockfile=False, has_verify_workflow=False)
    )
    assert "No lockfile found at `.github/workflows/actions.lock`" in text
    assert "No verify workflow found at `.github/workflows/verify-lockfile.yml`" in text


def test_format_missing_uncovered() -> None:
    text = format_missing(
        make_result(
            "r",
            uncovered_workflows=[".github/workflows/b.yml", ".github/workflows/a.yml"],
        )
    )
    assert text == (
        "- Workflows not covered by the lockfile:\n"
        "  - `.github/workflows/a.yml`\n"
        "  - `.github/workflows/b.yml`"
    )


def test_format_missing_nothing() -> None:
    assert format_missing(make_result("r")) == ""


def test_no_em_dashes_in_templates() -> None:
    assert "\N{EM DASH}" not in SUB_ISSUE_BODY_TEMPLATE
    assert "\N{EM DASH}" not in generate_tracking_body("o", [make_result("r")])


def test_generate_tracking_body_all_buckets() -> None:
    results = [
        make_result("zeta", sha_pinning_required=True),
        make_result("alpha", sha_pinning_required=False),
        make_result(
            "bad",
            compliant=False,
            has_lockfile=False,
            has_verify_workflow=False,
            uncovered_workflows=[".github/workflows/ci.yml"],
            sha_pinning_required=None,
        ),
        make_result("noext", compliant=None, sha_pinning_required=False),
        make_result(
            "nowf",
            has_workflows=False,
            compliant=None,
            workflow_files=[],
            sha_pinning_required=True,
        ),
    ]
    body = generate_tracking_body("org", results)

    assert "- **2** adopted" in body
    assert "- **1** not yet adopted" in body
    assert "- **1** with workflows but no external actions" in body
    assert "- **1** without workflows" in body
    assert "- **2** enforced / **2** not enforced" in body

    assert "### Compliant Repositories (Lockfile Adopted)" in body
    assert body.index("[alpha]") < body.index("[zeta]")
    assert "| [zeta](https://github.com/org/zeta)" in body
    assert "| Yes" in body and "| No " in body

    assert "### Repositories Not Yet Adopted" in body
    bad_row = next(line for line in body.splitlines() if "[bad]" in line)
    cells = [c.strip() for c in bad_row.strip("|").split("|")]
    assert cells == [
        "[bad](https://github.com/org/bad)",
        "?",
        "No",
        "No",
        "1",
    ]

    assert "### Workflows Without External Actions" in body
    assert "- [noext](https://github.com/org/noext)" in body
    assert "### Repositories Without Workflows" in body
    assert "- [nowf](https://github.com/org/nowf)" in body
    assert body.endswith("*This issue is automatically updated by a daily workflow.*")


def test_generate_tracking_body_empty() -> None:
    body = generate_tracking_body("org", [])
    assert "### Compliant" not in body
    assert "### Repositories Not Yet Adopted" not in body
    assert "- **0** adopted" in body


def test_not_adopted_table_rows_equal_width() -> None:
    body = generate_tracking_body(
        "org",
        [
            make_result("a", compliant=False),
            make_result("longer-name", compliant=False, has_lockfile=False),
        ],
    )
    table = [
        line
        for line in body.splitlines()
        if line.startswith("|") and ("Lockfile" in line or "[" in line or "---" in line)
    ]
    assert len({len(line) for line in table}) == 1


# --- issue helpers ---------------------------------------------------------


def test_get_issue_numeric_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(endpoint: str) -> dict:
        seen.append(endpoint)
        return {"id": 99}

    monkeypatch.setattr(check, "gh_api_json", fake)
    assert get_issue_numeric_id("o/r", 3) == 99
    assert seen == ["repos/o/r/issues/3"]


def test_find_tracking_issue_match(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    issues = [
        {"number": 1, "title": "other"},
        {"number": 2, "title": TRACKING_ISSUE_TITLE},
    ]
    router.add("issue", "list", result=json.dumps(issues))
    monkeypatch.setattr(check, "run_gh", router)
    assert find_tracking_issue("o/t") == issues[1]
    args = router.calls[0]
    assert args[args.index("--search") + 1] == f"in:title {TRACKING_ISSUE_TITLE}"


def test_find_tracking_issue_none(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("issue", "list", result="[]")
    monkeypatch.setattr(check, "run_gh", router)
    assert find_tracking_issue("o/t") is None


def test_find_tracking_issue_error(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("issue", "list", result=RuntimeError("x"))
    monkeypatch.setattr(check, "run_gh", router)
    assert find_tracking_issue("o/t") is None


def test_create_tracking_issue_pins(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    router.add("issue", "create", result="https://github.com/o/t/issues/12\n")
    router.add("issue", "pin")
    monkeypatch.setattr(check, "run_gh", router)
    assert create_tracking_issue("o/t", "body") == {
        "number": 12,
        "title": TRACKING_ISSUE_TITLE,
    }
    create = router.called_with("issue", "create")[0]
    assert create[create.index("--title") + 1] == TRACKING_ISSUE_TITLE
    assert create[create.index("--body") + 1] == "body"
    assert router.called_with("issue", "pin", "12")
    assert "Pinned tracking issue #12" in capsys.readouterr().out


def test_create_tracking_issue_pin_failure(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    router.add("issue", "create", result="https://github.com/o/t/issues/12/")
    router.add("issue", "pin", result=RuntimeError("nope"))
    monkeypatch.setattr(check, "run_gh", router)
    assert create_tracking_issue("o/t", "body")["number"] == 12
    assert "could not pin issue" in capsys.readouterr().out


def test_update_tracking_issue(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("issue", "edit")
    monkeypatch.setattr(check, "run_gh", router)
    update_tracking_issue("o/t", 4, "new body")
    assert router.calls == [
        ("issue", "edit", "4", "--repo", "o/t", "--body", "new body")
    ]


def test_get_existing_sub_issues(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("api", result='[{"number": 1}]')
    monkeypatch.setattr(check, "run_gh", router)
    assert get_existing_sub_issues("o/t", 4) == [{"number": 1}]
    assert router.calls[0][1] == "repos/o/t/issues/4/sub_issues"


def test_get_existing_sub_issues_error(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("api", result=RuntimeError("x"))
    monkeypatch.setattr(check, "run_gh", router)
    assert get_existing_sub_issues("o/t", 4) == []


def test_find_repo_issue(monkeypatch: pytest.MonkeyPatch, router: GhRouter) -> None:
    issues = [{"number": 7, "title": SUB_ISSUE_TITLE}]
    router.add("issue", "list", result=json.dumps(issues))
    monkeypatch.setattr(check, "run_gh", router)
    assert find_repo_issue("o", "r") == issues[0]
    args = router.calls[0]
    assert args[args.index("--repo") + 1] == "o/r"
    assert args[args.index("--search") + 1] == f"in:title {SUB_ISSUE_TITLE}"


def test_find_repo_issue_no_match(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("issue", "list", result='[{"number": 7, "title": "different"}]')
    monkeypatch.setattr(check, "run_gh", router)
    assert find_repo_issue("o", "r") is None


def test_find_repo_issue_error(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("issue", "list", result=RuntimeError("x"))
    monkeypatch.setattr(check, "run_gh", router)
    assert find_repo_issue("o", "r") is None


def test_create_sub_issue_uses_missing_section(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("issue", "create", result="https://github.com/o/r/issues/5\n")
    monkeypatch.setattr(check, "run_gh", router)
    result = make_result("r", compliant=False, has_lockfile=False)
    assert create_sub_issue("o", "r", result) == {
        "number": 5,
        "title": SUB_ISSUE_TITLE,
    }
    args = router.calls[0]
    body = args[args.index("--body") + 1]
    assert "No lockfile found" in body
    assert "gh actions-lock" in body
    assert args[args.index("--title") + 1] == SUB_ISSUE_TITLE


@pytest.mark.parametrize(
    "error",
    ["Issues are disabled for this repo", "has issues disabled", "HTTP 410"],
)
def test_create_sub_issue_issues_disabled(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    capsys: pytest.CaptureFixture[str],
    error: str,
) -> None:
    router.add("issue", "create", result=RuntimeError(error))
    monkeypatch.setattr(check, "run_gh", router)
    assert create_sub_issue("o", "r", make_result("r")) is None
    assert "issues are disabled" in capsys.readouterr().out


def test_create_sub_issue_other_error(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    router.add("issue", "create", result=RuntimeError("kaboom"))
    monkeypatch.setattr(check, "run_gh", router)
    assert create_sub_issue("o", "r", make_result("r")) is None
    assert "ERROR creating issue" in capsys.readouterr().out


def test_update_sub_issue(monkeypatch: pytest.MonkeyPatch, router: GhRouter) -> None:
    router.add("issue", "edit")
    monkeypatch.setattr(check, "run_gh", router)
    update_sub_issue("o", "r", 5, make_result("r", has_verify_workflow=False))
    args = router.calls[0]
    assert args[:3] == ("issue", "edit", "5")
    assert "No verify workflow found" in args[args.index("--body") + 1]


def test_update_sub_issue_error(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    router.add("issue", "edit", result=RuntimeError("x"))
    monkeypatch.setattr(check, "run_gh", router)
    update_sub_issue("o", "r", 5, make_result("r"))
    assert "ERROR updating issue" in capsys.readouterr().out


def test_close_sub_issue(monkeypatch: pytest.MonkeyPatch, router: GhRouter) -> None:
    router.add("issue", "close")
    monkeypatch.setattr(check, "run_gh", router)
    close_sub_issue("o", "r", 5)
    assert router.calls == [
        ("issue", "close", "5", "--repo", "o/r", "--reason", "completed")
    ]


def test_close_sub_issue_error(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    router.add("issue", "close", result=RuntimeError("x"))
    monkeypatch.setattr(check, "run_gh", router)
    close_sub_issue("o", "r", 5)
    assert "ERROR closing issue" in capsys.readouterr().out


def test_link_sub_issue(monkeypatch: pytest.MonkeyPatch, router: GhRouter) -> None:
    router.add("api")
    monkeypatch.setattr(check, "run_gh", router)
    link_sub_issue("o/t", 4, 99)
    assert router.calls == [
        (
            "api",
            "--method",
            "POST",
            "repos/o/t/issues/4/sub_issues",
            "-F",
            "sub_issue_id=99",
        )
    ]


def test_link_sub_issue_already_linked_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    router.add("api", result=RuntimeError("Sub-issue already exists"))
    monkeypatch.setattr(check, "run_gh", router)
    link_sub_issue("o/t", 4, 99)
    assert capsys.readouterr().out == ""


def test_link_sub_issue_other_error_warns(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    router.add("api", result=RuntimeError("bad things"))
    monkeypatch.setattr(check, "run_gh", router)
    link_sub_issue("o/t", 4, 99)
    assert "could not link sub-issue" in capsys.readouterr().out


# --- run() -----------------------------------------------------------------


@pytest.fixture
def results_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect results.json (derived from check.__file__) into tmp_path."""
    fake_file = tmp_path / "pkg" / "commands" / "check.py"
    monkeypatch.setattr(check, "__file__", str(fake_file))
    return tmp_path


def test_run_dry_run_scans_without_touching_issues(
    monkeypatch: pytest.MonkeyPatch,
    results_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_check_repo(org: str, name: str) -> dict:
        if name == "good":
            return make_result("good", compliant=None, sha_pinning_required=True)
        if name == "bad":
            return make_result("bad", compliant=False, sha_pinning_required=False)
        if name == "ok":
            return make_result("ok")
        return make_result(
            "nowf", has_workflows=False, compliant=None, sha_pinning_required=None
        )

    monkeypatch.setattr(check, "check_repo", fake_check_repo)
    monkeypatch.setattr(
        check,
        "run_gh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no gh in dry-run")),
    )

    check.run("org", "", repos=["nowf", "good", "bad", "ok"], dry_run=True)
    out = capsys.readouterr().out

    assert "Checking 4 specified repos" in out
    assert "no workflows" in out
    assert "no external actions [enforced]" in out
    assert "NOT adopted" in out
    assert "lockfile adopted [enforced]" in out
    assert "1 non-compliant repos found" in out
    assert "[dry-run] Would create/update tracking issue" in out
    assert "[dry-run] Would create/update sub-issue in org/bad" in out

    saved = json.loads((results_dir / "results.json").read_text())
    assert [r["name"] for r in saved] == ["bad", "good", "nowf", "ok"]


def test_run_scans_org_when_no_repos_given(
    monkeypatch: pytest.MonkeyPatch,
    results_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(check, "get_repos", lambda org: [{"name": "b"}, {"name": "a"}])
    monkeypatch.setattr(check, "check_repo", lambda org, name: make_result(name))
    check.run("org", "", dry_run=True)
    out = capsys.readouterr().out
    assert "Found 2 active repositories in org" in out
    assert "0 non-compliant repos found" in out


def _sub(repo: str, number: int, state: str) -> dict:
    return {
        "number": number,
        "state": state,
        "repository_url": f"https://api.github.com/repos/{repo}",
    }


def test_run_full_flow_updates_existing_tracking_issue(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    results_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = {
        # already linked, existing issue -> update only
        "linked": make_result("linked", compliant=False),
        # existing issue, not yet linked -> update + link
        "unlinked": make_result("unlinked", compliant=False),
        # no issue -> create + link
        "fresh": make_result("fresh", compliant=False),
        # no issue, creation fails (issues disabled) -> skipped
        "noissues": make_result("noissues", compliant=False),
        # now compliant, open sub-issue -> close
        "done": make_result("done"),
        # now compliant, sub-issue already closed -> leave alone
        "doneclosed": make_result("doneclosed"),
        # sub-issue exists for a repo not in this run -> ignore
        "none": make_result("none", compliant=None),
    }
    monkeypatch.setattr(check, "get_repos", lambda org: [{"name": n} for n in results])
    monkeypatch.setattr(check, "check_repo", lambda org, name: results[name])

    tracking = [{"number": 1, "title": TRACKING_ISSUE_TITLE}]
    router.add(
        "issue", "list", "--repo", "org/workflow-shas", result=json.dumps(tracking)
    )
    router.add("issue", "edit", "1")
    subs = [
        _sub("org/linked", 11, "open"),
        _sub("org/done", 12, "open"),
        _sub("org/doneclosed", 13, "closed"),
        _sub("org/other", 14, "open"),
    ]
    router.add(
        "api",
        "repos/org/workflow-shas/issues/1/sub_issues",
        "--paginate",
        result=json.dumps(subs),
    )

    def issue_list(args: tuple[str, ...]) -> str:
        repo = args[args.index("--repo") + 1]
        existing = {
            "org/linked": [{"number": 11, "title": SUB_ISSUE_TITLE}],
            "org/unlinked": [{"number": 21, "title": SUB_ISSUE_TITLE}],
        }
        return json.dumps(existing.get(repo, []))

    router.add("issue", "list", "--repo", result=issue_list)

    def issue_create(args: tuple[str, ...]) -> str:
        repo = args[args.index("--repo") + 1]
        if repo == "org/noissues":
            raise RuntimeError("Issues are disabled")
        return "https://github.com/org/fresh/issues/31"

    router.add("issue", "create", result=issue_create)
    router.add("issue", "edit")
    router.add("issue", "close")
    router.add("api", "--method", "POST")
    monkeypatch.setattr(check, "run_gh", router)
    monkeypatch.setattr(
        check, "gh_api_json", lambda ep: {"id": int(ep.rsplit("/", 1)[1]) * 100}
    )

    check.run("org", "workflow-shas")
    out = capsys.readouterr().out

    # tracking issue was updated, not created
    assert router.called_with("issue", "edit", "1", "--repo", "org/workflow-shas")
    assert not [
        c for c in router.called_with("issue", "create") if "org/workflow-shas" in c
    ]

    # sub-issue edits for linked/unlinked
    edited = {c[2] for c in router.called_with("issue", "edit")}
    assert {"1", "11", "21"} <= edited

    # link posts only for unlinked + fresh
    links = [c[-1] for c in router.called_with("api", "--method", "POST")]
    assert sorted(links) == ["sub_issue_id=2100", "sub_issue_id=3100"]

    # only the open compliant sub-issue gets closed
    assert [c[2] for c in router.called_with("issue", "close")] == ["12"]

    assert "Skipped org/noissues (issues are disabled)" in out
    assert "Done. Tracking issue: https://github.com/org/workflow-shas/issues/1" in out


def test_run_full_flow_creates_tracking_issue(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    results_dir: Path,
) -> None:
    monkeypatch.setattr(check, "check_repo", lambda org, name: make_result(name))
    router.add("issue", "list", result="[]")
    router.add("issue", "create", result="https://github.com/org/t/issues/8")
    router.add("issue", "pin")
    router.add("api", "repos/org/t/issues/8/sub_issues", "--paginate", result="[]")
    monkeypatch.setattr(check, "run_gh", router)

    check.run("org", "t", repos=["a"])

    assert router.called_with("issue", "create", "--repo", "org/t")
    assert router.called_with("issue", "pin", "8")


# --- results persistence / merge ------------------------------------------


def test_load_previous_results_missing(results_dir: Path) -> None:
    assert check.load_previous_results() == []


def test_load_previous_results_invalid(results_dir: Path) -> None:
    (results_dir / "results.json").write_text("not valid json{")
    assert check.load_previous_results() == []


def test_load_previous_results_present(results_dir: Path) -> None:
    data = [make_result("a"), make_result("b")]
    (results_dir / "results.json").write_text(json.dumps(data))
    assert check.load_previous_results() == data


def test_merge_results_replaces_and_keeps_sorted() -> None:
    previous = [make_result("alpha"), make_result("beta", compliant=False)]
    scanned = [make_result("beta", compliant=True), make_result("gamma")]
    merged = check.merge_results(previous, scanned)
    assert [r["name"] for r in merged] == ["alpha", "beta", "gamma"]
    beta = next(r for r in merged if r["name"] == "beta")
    assert beta["compliant"] is True


def test_run_subset_merges_into_previous_results(
    monkeypatch: pytest.MonkeyPatch,
    results_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A prior full run recorded two repos.
    previous = [
        make_result("alpha"),
        make_result("beta", compliant=False, has_lockfile=False),
    ]
    (results_dir / "results.json").write_text(json.dumps(previous))

    # Re-scan only beta, which is now compliant.
    monkeypatch.setattr(check, "check_repo", lambda org, name: make_result("beta"))
    check.run("org", "", repos=["beta"], dry_run=True)
    out = capsys.readouterr().out

    # Saved results keep alpha and update beta - not a one-repo wipe.
    saved = json.loads((results_dir / "results.json").read_text())
    assert [r["name"] for r in saved] == ["alpha", "beta"]
    assert next(r for r in saved if r["name"] == "beta")["compliant"] is True

    # The rendered body reflects both repos.
    assert "- **2** adopted" in out
    assert "[alpha]" in out and "[beta]" in out
    assert "no previous results.json" not in out


def test_run_subset_without_previous_warns(
    monkeypatch: pytest.MonkeyPatch,
    results_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(check, "check_repo", lambda org, name: make_result(name))
    check.run("org", "", repos=["solo"], dry_run=True)
    assert "no existing tracking issue or results.json found" in capsys.readouterr().out


# --- tracking-issue parsing / live baseline --------------------------------


def test_parse_tracking_body_round_trips() -> None:
    results = [
        make_result("alpha", workflow_files=["ci.yml", "release.yml"]),
        make_result("qmark", sha_pinning_required=None),
        make_result(
            "bad",
            compliant=False,
            has_lockfile=False,
            has_verify_workflow=False,
            uncovered_workflows=[".github/workflows/ci.yml"],
            sha_pinning_required=False,
        ),
        make_result("noext", compliant=None, has_workflows=True),
        make_result("nowf", has_workflows=False, compliant=None, workflow_files=[]),
    ]
    body = check.generate_tracking_body("org", results)
    parsed = {r["name"]: r for r in check.parse_tracking_body(body)}

    assert set(parsed) == {"alpha", "qmark", "bad", "noext", "nowf"}
    assert parsed["alpha"]["compliant"] is True
    assert sorted(parsed["alpha"]["workflow_files"]) == ["ci.yml", "release.yml"]
    assert parsed["alpha"]["sha_pinning_required"] is True
    assert parsed["qmark"]["sha_pinning_required"] is None
    assert parsed["bad"]["compliant"] is False
    assert parsed["bad"]["has_lockfile"] is False
    assert parsed["bad"]["has_verify_workflow"] is False
    assert len(parsed["bad"]["uncovered_workflows"]) == 1
    assert parsed["bad"]["sha_pinning_required"] is False
    assert parsed["noext"]["compliant"] is None
    assert parsed["noext"]["has_workflows"] is True
    assert parsed["nowf"]["has_workflows"] is False
    assert parsed["nowf"]["sha_pinning_required"] is True


def test_parse_tracking_body_skips_malformed_rows() -> None:
    body = "\n".join(
        [
            "### Compliant Repositories (Lockfile Adopted)",
            "| [oops |",
            "### Repositories Without Workflows",
            "- [oops",
        ]
    )
    assert check.parse_tracking_body(body) == []


def test_fetch_tracking_results_no_issue(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add("issue", "list", result="[]")
    monkeypatch.setattr(check, "run_gh", router)
    assert check.fetch_tracking_results("org", "t") == []


def test_fetch_tracking_results_parses_issue(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    body = check.generate_tracking_body(
        "org", [make_result("alpha"), make_result("bad", compliant=False)]
    )
    router.add(
        "issue",
        "list",
        result=json.dumps([{"number": 5, "title": TRACKING_ISSUE_TITLE}]),
    )
    router.add("issue", "view", result=json.dumps({"body": body}))
    monkeypatch.setattr(check, "run_gh", router)
    parsed = check.fetch_tracking_results("org", "t")
    assert {r["name"] for r in parsed} == {"alpha", "bad"}


def test_fetch_tracking_results_view_error(
    monkeypatch: pytest.MonkeyPatch, router: GhRouter
) -> None:
    router.add(
        "issue",
        "list",
        result=json.dumps([{"number": 5, "title": TRACKING_ISSUE_TITLE}]),
    )
    router.add("issue", "view", result=RuntimeError("boom"))
    monkeypatch.setattr(check, "run_gh", router)
    assert check.fetch_tracking_results("org", "t") == []


def test_run_subset_uses_live_issue_as_baseline(
    monkeypatch: pytest.MonkeyPatch,
    router: GhRouter,
    results_dir: Path,
) -> None:
    # The live issue is the authoritative record: keeper is adopted, esphome is
    # not. results.json is poisoned with only esphome - it must be ignored.
    live_body = check.generate_tracking_body(
        "org",
        [
            make_result("keeper"),
            make_result("esphome", compliant=False, has_lockfile=False),
        ],
    )
    (results_dir / "results.json").write_text(
        json.dumps([make_result("esphome", compliant=False)])
    )

    router.add(
        "issue",
        "list",
        result=json.dumps([{"number": 9, "title": TRACKING_ISSUE_TITLE}]),
    )
    router.add("issue", "view", result=json.dumps({"body": live_body}))
    router.add("issue", "edit", "9")
    router.add("api", "repos/org/t/issues/9/sub_issues", "--paginate", result="[]")
    monkeypatch.setattr(check, "check_repo", lambda org, name: make_result("esphome"))
    monkeypatch.setattr(check, "run_gh", router)

    check.run("org", "t", repos=["esphome"])

    edit = router.called_with("issue", "edit", "9")[0]
    body = edit[edit.index("--body") + 1]
    # keeper is preserved and esphome moved to adopted - no one-repo wipe.
    assert "- **2** adopted" in body
    assert "[keeper]" in body
    assert "### Repositories Not Yet Adopted" not in body
