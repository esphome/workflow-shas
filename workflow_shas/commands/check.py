"""Check SHA pinning compliance and manage tracking issues.

Scans all repos in an org, creates/updates a pinned tracking issue in
a designated tracking repo, and creates sub-issues in each non-compliant
repository.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..gh import gh_api_json, run_gh
from ..scan import check_repo, get_repos

# Title used to find/create the parent tracking issue
TRACKING_ISSUE_TITLE = "GitHub Actions SHA Pinning Compliance"

# Title prefix used for sub-issues in each repo
SUB_ISSUE_TITLE = "Pin GitHub Actions to commit SHAs"

SUB_ISSUE_BODY_TEMPLATE = """\
GitHub Actions in this repository reference actions or reusable workflows \
by mutable tag or branch instead of an immutable commit SHA.

Pinning to a full 40-character commit SHA prevents supply-chain attacks \
where a compromised or force-pushed tag silently changes the code your \
workflows execute.

### Unpinned references

{unpinned_list}

### What to do

Replace each tag/branch reference with its commit SHA and add a version comment:

```yaml
# Before
uses: actions/checkout@v4

# After
uses: actions/checkout@11bd719...  # v4.2.2
```

### References

- [GitHub Blog: Four tips to keep your GitHub Actions workflows secure]\
(https://github.blog/open-source/four-tips-to-keep-your-github-actions-workflows-secure/\
#use-specific-action-version-tags)
- [GitHub Docs: Security hardening for GitHub Actions]\
(https://docs.github.com/en/actions/security-for-github-actions/security-guides/\
security-hardening-for-github-actions#using-third-party-actions)
"""


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
    """Find an existing SHA pinning issue in a specific repo."""
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


def create_sub_issue(org: str, repo_name: str, not_pinned: list[str]) -> dict | None:
    """Create a sub-issue in the target repo."""
    full_name = f"{org}/{repo_name}"
    unpinned_list = "\n".join(f"- `{action}`" for action in sorted(not_pinned))
    body = SUB_ISSUE_BODY_TEMPLATE.format(unpinned_list=unpinned_list)

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


def update_sub_issue(
    org: str, repo_name: str, issue_number: int, not_pinned: list[str]
) -> None:
    """Update the body of an existing sub-issue."""
    full_name = f"{org}/{repo_name}"
    unpinned_list = "\n".join(f"- `{action}`" for action in sorted(not_pinned))
    body = SUB_ISSUE_BODY_TEMPLATE.format(unpinned_list=unpinned_list)

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
    pinned = [r for r in results if r["all_pinned"] is True]
    not_pinned = [r for r in results if r["all_pinned"] is False]
    no_workflows = [r for r in results if not r["has_workflows"]]
    no_actions = [r for r in results if r["has_workflows"] and r["all_pinned"] is None]

    enforced_count = sum(1 for r in results if r["sha_pinning_required"] is True)
    not_enforced_count = sum(1 for r in results if r["sha_pinning_required"] is False)

    def enforcement_label(r: dict) -> str:
        if r["sha_pinning_required"] is True:
            return "Yes"
        elif r["sha_pinning_required"] is False:
            return "No"
        return "?"

    lines = [
        "This issue tracks SHA pinning compliance for GitHub Actions across "
        f"all repositories in the [{org}](https://github.com/{org}) organization.",
        "",
        "**SHA pinning** means referencing actions by their full commit SHA "
        "(e.g., `actions/checkout@<sha>`) instead of a mutable tag "
        "(e.g., `actions/checkout@v4`). This prevents supply-chain attacks "
        "via compromised or force-pushed tags.",
        "",
        "The **Enforced** column shows whether the repo has `sha_pinning_required` "
        "enabled in Settings > Actions > General.",
        "",
        "Each non-compliant repository has a linked sub-issue with details.",
        "",
        "### Summary",
        "",
        f"- **{len(pinned)}** fully pinned",
        f"- **{len(not_pinned)}** not fully pinned",
        f"- **{len(no_actions)}** with workflows but no external actions",
        f"- **{len(no_workflows)}** without workflows",
        f"- **{enforced_count}** enforced / **{not_enforced_count}** not enforced",
        "",
    ]

    if pinned:
        lines.append("### Fully SHA-Pinned Repositories")
        lines.append("")
        table_rows = []
        for r in sorted(pinned, key=lambda x: x["name"]):
            repo_link = f"[{r['name']}](https://github.com/{org}/{r['name']})"
            wf_list = ", ".join(
                [f"`{wf}`" for wf in sorted(r["workflow_files"])]
                + [f"`{af}`" for af in sorted(r.get("action_files", []))]
            )
            table_rows.append([repo_link, enforcement_label(r), wf_list])
        lines.extend(
            format_table(["Repository", "Enforced", "Workflow Files"], table_rows)
        )
        lines.append("")

    if not_pinned:
        lines.append("### Repositories NOT Fully SHA-Pinned")
        lines.append("")
        table_rows = []
        for r in sorted(not_pinned, key=lambda x: x["name"]):
            repo_link = f"[{r['name']}](https://github.com/{org}/{r['name']})"
            unpinned = str(len(r["not_pinned"]))
            pinned_count = str(len(r["sha_pinned"]))
            table_rows.append([repo_link, enforcement_label(r), unpinned, pinned_count])
        lines.extend(
            format_table(["Repository", "Enforced", "Unpinned", "Pinned"], table_rows)
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
    tracking_repo_full = f"{org}/{tracking_repo}"

    # --- Phase 1: Scan repos ---
    if repos:
        repo_names = sorted(repos)
        print(f"Checking {len(repo_names)} specified repos...\n")
    else:
        print("Fetching repository list...")
        all_repos = get_repos(org)
        repo_names = sorted(r["name"] for r in all_repos)
        print(f"Found {len(repo_names)} active repositories in {org}\n")

    results = []
    for i, name in enumerate(repo_names, 1):
        print(f"  [{i}/{len(repo_names)}] Checking {name}...", end=" ", flush=True)
        result = check_repo(org, name)
        results.append(result)

        enforced = " [enforced]" if result["sha_pinning_required"] else ""
        if not result["has_workflows"]:
            print(f"no workflows{enforced}")
        elif result["all_pinned"] is None:
            print(f"no external actions{enforced}")
        elif result["all_pinned"]:
            print(f"fully pinned{enforced}")
        else:
            print(f"NOT pinned ({len(result['not_pinned'])} unpinned){enforced}")

    # Save raw results
    json_path = Path(__file__).resolve().parent.parent.parent / "results.json"
    json_path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nResults saved to {json_path}")

    not_pinned_repos = [r for r in results if r["all_pinned"] is False]
    compliant_repos = {r["name"] for r in results if r["all_pinned"] is not False}

    print(f"\n{len(not_pinned_repos)} non-compliant repos found")

    if dry_run:
        print("\n[dry-run] Would create/update tracking issue with body:")
        print("---")
        print(generate_tracking_body(org, results))
        print("---")
        for r in not_pinned_repos:
            print(f"[dry-run] Would create/update sub-issue in {org}/{r['name']}")
        return

    # --- Phase 2: Manage tracking issue ---
    print("\nManaging tracking issue...")
    tracking_body = generate_tracking_body(org, results)

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
    for r in sorted(not_pinned_repos, key=lambda x: x["name"]):
        repo_name = r["name"]
        repo_full = f"{org}/{repo_name}"
        print(f"  {repo_name}:")

        existing = find_repo_issue(org, repo_name)
        if existing:
            update_sub_issue(org, repo_name, existing["number"], r["not_pinned"])
            sub_issue_number = existing["number"]
        else:
            created = create_sub_issue(org, repo_name, r["not_pinned"])
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
