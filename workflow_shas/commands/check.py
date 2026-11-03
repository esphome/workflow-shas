"""Check gh-actions-lock adoption and manage tracking issues.

Scans all repos in an org, creates/updates a pinned tracking issue in
a designated tracking repo, and creates sub-issues in each repository
that has not yet adopted ``gh actions-lock``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..gh import gh_api_json, run_gh
from ..scan import (
    LOCKFILE_PATH,
    VERIFY_WORKFLOW_NAME,
    WORKFLOWS_DIR,
    check_repo,
    get_repos,
)

# Title used to find/create the parent tracking issue
TRACKING_ISSUE_TITLE = "GitHub Actions Lockfile Adoption"

# Title prefix used for sub-issues in each repo
SUB_ISSUE_TITLE = "Adopt gh actions-lock for GitHub Actions"

SUB_ISSUE_BODY_TEMPLATE = """\
GitHub Actions in this repository reference actions or reusable workflows \
by mutable tag or branch, and are not yet locked with \
[`gh actions-lock`](https://github.com/github/gh-actions-lock).

`gh actions-lock` keeps readable version tags in your `uses:` lines and \
records the exact commit SHA of every direct and transitive action in \
`.github/workflows/actions.lock`. A compromised or force-pushed tag can no \
longer silently change the code your workflows execute, and changes to the \
lockfile are visible in pull request diffs.

### What is missing

{missing}

### What to do

1. Install the extension and generate the lockfile:

   ```sh
   gh extension install github/gh-actions-lock
   gh actions-lock
   ```

   Keep version tags in `uses:` (for example `actions/checkout@v7.0.1`). \
Any inline commit SHAs are rewritten back to version tags automatically.

2. Add `.github/workflows/verify-lockfile.yml`, which runs \
`gh actions-lock --verify` on every pull request and push to catch drift \
or tampering.

3. Commit `.github/workflows/actions.lock` and the verify workflow.

### References

- [github/gh-actions-lock](https://github.com/github/gh-actions-lock)
- [GitHub Docs: Security hardening for GitHub Actions]\
(https://docs.github.com/en/actions/security-for-github-actions/security-guides/\
security-hardening-for-github-actions#using-third-party-actions)
"""


def format_missing(result: dict) -> str:
    """Describe what a repo still needs to adopt ``gh actions-lock``."""
    items: list[str] = []
    if not result["has_lockfile"]:
        items.append(f"- No lockfile found at `{LOCKFILE_PATH}`")
    elif result["uncovered_workflows"]:
        items.append("- Workflows not covered by the lockfile:")
        items.extend(f"  - `{wf}`" for wf in sorted(result["uncovered_workflows"]))
    if not result["has_verify_workflow"]:
        items.append(
            f"- No verify workflow found at `{WORKFLOWS_DIR}/{VERIFY_WORKFLOW_NAME}`"
        )
    return "\n".join(items)


# ---------------------------------------------------------------------------
# Issue management helpers
# ---------------------------------------------------------------------------


def get_issue_numeric_id(repo_full: str, issue_number: int) -> int:
    """Get the numeric (REST API) id for an issue.

    ``gh issue view --json id`` returns the GraphQL ``node_id`` (a string
    like ``I_kwDO...``), but the sub-issues API requires the integer ``id``
    from the REST API.
    """
    data = gh_api_json(f"repos/{repo_full}/issues/{issue_number}")
    return data["id"]


def find_tracking_issue(tracking_repo: str) -> dict | None:
    """Find the existing tracking issue in the tracking repo."""
    try:
        issues = json.loads(
            run_gh(
                "issue",
                "list",
                "--repo",
                tracking_repo,
                "--state",
                "open",
                "--search",
                f"in:title {TRACKING_ISSUE_TITLE}",
                "--json",
                "number,title",
                "--limit",
                "10",
            )
        )
        for issue in issues:
            if issue["title"] == TRACKING_ISSUE_TITLE:
                return issue
    except RuntimeError:
        pass
    return None


def create_tracking_issue(tracking_repo: str, body: str) -> dict:
    """Create the tracking issue and pin it."""
    url = run_gh(
        "issue",
        "create",
        "--repo",
        tracking_repo,
        "--title",
        TRACKING_ISSUE_TITLE,
        "--body",
        body,
    ).strip()
    number = int(url.rstrip("/").split("/")[-1])

    try:
        run_gh("issue", "pin", str(number), "--repo", tracking_repo)
        print(f"  Pinned tracking issue #{number}")
    except RuntimeError as e:
        print(f"  Warning: could not pin issue: {e}")

    print(f"  Created tracking issue: {url}")
    return {"number": number, "title": TRACKING_ISSUE_TITLE}


def update_tracking_issue(tracking_repo: str, issue_number: int, body: str) -> None:
    """Update the body of the tracking issue."""
    run_gh(
        "issue",
        "edit",
        str(issue_number),
        "--repo",
        tracking_repo,
        "--body",
        body,
    )
    print(f"  Updated tracking issue #{issue_number}")


def get_existing_sub_issues(tracking_repo: str, issue_number: int) -> list[dict]:
    """Get all current sub-issues of the tracking issue."""
    try:
        return json.loads(
            run_gh(
                "api",
                f"repos/{tracking_repo}/issues/{issue_number}/sub_issues",
                "--paginate",
            )
        )
    except RuntimeError:
        return []


def find_repo_issue(org: str, repo_name: str) -> dict | None:
    """Find an existing lockfile adoption issue in a specific repo."""
    full_name = f"{org}/{repo_name}"
    try:
        issues = json.loads(
            run_gh(
                "issue",
                "list",
                "--repo",
                full_name,
                "--state",
                "open",
                "--search",
                f"in:title {SUB_ISSUE_TITLE}",
                "--json",
                "number,title",
                "--limit",
                "10",
            )
        )
        for issue in issues:
            if issue["title"] == SUB_ISSUE_TITLE:
                return issue
    except RuntimeError:
        pass
    return None


def create_sub_issue(org: str, repo_name: str, result: dict) -> dict | None:
    """Create a sub-issue in the target repo."""
    full_name = f"{org}/{repo_name}"
    body = SUB_ISSUE_BODY_TEMPLATE.format(missing=format_missing(result))

    try:
        url = run_gh(
            "issue",
            "create",
            "--repo",
            full_name,
            "--title",
            SUB_ISSUE_TITLE,
            "--body",
            body,
        ).strip()
        number = int(url.rstrip("/").split("/")[-1])
        print(f"    Created issue #{number} in {full_name}")
        return {"number": number, "title": SUB_ISSUE_TITLE}
    except RuntimeError as e:
        err = str(e).lower()
        if "issues are disabled" in err or "has issues disabled" in err or "410" in err:
            print(f"    Skipped {full_name} (issues are disabled)")
        else:
            print(f"    ERROR creating issue in {full_name}: {e}")
        return None


def update_sub_issue(org: str, repo_name: str, issue_number: int, result: dict) -> None:
    """Update the body of an existing sub-issue."""
    full_name = f"{org}/{repo_name}"
    body = SUB_ISSUE_BODY_TEMPLATE.format(missing=format_missing(result))

    try:
        run_gh(
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            full_name,
            "--body",
            body,
        )
        print(f"    Updated issue #{issue_number} in {full_name}")
    except RuntimeError as e:
        print(f"    ERROR updating issue in {full_name}: {e}")


def close_sub_issue(org: str, repo_name: str, issue_number: int) -> None:
    """Close a sub-issue (repo is now compliant)."""
    full_name = f"{org}/{repo_name}"
    try:
        run_gh(
            "issue",
            "close",
            str(issue_number),
            "--repo",
            full_name,
            "--reason",
            "completed",
        )
        print(f"    Closed issue #{issue_number} in {full_name} (now compliant)")
    except RuntimeError as e:
        print(f"    ERROR closing issue in {full_name}: {e}")


def link_sub_issue(
    tracking_repo: str, tracking_issue_number: int, sub_issue_id: int
) -> None:
    """Add a sub-issue to the tracking issue."""
    try:
        run_gh(
            "api",
            "--method",
            "POST",
            f"repos/{tracking_repo}/issues/{tracking_issue_number}/sub_issues",
            "-F",
            f"sub_issue_id={sub_issue_id}",
        )
    except RuntimeError as e:
        err = str(e)
        if "already" in err.lower() or "Sub-issue already exists" in err:
            pass
        else:
            print(f"    Warning: could not link sub-issue: {e}")


# ---------------------------------------------------------------------------
# Tracking issue body generation
# ---------------------------------------------------------------------------


def format_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Format a markdown table with equal-width columns."""
    num_cols = len(headers)
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i in range(num_cols):
            col_widths[i] = max(col_widths[i], len(row[i]))

    def fmt_row(cells: list[str]) -> str:
        padded = [cells[i].ljust(col_widths[i]) for i in range(num_cols)]
        return "| " + " | ".join(padded) + " |"

    separator = "|" + "|".join("-" * (w + 2) for w in col_widths) + "|"
    return [fmt_row(headers), separator] + [fmt_row(row) for row in rows]


def generate_tracking_body(org: str, results: list[dict]) -> str:
    """Generate the markdown body for the tracking issue."""
    adopted = [r for r in results if r["compliant"] is True]
    not_adopted = [r for r in results if r["compliant"] is False]
    no_workflows = [r for r in results if not r["has_workflows"]]
    no_actions = [r for r in results if r["has_workflows"] and r["compliant"] is None]

    enforced_count = sum(1 for r in results if r["sha_pinning_required"] is True)
    not_enforced_count = sum(1 for r in results if r["sha_pinning_required"] is False)

    def enforcement_label(r: dict) -> str:
        if r["sha_pinning_required"] is True:
            return "Yes"
        elif r["sha_pinning_required"] is False:
            return "No"
        return "?"

    lines = [
        "This issue tracks adoption of "
        "[`gh actions-lock`](https://github.com/github/gh-actions-lock) for "
        "GitHub Actions across all repositories in the "
        f"[{org}](https://github.com/{org}) organization.",
        "",
        "**`gh actions-lock`** keeps version tags in `uses:` lines and records "
        f"the exact commit SHA of every action in `{LOCKFILE_PATH}`. A "
        f"`{VERIFY_WORKFLOW_NAME}` workflow runs `gh actions-lock --verify` on "
        "every pull request to catch drift or tampering. This prevents "
        "supply-chain attacks via compromised or force-pushed tags.",
        "",
        "A repository is **adopted** when every workflow with external actions "
        "is covered by the lockfile and the verify workflow exists.",
        "",
        "The **Enforced** column shows whether the repo has `sha_pinning_required` "
        "enabled in Settings > Actions > General (the lockfile satisfies it).",
        "",
        "Each repository that has not yet adopted the lockfile has a linked "
        "sub-issue with details.",
        "",
        "### Summary",
        "",
        f"- **{len(adopted)}** adopted",
        f"- **{len(not_adopted)}** not yet adopted",
        f"- **{len(no_actions)}** with workflows but no external actions",
        f"- **{len(no_workflows)}** without workflows",
        f"- **{enforced_count}** enforced / **{not_enforced_count}** not enforced",
        "",
    ]

    if adopted:
        lines.append("### Compliant Repositories (Lockfile Adopted)")
        lines.append("")
        table_rows = []
        for r in sorted(adopted, key=lambda x: x["name"]):
            repo_link = f"[{r['name']}](https://github.com/{org}/{r['name']})"
            wf_list = ", ".join(f"`{wf}`" for wf in sorted(r["workflow_files"]))
            table_rows.append([repo_link, enforcement_label(r), wf_list])
        lines.extend(
            format_table(["Repository", "Enforced", "Workflow Files"], table_rows)
        )
        lines.append("")

    if not_adopted:
        lines.append("### Repositories Not Yet Adopted")
        lines.append("")
        table_rows = []
        for r in sorted(not_adopted, key=lambda x: x["name"]):
            repo_link = f"[{r['name']}](https://github.com/{org}/{r['name']})"
            lockfile = "Yes" if r["has_lockfile"] else "No"
            verify = "Yes" if r["has_verify_workflow"] else "No"
            uncovered = str(len(r["uncovered_workflows"]))
            table_rows.append(
                [repo_link, enforcement_label(r), lockfile, verify, uncovered]
            )
        lines.extend(
            format_table(
                ["Repository", "Enforced", "Lockfile", "Verify Workflow", "Uncovered"],
                table_rows,
            )
        )
        lines.append("")

    if no_actions:
        lines.append("### Workflows Without External Actions")
        lines.append("")
        for r in sorted(no_actions, key=lambda x: x["name"]):
            lines.append(f"- [{r['name']}](https://github.com/{org}/{r['name']})")
        lines.append("")

    if no_workflows:
        lines.append("### Repositories Without Workflows")
        lines.append("")
        for r in sorted(no_workflows, key=lambda x: x["name"]):
            lines.append(f"- [{r['name']}](https://github.com/{org}/{r['name']})")
        lines.append("")

    lines.append("---")
    lines.append("*This issue is automatically updated by a daily workflow.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Results persistence
# ---------------------------------------------------------------------------


def results_path() -> Path:
    """Path to the saved ``results.json`` (repo root, next to the package)."""
    return Path(__file__).resolve().parent.parent.parent / "results.json"


def load_previous_results() -> list[dict]:
    """Load the ``results.json`` from a prior run, or ``[]`` if unavailable."""
    try:
        return json.loads(results_path().read_text())
    except (OSError, ValueError):
        return []


def merge_results(previous: list[dict], scanned: list[dict]) -> list[dict]:
    """Merge freshly scanned repo results over previously saved ones.

    A scanned entry replaces the previous entry for the same repo; previous
    entries for repos that were not scanned this run are kept untouched. The
    result is sorted by repo name. This keeps a ``--repo`` subset run from
    rewriting the tracking issue body with only the scanned repo(s).
    """
    by_name: dict[str, dict] = {r["name"]: r for r in previous}
    for r in scanned:
        by_name[r["name"]] = r
    return sorted(by_name.values(), key=lambda r: r["name"])


_REPO_LINK_RE = re.compile(r"\[([^\]]+)\]\(")


def _enforced_from_label(label: str) -> bool | None:
    """Map an Enforced-column label back to ``sha_pinning_required``."""
    return {"Yes": True, "No": False}.get(label, None)


def parse_tracking_body(body: str) -> list[dict]:
    """Reconstruct per-repo results from an existing tracking issue body.

    The live issue is the only complete, authoritative record of every repo's
    state, so a ``--repo`` subset run rebuilds its baseline from it (rather
    than from a possibly stale or subset-only ``results.json``) and then merges
    the freshly scanned repos over the top. Fields that the body does not carry
    are filled with safe defaults: repos listed only as bullets (no workflows /
    no external actions) have no Enforced column, so ``sha_pinning_required`` is
    assumed enabled (the common case; a full scan corrects any drift).
    """
    results: list[dict] = []
    section: str | None = None
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("### Compliant Repositories"):
            section = "compliant"
        elif s.startswith("### Repositories Not Yet Adopted"):
            section = "not_adopted"
        elif s.startswith("### Workflows Without External Actions"):
            section = "no_actions"
        elif s.startswith("### Repositories Without Workflows"):
            section = "no_workflows"
        elif s.startswith("###") or s.startswith("---"):
            section = None
        elif section in ("compliant", "not_adopted") and s.startswith("| ["):
            cells = [c.strip() for c in s.strip("|").split("|")]
            match = _REPO_LINK_RE.match(cells[0])
            if not match:
                continue
            name = match.group(1)
            if section == "compliant":
                wf_cell = cells[2]
                workflow_files = (
                    [w.strip().strip("`") for w in wf_cell.split(",") if w.strip()]
                    if wf_cell
                    else []
                )
                results.append(
                    {
                        "name": name,
                        "has_workflows": True,
                        "has_lockfile": True,
                        "has_verify_workflow": True,
                        "uncovered_workflows": [],
                        "compliant": True,
                        "sha_pinning_required": _enforced_from_label(cells[1]),
                        "workflow_files": workflow_files,
                    }
                )
            else:
                count = int(cells[4]) if cells[4].isdigit() else 0
                results.append(
                    {
                        "name": name,
                        "has_workflows": True,
                        "has_lockfile": cells[2] == "Yes",
                        "has_verify_workflow": cells[3] == "Yes",
                        "uncovered_workflows": [""] * count,
                        "compliant": False,
                        "sha_pinning_required": _enforced_from_label(cells[1]),
                        "workflow_files": [],
                    }
                )
        elif section in ("no_actions", "no_workflows") and s.startswith("- ["):
            match = _REPO_LINK_RE.search(s)
            if not match:
                continue
            results.append(
                {
                    "name": match.group(1),
                    "has_workflows": section == "no_actions",
                    "has_lockfile": False,
                    "has_verify_workflow": False,
                    "uncovered_workflows": [],
                    "compliant": None,
                    "sha_pinning_required": True,
                    "workflow_files": [],
                }
            )
    return results


def fetch_tracking_results(org: str, tracking_repo: str) -> list[dict]:
    """Rebuild the full repo baseline by parsing the live tracking issue."""
    tracking_repo_full = f"{org}/{tracking_repo}"
    issue = find_tracking_issue(tracking_repo_full)
    if not issue:
        return []
    try:
        data = json.loads(
            run_gh(
                "issue",
                "view",
                str(issue["number"]),
                "--repo",
                tracking_repo_full,
                "--json",
                "body",
            )
        )
    except (RuntimeError, ValueError):
        return []
    return parse_tracking_body(data.get("body", ""))


# ---------------------------------------------------------------------------
# Subcommand entry point
# ---------------------------------------------------------------------------


def run(
    org: str,
    tracking_repo: str,
    *,
    repos: list[str] | None = None,
    dry_run: bool = False,
) -> None:
    """Run the check command."""
    # --- Phase 1: Scan repos ---
    if repos:
        repo_names = sorted(repos)
        print(f"Checking {len(repo_names)} specified repos...\n")
    else:
        print("Fetching repository list...")
        all_repos = get_repos(org)
        repo_names = sorted(r["name"] for r in all_repos)
        print(f"Found {len(repo_names)} active repositories in {org}\n")

    scanned_results = []
    for i, name in enumerate(repo_names, 1):
        print(f"  [{i}/{len(repo_names)}] Checking {name}...", end=" ", flush=True)
        result = check_repo(org, name)
        scanned_results.append(result)

        enforced = " [enforced]" if result["sha_pinning_required"] else ""
        if not result["has_workflows"]:
            print(f"no workflows{enforced}")
        elif result["compliant"] is None:
            print(f"no external actions{enforced}")
        elif result["compliant"]:
            print(f"lockfile adopted{enforced}")
        else:
            print(f"NOT adopted{enforced}")

    # When scanning a subset (--repo), merge the scanned repos over a baseline
    # of every other repo so the tracking issue body reflects only the scanned
    # repo(s) moving, instead of being rewritten with just those repos. The
    # live tracking issue is the authoritative full record, so prefer it; fall
    # back to results.json only when there is no issue (e.g. a dry run with no
    # tracking repo, or the very first run).
    if repos:
        baseline = fetch_tracking_results(org, tracking_repo) if tracking_repo else []
        if not baseline:
            baseline = load_previous_results()
        if not baseline:
            print(
                "\nWarning: no existing tracking issue or results.json found; the "
                "body will reflect only the scanned repo(s)."
            )
        all_results = merge_results(baseline, scanned_results)
    else:
        all_results = scanned_results

    # Save the full (merged) results
    json_path = results_path()
    json_path.write_text(json.dumps(all_results, indent=2) + "\n")
    print(f"\nResults saved to {json_path}")

    # Sub-issue management acts only on the repos actually scanned this run.
    noncompliant_repos = [r for r in scanned_results if r["compliant"] is False]
    compliant_repos = {
        r["name"] for r in scanned_results if r["compliant"] is not False
    }

    print(f"\n{len(noncompliant_repos)} non-compliant repos found")

    if dry_run:
        print("\n[dry-run] Would create/update tracking issue with body:")
        print("---")
        print(generate_tracking_body(org, all_results))
        print("---")
        for r in noncompliant_repos:
            print(f"[dry-run] Would create/update sub-issue in {org}/{r['name']}")
        return

    # --- Phase 2: Manage tracking issue ---
    tracking_repo_full = f"{org}/{tracking_repo}"
    print("\nManaging tracking issue...")
    tracking_body = generate_tracking_body(org, all_results)

    tracking_issue = find_tracking_issue(tracking_repo_full)
    if tracking_issue:
        update_tracking_issue(
            tracking_repo_full, tracking_issue["number"], tracking_body
        )
    else:
        tracking_issue = create_tracking_issue(tracking_repo_full, tracking_body)

    tracking_number = tracking_issue["number"]

    # Get existing sub-issues to know what's already linked
    existing_subs = get_existing_sub_issues(tracking_repo_full, tracking_number)
    existing_sub_repos: dict[str, dict] = {}
    for sub in existing_subs:
        repo_url = sub.get("repository_url", "")
        repo_full = "/".join(repo_url.rstrip("/").split("/")[-2:])
        existing_sub_repos[repo_full] = sub

    # --- Phase 3: Create/update sub-issues in non-compliant repos ---
    print("\nManaging sub-issues...")
    for r in sorted(noncompliant_repos, key=lambda x: x["name"]):
        repo_name = r["name"]
        repo_full = f"{org}/{repo_name}"
        print(f"  {repo_name}:")

        existing = find_repo_issue(org, repo_name)
        if existing:
            update_sub_issue(org, repo_name, existing["number"], r)
            sub_issue_number = existing["number"]
        else:
            created = create_sub_issue(org, repo_name, r)
            if not created:
                continue
            sub_issue_number = created["number"]

        if repo_full not in existing_sub_repos:
            numeric_id = get_issue_numeric_id(repo_full, sub_issue_number)
            link_sub_issue(tracking_repo_full, tracking_number, numeric_id)
            print(f"    Linked as sub-issue of #{tracking_number}")

    # --- Phase 4: Close sub-issues for repos that are now compliant ---
    print("\nChecking for newly compliant repos...")
    for repo_full, sub in existing_sub_repos.items():
        repo_name = repo_full.split("/")[-1]
        if repo_name in compliant_repos and sub.get("state") == "open":
            close_sub_issue(org, repo_name, sub["number"])

    print(
        f"\nDone. Tracking issue: https://github.com/{tracking_repo_full}/issues/{tracking_number}"
    )
