"""Adopt ``gh actions-lock`` across organization repositories.

For each repo that has not yet adopted the lockfile:
1. Clones the repo (or reuses an existing worktree)
2. Creates a branch
3. Runs ``gh actions-lock`` (writes ``.github/workflows/actions.lock`` and
   rewrites inline SHA pins back to version tags)
4. Adds the ``verify-lockfile.yml`` workflow and locks it too
5. Commits, pushes, and opens a PR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..gh import run_actions_lock, run_cmd, run_gh
from .check import SUB_ISSUE_TITLE

BRANCH_NAME = "adopt-actions-lock"
WORKTREE_NAME = "actions-lock"

# Rewrite workspace-local ``uses: ./foo`` references to repo-relative
# ``$/foo`` so gh actions-lock can follow them and lock their dependencies.
# Without this, once a lockfile exists GitHub's sha_pinning_required
# enforcement rejects any action reached through a local composite action
# because it is not recorded in the lockfile.
MIGRATE_LOCAL_ACTIONS_FLAG = "--migrate-local-actions"

# Always passed so the tool never waits on an interactive prompt.
BASE_LOCK_ARGS = ("--no-interactive", MIGRATE_LOCAL_ACTIONS_FLAG)

PR_TITLE = "Adopt gh actions-lock for GitHub Actions pinning"

VERIFY_WORKFLOW_PATH = ".github/workflows/verify-lockfile.yml"

VERIFY_WORKFLOW_TEMPLATE = """\
name: verify-lockfile

on:
  pull_request:
  push:
    branches:
      - main

permissions:
  contents: read

jobs:
  verify:
    name: Verify actions lockfile
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Checkout
        uses: actions/checkout@v7.0.1
        with:
          persist-credentials: false
      - name: Install gh-actions-lock
        run: gh extension install github/gh-actions-lock --pin v0.4.0
      - name: Verify lockfile
        run: gh actions-lock --verify
"""


def pr_body(issue_number: int | None = None) -> str:
    """Generate the PR body, optionally linking to a tracking issue."""
    closes = ""
    if issue_number is not None:
        closes = f"\n\nCloses #{issue_number}\n"

    return f"""\
## Summary

Adopt [`gh actions-lock`](https://github.com/github/gh-actions-lock) to lock \
GitHub Action and reusable workflow references to immutable commit SHAs.{closes}

## What changed

- `uses:` lines now reference readable version tags (for example \
`actions/checkout@v7.0.1`). Any inline commit SHAs were rewritten back to \
version tags.
- The commit SHA of every direct and transitive action is recorded in \
`.github/workflows/actions.lock`.
- `.github/workflows/verify-lockfile.yml` runs `gh actions-lock --verify` on \
every pull request and push to `main`, so drift or tampering is caught before \
a workflow runs.

## Why?

Referencing actions by tag (e.g., `actions/checkout@v4`) is convenient but \
carries a supply-chain risk: tags are mutable and can be force-pushed to point \
at arbitrary commits. If an action's tag is compromised, every workflow that \
references it by tag will silently run the attacker's code.

The lockfile pins every action to a full commit SHA while keeping workflow \
files readable, and changes to the lockfile show up in pull request diffs. It \
also satisfies GitHub's `sha_pinning_required` setting.

## References

- [github/gh-actions-lock](https://github.com/github/gh-actions-lock)
- [GitHub Docs: Security hardening for GitHub Actions]\
(https://docs.github.com/en/actions/security-for-github-actions/security-guides/\
security-hardening-for-github-actions#using-third-party-actions)
- [GitHub Docs: Enforcing SHA pinning for actions]\
(https://docs.github.com/en/organizations/managing-organization-settings/\
disabling-or-limiting-github-actions-for-your-organization\
#requiring-workflows-to-use-pinned-versions-of-actions)
"""


def get_default_branch(org: str, repo_name: str) -> str:
    """Get the default branch for a repo."""
    return run_gh("api", f"repos/{org}/{repo_name}", "--jq", ".default_branch").strip()


def get_noncompliant_repos(results_file: Path) -> list[dict]:
    """Load results.json and return repos that have not adopted the lockfile."""
    if not results_file.exists():
        print(f"Error: {results_file} not found. Run the check command first.")
        sys.exit(1)

    results = json.loads(results_file.read_text())
    return [r for r in results if r.get("compliant") is False]


def ensure_repo(org: str, repo_name: str, workspace: Path) -> Path:
    """Ensure we have a working directory for the repo.

    Reuses existing worktrees for incremental commits on re-runs.
    Returns the Path to the working directory.
    """
    existing = workspace / repo_name
    worktree_base = workspace / f"{repo_name}.worktrees"
    worktree_dir = worktree_base / WORKTREE_NAME

    if existing.is_dir() and (existing / ".git").exists():
        if worktree_dir.exists():
            print(f"    Reusing worktree at {worktree_dir}")
            return worktree_dir

        default_branch = get_default_branch(org, repo_name)
        run_cmd("git", "fetch", "origin", default_branch, cwd=existing)

        # Ensure the main clone isn't sitting on the lock branch
        current = run_cmd("git", "branch", "--show-current", cwd=existing, check=False)
        if current.stdout.strip() == BRANCH_NAME:
            run_cmd("git", "checkout", default_branch, cwd=existing, check=False)

        # Clean up stale worktree bookkeeping
        run_cmd("git", "worktree", "prune", cwd=existing, check=False)
        run_cmd("git", "branch", "-D", BRANCH_NAME, cwd=existing, check=False)

        worktree_base.mkdir(parents=True, exist_ok=True)
        run_cmd(
            "git",
            "worktree",
            "add",
            str(worktree_dir),
            f"origin/{default_branch}",
            "-B",
            BRANCH_NAME,
            "--force",
            cwd=existing,
        )
        run_cmd(
            "git",
            "remote",
            "set-url",
            "origin",
            f"https://github.com/{org}/{repo_name}.git",
            cwd=worktree_dir,
            check=False,
        )

        print(f"    Worktree created at {worktree_dir}")
        return worktree_dir

    else:
        default_branch = get_default_branch(org, repo_name)
        clone_dir = workspace / repo_name

        run_cmd(
            "gh",
            "repo",
            "clone",
            f"{org}/{repo_name}",
            str(clone_dir),
            "--",
            "--branch",
            default_branch,
            "--single-branch",
        )
        run_cmd("git", "checkout", "-b", BRANCH_NAME, cwd=clone_dir)

        print(f"    Cloned to {clone_dir}")
        return clone_dir


def reset_repo(org: str, repo_name: str, workspace: Path) -> None:
    """Reset any local changes / worktrees for a repo and delete the branch."""
    existing = workspace / repo_name
    worktree_dir = workspace / f"{repo_name}.worktrees" / WORKTREE_NAME

    if worktree_dir.exists() and existing.is_dir():
        run_cmd(
            "git",
            "worktree",
            "remove",
            "--force",
            str(worktree_dir),
            cwd=existing,
            check=False,
        )
        print(f"    Removed worktree for {repo_name}")

    if existing.is_dir() and (existing / ".git").exists():
        result = run_cmd("git", "branch", "--show-current", cwd=existing, check=False)
        if result.stdout.strip() == BRANCH_NAME:
            default_branch = get_default_branch(org, repo_name)
            run_cmd("git", "checkout", default_branch, cwd=existing, check=False)

        result = run_cmd("git", "branch", "-D", BRANCH_NAME, cwd=existing, check=False)
        if result.returncode == 0:
            print(f"    Deleted local branch {BRANCH_NAME}")

        result = run_cmd(
            "git",
            "push",
            "origin",
            "--delete",
            BRANCH_NAME,
            cwd=existing,
            check=False,
        )
        if result.returncode == 0:
            print(f"    Deleted remote branch {BRANCH_NAME}")


def migrate_repo(work_dir: Path) -> bool:
    """Run ``gh actions-lock`` and add the verify workflow.

    ``gh actions-lock`` writes the lockfile, rewrites inline SHA pins back to
    version tags, and (via ``--migrate-local-actions``) rewrites local
    ``./foo`` references to repo-relative ``$/foo`` so their dependencies are
    locked too. The verify workflow is added when missing and then locked by
    a second run.

    Returns True if the working tree has any changes afterwards.
    """
    run_actions_lock(*BASE_LOCK_ARGS, cwd=work_dir)

    verify_path = work_dir / VERIFY_WORKFLOW_PATH
    if not verify_path.exists():
        verify_path.parent.mkdir(parents=True, exist_ok=True)
        verify_path.write_text(VERIFY_WORKFLOW_TEMPLATE)
        print(f"    Added {VERIFY_WORKFLOW_PATH}")
        run_actions_lock(*BASE_LOCK_ARGS, cwd=work_dir)

    return has_changes(work_dir)


def verify_lockfile(work_dir: Path) -> bool:
    """Return True if ``gh actions-lock --no-fix --json`` reports a valid lockfile."""
    result = run_actions_lock(
        "--no-fix", "--json", *BASE_LOCK_ARGS, cwd=work_dir, check=False
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return isinstance(report, dict) and report.get("valid") is True


def has_changes(work_dir: Path) -> bool:
    """Check if the working directory has uncommitted or untracked changes."""
    result = run_cmd("git", "status", "--porcelain", cwd=work_dir)
    return bool(result.stdout.strip())


def commit_changes(work_dir: Path) -> None:
    """Stage and commit the lockfile and workflow changes."""
    run_cmd("git", "add", ".github/", cwd=work_dir)

    result = run_cmd("git", "diff", "--cached", "--quiet", cwd=work_dir, check=False)
    if result.returncode == 0:
        print("    No staged changes to commit")
        return

    run_cmd(
        "git",
        "commit",
        "-m",
        "Adopt gh actions-lock for GitHub Actions pinning\n\n"
        "Lock action references to commit SHAs in .github/workflows/actions.lock\n"
        "and keep version tags in uses: lines. Add a verify-lockfile workflow\n"
        "that runs gh actions-lock --verify to catch drift or tampering.",
        cwd=work_dir,
    )
    print("    Committed changes")


def push_branch(work_dir: Path) -> None:
    """Push the branch to origin."""
    run_cmd(
        "git", "push", "--set-upstream", "origin", BRANCH_NAME, "--force", cwd=work_dir
    )
    print("    Pushed branch to origin")


def find_repo_issue(org: str, repo_name: str) -> int | None:
    """Find the open lockfile adoption issue number in a repo, if any."""
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
                return issue["number"]
    except RuntimeError:
        pass
    return None


def open_pr(
    org: str, repo_name: str, work_dir: Path, issue_number: int | None = None
) -> str | None:
    """Open a PR and return the URL."""
    default_branch = get_default_branch(org, repo_name)

    existing = run_gh(
        "pr",
        "list",
        "--head",
        BRANCH_NAME,
        "--state",
        "open",
        "--json",
        "url",
        "--repo",
        f"{org}/{repo_name}",
        cwd=work_dir,
    ).strip()

    if existing and existing != "[]":
        prs = json.loads(existing)
        if prs:
            url = prs[0]["url"]
            # Update the body so it includes the latest Closes link
            pr_number = url.rstrip("/").split("/")[-1]
            run_gh(
                "pr",
                "edit",
                pr_number,
                "--body",
                pr_body(issue_number),
                "--repo",
                f"{org}/{repo_name}",
                cwd=work_dir,
            )
            print(f"    PR already exists (body updated): {url}")
            return url

    url = run_gh(
        "pr",
        "create",
        "--title",
        PR_TITLE,
        "--body",
        pr_body(issue_number),
        "--base",
        default_branch,
        "--head",
        BRANCH_NAME,
        "--repo",
        f"{org}/{repo_name}",
        cwd=work_dir,
    ).strip()

    print(f"    PR opened: {url}")
    return url


def process_repo(
    org: str,
    repo_info: dict,
    workspace: Path,
    *,
    dry_run: bool,
    no_push: bool,
    no_pr: bool,
    reset: bool,
) -> dict:
    """Process a single repo. Returns a summary dict."""
    repo_name = repo_info["name"]
    summary: dict = {"name": repo_name, "status": "unknown", "pr_url": None}

    try:
        if reset:
            reset_repo(org, repo_name, workspace)

        work_dir = ensure_repo(org, repo_name, workspace)
        changed = migrate_repo(work_dir)

        if not changed:
            print("    No changes needed")
            summary["status"] = "no_changes"
            return summary

        if dry_run:
            diff = run_cmd("git", "diff", "--stat", cwd=work_dir, check=False)
            status = run_cmd("git", "status", "--short", cwd=work_dir, check=False)
            print(f"    [dry-run] Changes:\n{diff.stdout}{status.stdout}")
            summary["status"] = "dry_run"
            return summary

        if not verify_lockfile(work_dir):
            print("    WARNING: gh actions-lock reports the lockfile is not valid")

        commit_changes(work_dir)

        if no_push:
            summary["status"] = "committed"
            return summary

        push_branch(work_dir)

        if no_pr:
            summary["status"] = "pushed"
            return summary

        issue_number = find_repo_issue(org, repo_name)
        pr_url = open_pr(org, repo_name, work_dir, issue_number)
        summary["status"] = "pr_opened"
        summary["pr_url"] = pr_url
        return summary

    except Exception as e:
        print(f"    ERROR: {e}")
        summary["status"] = f"error: {e}"
        return summary


# ---------------------------------------------------------------------------
# Subcommand entry point
# ---------------------------------------------------------------------------


def run(
    org: str,
    workspace: Path,
    results_file: Path,
    *,
    repos: list[str] | None = None,
    dry_run: bool = False,
    no_push: bool = False,
    no_pr: bool = False,
    reset: bool = False,
) -> None:
    """Run the lock command."""
    # Resolve effective flags
    if dry_run:
        no_push = True
    if no_push:
        no_pr = True

    if dry_run:
        print("Mode: DRY RUN (local changes only)")
    elif no_push:
        print("Mode: COMMIT ONLY (no push, no PR)")
    elif no_pr:
        print("Mode: COMMIT + PUSH (no PR)")
    else:
        print("Mode: FULL (commit, push, open PR)")

    if reset:
        print("Reset: YES (prior changes will be discarded)")
    print()

    pending = get_noncompliant_repos(results_file)
    if repos:
        repo_set = set(repos)
        pending = [r for r in pending if r["name"] in repo_set]
        missing = repo_set - {r["name"] for r in pending}
        if missing:
            print(
                f"Error: repos not found in the not-adopted list: {', '.join(sorted(missing))}"
            )
            print("Run the check command first, or check the names.")
            sys.exit(1)

    print(f"Found {len(pending)} repos that have not adopted gh actions-lock\n")

    summaries = []
    for i, repo_info in enumerate(sorted(pending, key=lambda r: r["name"]), 1):
        name = repo_info["name"]
        count = len(repo_info.get("external_workflows", []))
        print(f"[{i}/{len(pending)}] {name} ({count} workflows with external actions)")

        summary = process_repo(
            org,
            repo_info,
            workspace,
            dry_run=dry_run,
            no_push=no_push,
            no_pr=no_pr,
            reset=reset,
        )
        summaries.append(summary)
        print()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for s in summaries:
        status = s["status"]
        pr = f" -> {s['pr_url']}" if s.get("pr_url") else ""
        print(f"  {s['name']}: {status}{pr}")
