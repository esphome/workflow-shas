#!/usr/bin/env python3
"""Check all esphome organization repos for SHA pinning in GitHub Actions.

Creates/updates a pinned tracking issue in esphome/workflow-shas with
sub-issues in each non-compliant repository.

Usage:
    python check_sha_pinning.py              # full run: scan + update issues
    python check_sha_pinning.py --dry-run    # scan only, don't create/update issues
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
from pathlib import Path

ORG = "esphome"
TRACKING_REPO = "workflow-shas"
TRACKING_REPO_FULL = f"{ORG}/{TRACKING_REPO}"

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
USES_PATTERN = re.compile(r"uses:\s*([^@\s]+)@(\S+)")

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
# GitHub API helpers
# ---------------------------------------------------------------------------


def run_gh(*args: str) -> str:
    """Run a gh CLI command and return stdout."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def gh_api_json(endpoint: str, **kwargs) -> dict | list:
    """Call the GitHub REST API and return parsed JSON."""
    args = ["api", endpoint]
    for k, v in kwargs.items():
        args.extend([f"--{k}", v])
    return json.loads(run_gh(*args))


# ---------------------------------------------------------------------------
# Repo scanning (unchanged from original)
# ---------------------------------------------------------------------------


def get_repos() -> list[dict]:
    """Get all non-archived repos in the organization."""
    data = json.loads(
        run_gh("repo", "list", ORG, "--limit", "200", "--json", "name,isArchived")
    )
    return [r for r in data if not r["isArchived"]]


def get_sha_pinning_required(repo_name: str) -> bool | None:
    """Check if the repo has sha_pinning_required enabled."""
    full_name = f"{ORG}/{repo_name}"
    try:
        output = run_gh(
            "api",
            f"repos/{full_name}/actions/permissions",
            "--jq",
            ".sha_pinning_required",
        )
        value = output.strip().lower()
        if value == "true":
            return True
        elif value == "false":
            return False
        return None
    except RuntimeError:
        return None


def get_workflow_files(repo_name: str) -> list[str]:
    """Get list of workflow file paths for a repo."""
    full_name = f"{ORG}/{repo_name}"
    try:
        output = run_gh(
            "api",
            f"repos/{full_name}/contents/.github/workflows",
            "--jq",
            ".[].name",
        )
        files = [f.strip() for f in output.strip().split("\n") if f.strip()]
        return [f for f in files if f.endswith((".yml", ".yaml"))]
    except RuntimeError:
        return []


def get_file_content(repo_name: str, path: str) -> str:
    """Get raw file content from a repo (base64-decoded)."""
    full_name = f"{ORG}/{repo_name}"
    try:
        raw = run_gh("api", f"repos/{full_name}/contents/{path}", "--jq", ".content")
        return base64.b64decode(raw.replace("\n", "")).decode("utf-8")
    except (RuntimeError, Exception):
        return ""


def analyze_workflow(content: str) -> dict:
    """Analyze a workflow file for action references."""
    sha_pinned = []
    not_pinned = []

    for match in USES_PATTERN.finditer(content):
        action = match.group(1)
        ref = match.group(2)

        # Strip inline comments from ref (e.g., "abc123 # v4.2" -> "abc123")
        if " " in ref:
            ref = ref.split()[0]

        if action.startswith("./") or action.startswith("docker://"):
            continue

        if SHA_PATTERN.match(ref):
            sha_pinned.append(f"{action}@{ref}")
        else:
            not_pinned.append(f"{action}@{ref}")

    return {"sha_pinned": sha_pinned, "not_pinned": not_pinned}


def check_repo(repo_name: str) -> dict:
    """Check a single repo for SHA pinning status."""
    sha_pinning_required = get_sha_pinning_required(repo_name)
    workflow_files = get_workflow_files(repo_name)

    if not workflow_files:
        return {
            "name": repo_name,
            "has_workflows": False,
            "all_pinned": None,
            "sha_pinning_required": sha_pinning_required,
            "sha_pinned": [],
            "not_pinned": [],
            "workflow_files": [],
        }

    all_sha_pinned = []
    all_not_pinned = []

    for wf in workflow_files:
        path = f".github/workflows/{wf}"
        content = get_file_content(repo_name, path)
        if not content:
            continue
        result = analyze_workflow(content)
        all_sha_pinned.extend(result["sha_pinned"])
        all_not_pinned.extend(result["not_pinned"])

    has_actions = len(all_sha_pinned) + len(all_not_pinned) > 0
    all_pinned = has_actions and len(all_not_pinned) == 0

    return {
        "name": repo_name,
        "has_workflows": True,
        "all_pinned": all_pinned if has_actions else None,
        "sha_pinning_required": sha_pinning_required,
        "sha_pinned": sorted(set(all_sha_pinned)),
        "not_pinned": sorted(set(all_not_pinned)),
        "workflow_files": workflow_files,
    }


# ---------------------------------------------------------------------------
# Issue management
# ---------------------------------------------------------------------------


def get_issue_numeric_id(repo_full: str, issue_number: int) -> int:
    """Get the numeric (REST API) id for an issue.

    ``gh issue view --json id`` returns the GraphQL ``node_id`` (a string
    like ``I_kwDO...``), but the sub-issues API requires the integer ``id``
    from the REST API.
    """
    data = gh_api_json(f"repos/{repo_full}/issues/{issue_number}")
    return data["id"]


def find_tracking_issue() -> dict | None:
    """Find the existing tracking issue in the tracking repo."""
    try:
        issues = json.loads(
            run_gh(
                "issue",
                "list",
                "--repo",
                TRACKING_REPO_FULL,
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


def create_tracking_issue(body: str) -> dict:
    """Create the tracking issue and pin it."""
    url = run_gh(
        "issue",
        "create",
        "--repo",
        TRACKING_REPO_FULL,
        "--title",
        TRACKING_ISSUE_TITLE,
        "--body",
        body,
    ).strip()
    # Extract issue number from URL
    number = int(url.rstrip("/").split("/")[-1])

    # Pin the issue
    try:
        run_gh("issue", "pin", str(number), "--repo", TRACKING_REPO_FULL)
        print(f"  Pinned tracking issue #{number}")
    except RuntimeError as e:
        print(f"  Warning: could not pin issue: {e}")

    print(f"  Created tracking issue: {url}")
    return {"number": number, "title": TRACKING_ISSUE_TITLE}


def update_tracking_issue(issue_number: int, body: str) -> None:
    """Update the body of the tracking issue."""
    run_gh(
        "issue",
        "edit",
        str(issue_number),
        "--repo",
        TRACKING_REPO_FULL,
        "--body",
        body,
    )
    print(f"  Updated tracking issue #{issue_number}")


def get_existing_sub_issues(issue_number: int) -> list[dict]:
    """Get all current sub-issues of the tracking issue."""
    try:
        return json.loads(
            run_gh(
                "api",
                f"repos/{TRACKING_REPO_FULL}/issues/{issue_number}/sub_issues",
                "--paginate",
            )
        )
    except RuntimeError:
        return []


def find_repo_issue(repo_name: str) -> dict | None:
    """Find an existing SHA pinning issue in a specific repo."""
    full_name = f"{ORG}/{repo_name}"
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


def create_sub_issue(repo_name: str, not_pinned: list[str]) -> dict | None:
    """Create a sub-issue in the target repo."""
    full_name = f"{ORG}/{repo_name}"
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


def update_sub_issue(repo_name: str, issue_number: int, not_pinned: list[str]) -> None:
    """Update the body of an existing sub-issue."""
    full_name = f"{ORG}/{repo_name}"
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


def close_sub_issue(repo_name: str, issue_number: int) -> None:
    """Close a sub-issue (repo is now compliant)."""
    full_name = f"{ORG}/{repo_name}"
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


def link_sub_issue(tracking_issue_number: int, sub_issue_id: int) -> None:
    """Add a sub-issue to the tracking issue."""
    try:
        run_gh(
            "api",
            "--method",
            "POST",
            f"repos/{TRACKING_REPO_FULL}/issues/{tracking_issue_number}/sub_issues",
            "-f",
            f"sub_issue_id={sub_issue_id}",
        )
    except RuntimeError as e:
        # May already be linked, or cross-repo sub-issues may not be supported
        err = str(e)
        if "already" in err.lower() or "Sub-issue already exists" in err:
            pass  # Already linked
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


def generate_tracking_body(results: list[dict]) -> str:
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
        "all repositories in the [esphome](https://github.com/esphome) organization.",
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

    # Compliant repos
    if pinned:
        lines.append("### Fully SHA-Pinned Repositories")
        lines.append("")
        table_rows = []
        for r in sorted(pinned, key=lambda x: x["name"]):
            repo_link = f"[{r['name']}](https://github.com/{ORG}/{r['name']})"
            wf_list = ", ".join(f"`{wf}`" for wf in sorted(r["workflow_files"]))
            table_rows.append([repo_link, enforcement_label(r), wf_list])
        lines.extend(
            format_table(["Repository", "Enforced", "Workflow Files"], table_rows)
        )
        lines.append("")

    # Non-compliant repos
    if not_pinned:
        lines.append("### Repositories NOT Fully SHA-Pinned")
        lines.append("")
        table_rows = []
        for r in sorted(not_pinned, key=lambda x: x["name"]):
            repo_link = f"[{r['name']}](https://github.com/{ORG}/{r['name']})"
            unpinned = str(len(r["not_pinned"]))
            pinned_count = str(len(r["sha_pinned"]))
            table_rows.append([repo_link, enforcement_label(r), unpinned, pinned_count])
        lines.extend(
            format_table(["Repository", "Enforced", "Unpinned", "Pinned"], table_rows)
        )
        lines.append("")

    # No external actions
    if no_actions:
        lines.append("### Workflows Without External Actions")
        lines.append("")
        for r in sorted(no_actions, key=lambda x: x["name"]):
            lines.append(f"- [{r['name']}](https://github.com/{ORG}/{r['name']})")
        lines.append("")

    # No workflows
    if no_workflows:
        lines.append("### Repositories Without Workflows")
        lines.append("")
        for r in sorted(no_workflows, key=lambda x: x["name"]):
            lines.append(f"- [{r['name']}](https://github.com/{ORG}/{r['name']})")
        lines.append("")

    lines.append("---")
    lines.append("*This issue is automatically updated by a daily workflow.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check SHA pinning compliance and manage tracking issues.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan repos and print results, but don't create/update issues.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # --- Phase 1: Scan all repos ---
    print("Fetching repository list...")
    repos = get_repos()
    print(f"Found {len(repos)} active repositories in {ORG}\n")

    results = []
    for i, repo in enumerate(sorted(repos, key=lambda x: x["name"]), 1):
        name = repo["name"]
        print(f"  [{i}/{len(repos)}] Checking {name}...", end=" ", flush=True)
        result = check_repo(name)
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
    json_path = Path(__file__).parent / "results.json"
    json_path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nResults saved to {json_path}")

    not_pinned_repos = [r for r in results if r["all_pinned"] is False]
    compliant_repos = {r["name"] for r in results if r["all_pinned"] is not False}

    print(f"\n{len(not_pinned_repos)} non-compliant repos found")

    if args.dry_run:
        print("\n[dry-run] Would create/update tracking issue with body:")
        print("---")
        print(generate_tracking_body(results))
        print("---")
        for r in not_pinned_repos:
            print(f"[dry-run] Would create/update sub-issue in {ORG}/{r['name']}")
        return

    # --- Phase 2: Manage tracking issue ---
    print("\nManaging tracking issue...")
    tracking_body = generate_tracking_body(results)

    tracking_issue = find_tracking_issue()
    if tracking_issue:
        update_tracking_issue(tracking_issue["number"], tracking_body)
    else:
        tracking_issue = create_tracking_issue(tracking_body)

    tracking_number = tracking_issue["number"]

    # Get existing sub-issues to know what's already linked
    existing_subs = get_existing_sub_issues(tracking_number)
    # Map: repo full_name -> sub-issue data
    existing_sub_repos = {}
    for sub in existing_subs:
        repo_url = sub.get("repository_url", "")
        # repository_url is like "https://api.github.com/repos/esphome/firmware"
        repo_full = "/".join(repo_url.rstrip("/").split("/")[-2:])
        existing_sub_repos[repo_full] = sub

    # --- Phase 3: Create/update sub-issues in non-compliant repos ---
    print("\nManaging sub-issues...")
    for r in sorted(not_pinned_repos, key=lambda x: x["name"]):
        repo_name = r["name"]
        repo_full = f"{ORG}/{repo_name}"
        print(f"  {repo_name}:")

        # Find or create the sub-issue in the target repo
        existing = find_repo_issue(repo_name)
        if existing:
            update_sub_issue(repo_name, existing["number"], r["not_pinned"])
            sub_issue_number = existing["number"]
        else:
            created = create_sub_issue(repo_name, r["not_pinned"])
            if not created:
                continue
            sub_issue_number = created["number"]

        # Link as sub-issue if not already linked
        if repo_full not in existing_sub_repos:
            numeric_id = get_issue_numeric_id(repo_full, sub_issue_number)
            link_sub_issue(tracking_number, numeric_id)
            print(f"    Linked as sub-issue of #{tracking_number}")

    # --- Phase 4: Close sub-issues for repos that are now compliant ---
    print("\nChecking for newly compliant repos...")
    for repo_full, sub in existing_sub_repos.items():
        repo_name = repo_full.split("/")[-1]
        if repo_name in compliant_repos and sub.get("state") == "open":
            # Find the issue in the target repo to close it
            close_sub_issue(repo_name, sub["number"])

    print(
        f"\nDone. Tracking issue: https://github.com/{TRACKING_REPO_FULL}/issues/{tracking_number}"
    )


if __name__ == "__main__":
    main()
