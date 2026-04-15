"""Pin GitHub Actions to SHAs across organization repositories.

For each repo with unpinned action references:
1. Clones the repo (or reuses an existing worktree)
2. Creates a branch
3. Resolves all action/workflow refs to commit SHAs inline
4. Commits, pushes, and opens a PR
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from ..gh import run_cmd, run_gh
from ..scan import SHA_PATTERN
from .check import SUB_ISSUE_TITLE

BRANCH_NAME = "pin-action-shas"

# Matches both step-level and job-level uses: — captures the prefix so
# the replacement can preserve original whitespace.
USES_PATTERN = re.compile(r"(uses:\s*)([^@\s]+)@(\S+)")

# A short version tag lacks a full semver: "v4", "v4.1" but NOT "v4.3.1"
SHORT_VERSION_PATTERN = re.compile(r"^v\d+(\.\d+)?$")

PR_TITLE = "Pin GitHub Actions to commit SHAs"


def pr_body(issue_number: int | None = None) -> str:
    """Generate the PR body, optionally linking to a tracking issue."""
    closes = ""
    if issue_number is not None:
        closes = f"\n\nCloses #{issue_number}\n"

    return f"""\
## Summary

Pin all GitHub Action and reusable workflow references to their full commit SHAs
instead of mutable tags or branch names.{closes}

## Why?

Referencing actions by tag (e.g., `actions/checkout@v4`) is convenient but
carries a supply-chain risk: tags are mutable and can be force-pushed to point
at arbitrary commits. If an action's tag is compromised, every workflow that
references it by tag will silently run the attacker's code.

Pinning to a full 40-character commit SHA (e.g.,
`actions/checkout@11bd719...`) makes the reference immutable. Even if a tag is
tampered with, workflows pinned to a SHA will continue to use the exact code
that was reviewed and trusted.

A version comment is included next to each SHA for readability
(e.g., `actions/checkout@11bd719... # v4.2.2`).

## References

- [GitHub Blog: Four tips to keep your GitHub Actions workflows secure]\
(https://github.blog/open-source/four-tips-to-keep-your-github-actions-workflows-secure/\
#use-specific-action-version-tags)
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


def get_unpinned_repos(results_file: Path) -> list[dict]:
    """Load results.json and return repos that have unpinned actions."""
    if not results_file.exists():
        print(f"Error: {results_file} not found. Run the check command first.")
        sys.exit(1)

    results = json.loads(results_file.read_text())
    return [r for r in results if r.get("all_pinned") is False]


def ensure_repo(org: str, repo_name: str, workspace: Path) -> Path:
    """Ensure we have a working directory for the repo.

    Reuses existing worktrees for incremental commits on re-runs.
    Returns the Path to the working directory.
    """
    existing = workspace / repo_name
    worktree_base = workspace / f"{repo_name}.worktrees"
    worktree_dir = worktree_base / "pin-shas"

    if existing.is_dir() and (existing / ".git").exists():
        if worktree_dir.exists():
            print(f"    Reusing worktree at {worktree_dir}")
            return worktree_dir

        default_branch = get_default_branch(org, repo_name)
        run_cmd("git", "fetch", "origin", default_branch, cwd=existing)

        # Ensure the main clone isn't sitting on the pin branch
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
    worktree_dir = workspace / f"{repo_name}.worktrees" / "pin-shas"

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


def _repo_slug(action: str) -> str:
    """Extract owner/repo from an action or reusable workflow reference."""
    parts = action.split("/")
    return f"{parts[0]}/{parts[1]}"


def resolve_ref_to_sha(repo_slug: str, ref: str) -> str | None:
    """Resolve a tag or branch name to its commit SHA.

    Handles both lightweight and annotated tags, and branch refs.
    """
    for ref_type in ("tags", "heads"):
        try:
            output = run_gh(
                "api",
                f"repos/{repo_slug}/git/ref/{ref_type}/{ref}",
                "--jq",
                ".object.type,.object.sha",
            )
        except RuntimeError:
            continue

        lines = output.strip().split("\n")
        if len(lines) != 2:
            continue

        obj_type, obj_sha = lines[0].strip(), lines[1].strip()

        if obj_type == "commit":
            return obj_sha

        if obj_type == "tag":
            try:
                commit_sha = run_gh(
                    "api",
                    f"repos/{repo_slug}/git/tags/{obj_sha}",
                    "--jq",
                    ".object.sha",
                ).strip()
                return commit_sha
            except RuntimeError:
                return None

    return None


def resolve_full_version_tag(repo_slug: str, sha: str, original_ref: str) -> str:
    """Find the most specific version tag pointing at a given SHA.

    Given that we resolved 'v4' to SHA abc123, check if there's a more
    specific tag like 'v4.3.1' pointing to the same commit.
    """
    if not SHORT_VERSION_PATTERN.match(original_ref):
        return original_ref

    try:
        output = run_gh(
            "api",
            f"repos/{repo_slug}/tags",
            "--paginate",
            "--jq",
            f'.[] | select(.commit.sha == "{sha}") | .name',
        )
    except RuntimeError:
        return original_ref

    tags = [t.strip() for t in output.strip().split("\n") if t.strip()]
    if not tags:
        return original_ref

    prefix = original_ref.rstrip(".") + "."
    candidates = [t for t in tags if t == original_ref or t.startswith(prefix)]
    if not candidates:
        return original_ref

    candidates.sort(key=lambda t: t.count("."), reverse=True)
    return candidates[0]


def _pin_file(
    wf_file: Path,
    cache: dict[tuple[str, str], tuple[str, str] | None],
) -> bool:
    """Pin action refs in a single file. Returns True if the file was modified."""
    content = wf_file.read_text()
    new_content = content

    for match in USES_PATTERN.finditer(content):
        prefix = match.group(1)
        action = match.group(2)
        ref = match.group(3)

        if " " in ref:
            ref = ref.split()[0]

        if action.startswith("./") or action.startswith("docker://"):
            continue

        if SHA_PATTERN.match(ref):
            continue

        repo_slug = _repo_slug(action)
        cache_key = (repo_slug, ref)

        if cache_key not in cache:
            sha = resolve_ref_to_sha(repo_slug, ref)
            if sha is None:
                print(f"    WARNING: could not resolve {action}@{ref}")
                cache[cache_key] = None
            else:
                version_tag = resolve_full_version_tag(repo_slug, sha, ref)
                cache[cache_key] = (sha, version_tag)

        resolved = cache[cache_key]
        if resolved is None:
            continue

        sha, version_tag = resolved
        old_text = f"{prefix}{action}@{match.group(3)}"
        new_text = f"{prefix}{action}@{sha}  # {version_tag}"
        new_content = new_content.replace(old_text, new_text, 1)

        if version_tag != ref:
            print(
                f"    {wf_file.name}: {action}@{ref} -> @{sha[:12]}... # {version_tag}"
            )
        else:
            print(f"    {wf_file.name}: {action}@{ref} -> @{sha[:12]}... # {ref}")

    if new_content != content:
        wf_file.write_text(new_content)
        return True
    return False


def pin_actions(work_dir: Path) -> bool:
    """Rewrite all action and reusable workflow refs to SHA pins.

    Processes .github/workflows/*.yml and any action.yml/action.yaml
    files found anywhere in the repo.

    Returns True if any files were modified.
    """
    cache: dict[tuple[str, str], tuple[str, str] | None] = {}

    files_to_pin: list[Path] = []

    # Workflow files
    workflows_dir = work_dir / ".github" / "workflows"
    if workflows_dir.is_dir():
        files_to_pin.extend(sorted(workflows_dir.glob("*.y*ml")))

    # Action definition files anywhere in the repo
    for action_file in sorted(work_dir.rglob("action.y*ml")):
        if action_file.name in ("action.yml", "action.yaml"):
            files_to_pin.append(action_file)

    if not files_to_pin:
        print("    No workflow or action files found")
        return False

    for f in files_to_pin:
        _pin_file(f, cache)

    # Check via git diff whether anything actually changed on disk
    result = run_cmd("git", "diff", "--quiet", cwd=work_dir, check=False)
    return result.returncode != 0


def has_changes(work_dir: Path) -> bool:
    """Check if the working directory has uncommitted changes."""
    result = run_cmd("git", "diff", "--quiet", cwd=work_dir, check=False)
    return result.returncode != 0


def commit_changes(work_dir: Path) -> None:
    """Stage and commit the pinning changes."""
    run_cmd("git", "add", ".github/workflows/", cwd=work_dir)
    # Stage any action.yml/action.yaml files anywhere in the repo
    for action_file in work_dir.rglob("action.y*ml"):
        if action_file.name in ("action.yml", "action.yaml"):
            rel = action_file.relative_to(work_dir)
            run_cmd("git", "add", str(rel), cwd=work_dir)

    result = run_cmd("git", "diff", "--cached", "--quiet", cwd=work_dir, check=False)
    if result.returncode == 0:
        print("    No staged changes to commit")
        return

    run_cmd(
        "git",
        "commit",
        "-m",
        "Pin GitHub Actions to commit SHAs\n\n"
        "Replace mutable tag references with immutable commit SHAs\n"
        "to prevent supply-chain attacks via compromised tags.\n"
        "Version comments are preserved for readability.",
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
    """Find the open SHA pinning issue number in a repo, if any."""
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
        changed = pin_actions(work_dir)

        if not changed and not has_changes(work_dir):
            print("    No changes needed")
            summary["status"] = "no_changes"
            return summary

        if dry_run:
            diff = run_cmd("git", "diff", "--stat", cwd=work_dir, check=False)
            print(f"    [dry-run] Changes:\n{diff.stdout}")
            summary["status"] = "dry_run"
            return summary

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
    """Run the pin command."""
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

    unpinned = get_unpinned_repos(results_file)
    if repos:
        repo_set = set(repos)
        unpinned = [r for r in unpinned if r["name"] in repo_set]
        missing = repo_set - {r["name"] for r in unpinned}
        if missing:
            print(
                f"Error: repos not found in unpinned list: {', '.join(sorted(missing))}"
            )
            print("Run the check command first, or check the names.")
            sys.exit(1)

    print(f"Found {len(unpinned)} repos with unpinned actions\n")

    summaries = []
    for i, repo_info in enumerate(sorted(unpinned, key=lambda r: r["name"]), 1):
        name = repo_info["name"]
        count = len(repo_info["not_pinned"])
        print(f"[{i}/{len(unpinned)}] {name} ({count} unpinned actions)")

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
