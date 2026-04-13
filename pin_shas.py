#!/usr/bin/env python3
"""Pin GitHub Actions to SHAs across all esphome organization repositories.

For each repo with unpinned action references, this script:
1. Clones the repo (or creates a worktree if already cloned locally)
2. Creates a branch
3. Runs a local pinning command (placeholder) to rewrite uses: refs to SHAs
4. Commits, pushes, and opens a PR

Usage:
    python pin_shas.py --dry-run          # local changes only, no commit/push/PR
    python pin_shas.py --no-push          # commit locally but don't push or open PR
    python pin_shas.py --no-pr            # commit and push but don't open PR
    python pin_shas.py --reset --dry-run  # discard prior changes, then dry-run again
    python pin_shas.py                    # full run: commit, push, open PR
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ORG = "esphome"
WORKSPACE = Path(__file__).resolve().parent.parent  # /home/jesse/workspace/esphome
BRANCH_NAME = "pin-action-shas"
RESULTS_FILE = Path(__file__).resolve().parent / "results.json"

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# Matches both step-level and job-level uses:
#   uses: actions/checkout@v4
#   uses: actions/checkout@v4.2.1
#   uses: esphome/workflows/.github/workflows/build.yml@2025.10.0
#   uses: owner/action@abc123...  # already pinned (40-char hex)
# Captures: (action_or_workflow, ref)
USES_PATTERN = re.compile(r"(uses:\s*)([^@\s]+)@(\S+)")

# A short version tag lacks a full semver: "v4", "v4.1" but NOT "v4.3.1"
SHORT_VERSION_PATTERN = re.compile(r"^v\d+(\.\d+)?$")

PR_TITLE = "Pin GitHub Actions to commit SHAs"
PR_BODY = """\
## Summary

Pin all GitHub Action and reusable workflow references to their full commit SHAs
instead of mutable tags or branch names.

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

- [GitHub Blog: Four tips to keep your GitHub Actions workflows secure](https://github.blog/open-source/four-tips-to-keep-your-github-actions-workflows-secure/#use-specific-action-version-tags)
- [GitHub Docs: Security hardening for GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions)
- [GitHub Docs: Enforcing SHA pinning for actions](https://docs.github.com/en/organizations/managing-organization-settings/disabling-or-limiting-github-actions-for-your-organization#requiring-workflows-to-use-pinned-versions-of-actions)
"""


def run(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess command."""
    result = subprocess.run(
        args,
        cwd=cwd,
        capture_output=capture,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        cmd = " ".join(args)
        stderr = result.stderr if capture else ""
        raise RuntimeError(f"Command failed: {cmd}\n{stderr}")
    return result


def run_gh(*args: str, cwd: Path | None = None) -> str:
    """Run a gh CLI command and return stdout."""
    return run("gh", *args, cwd=cwd).stdout


def get_default_branch(repo_name: str) -> str:
    """Get the default branch for a repo."""
    return run_gh("api", f"repos/{ORG}/{repo_name}", "--jq", ".default_branch").strip()


def get_unpinned_repos() -> list[dict]:
    """Load results.json and return repos that have unpinned actions."""
    if not RESULTS_FILE.exists():
        print(f"Error: {RESULTS_FILE} not found. Run check_sha_pinning.py first.")
        sys.exit(1)

    results = json.loads(RESULTS_FILE.read_text())
    return [r for r in results if r.get("all_pinned") is False]


def ensure_repo(repo_name: str) -> Path:
    """Ensure we have a working directory for the repo.

    If a worktree already exists at WORKSPACE/<repo_name>.worktrees/pin-shas,
    reuse it (allows incremental commits on re-runs).
    If the repo is cloned at WORKSPACE/<repo_name>, create a new worktree.
    Otherwise, do a fresh clone.

    Returns the Path to the working directory.
    """
    existing = WORKSPACE / repo_name
    worktree_base = WORKSPACE / f"{repo_name}.worktrees"
    worktree_dir = worktree_base / "pin-shas"

    if existing.is_dir() and (existing / ".git").exists():
        # Reuse existing worktree if present
        if worktree_dir.exists():
            print(f"    Reusing worktree at {worktree_dir}")
            return worktree_dir

        # Repo exists locally — create a new worktree
        default_branch = get_default_branch(repo_name)

        # Fetch latest default branch
        run("git", "fetch", "origin", default_branch, cwd=existing)

        # Delete stale branch if it exists (leftover without worktree)
        run("git", "branch", "-D", BRANCH_NAME, cwd=existing, check=False)

        # Create worktree from origin/<default_branch>
        worktree_base.mkdir(parents=True, exist_ok=True)
        run(
            "git",
            "worktree",
            "add",
            str(worktree_dir),
            f"origin/{default_branch}",
            "-b",
            BRANCH_NAME,
            "--force",
            cwd=existing,
        )

        # Set upstream so we push to the right place
        run(
            "git",
            "remote",
            "set-url",
            "origin",
            f"https://github.com/{ORG}/{repo_name}.git",
            cwd=worktree_dir,
            check=False,
        )

        print(f"    Worktree created at {worktree_dir}")
        return worktree_dir

    else:
        # Fresh clone
        default_branch = get_default_branch(repo_name)
        clone_dir = WORKSPACE / repo_name

        run(
            "gh",
            "repo",
            "clone",
            f"{ORG}/{repo_name}",
            str(clone_dir),
            "--",
            "--branch",
            default_branch,
            "--single-branch",
        )

        # Create the branch
        run("git", "checkout", "-b", BRANCH_NAME, cwd=clone_dir)

        print(f"    Cloned to {clone_dir}")
        return clone_dir


def reset_repo(repo_name: str) -> None:
    """Reset any local changes / worktrees for a repo and delete the branch."""
    existing = WORKSPACE / repo_name
    worktree_dir = WORKSPACE / f"{repo_name}.worktrees" / "pin-shas"

    # Remove worktree if present
    if worktree_dir.exists() and existing.is_dir():
        run(
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
        # If the main checkout is on the pin branch, switch off it first
        result = run("git", "branch", "--show-current", cwd=existing, check=False)
        if result.stdout.strip() == BRANCH_NAME:
            default_branch = get_default_branch(repo_name)
            run("git", "checkout", default_branch, cwd=existing, check=False)

        # Delete local branch
        result = run(
            "git",
            "branch",
            "-D",
            BRANCH_NAME,
            cwd=existing,
            check=False,
        )
        if result.returncode == 0:
            print(f"    Deleted local branch {BRANCH_NAME}")

        # Delete remote branch
        result = run(
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
    """Extract owner/repo from an action or reusable workflow reference.

    'actions/checkout'                          -> 'actions/checkout'
    'esphome/workflows/.github/workflows/b.yml' -> 'esphome/workflows'
    """
    parts = action.split("/")
    return f"{parts[0]}/{parts[1]}"


def resolve_ref_to_sha(repo_slug: str, ref: str) -> str | None:
    """Resolve a tag or branch name to its commit SHA.

    Handles both lightweight tags (type=commit) and annotated tags
    (type=tag, requires dereferencing to the underlying commit).
    Also handles branch refs.
    """
    # Try as a tag first, then as a branch
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
            # Annotated tag — dereference to get the commit
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
    specific tag like 'v4.3.1' pointing to the same commit. If so, return it.
    Otherwise return the original ref.

    For non-version refs (branches, calver like '2025.10.0'), return as-is.
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

    # Filter to tags under the same major version prefix
    prefix = original_ref.rstrip(".") + "."
    candidates = [t for t in tags if t == original_ref or t.startswith(prefix)]
    if not candidates:
        return original_ref

    # Pick the most specific (most dots) — "v4.3.1" over "v4.3" over "v4"
    candidates.sort(key=lambda t: t.count("."), reverse=True)
    return candidates[0]


def pin_actions(work_dir: Path) -> bool:
    """Rewrite all action and reusable workflow refs to SHA pins.

    For each `uses: owner/action@ref` or `uses: owner/repo/path@ref`:
    - Skip if already pinned to a 40-char SHA
    - Skip local actions (./) and docker:// references
    - Resolve the ref (tag or branch) to its commit SHA
    - Find the most specific version tag for the comment
    - Rewrite to: `uses: owner/action@<sha> # <version>`

    Returns True if any files were modified.
    """
    workflows_dir = work_dir / ".github" / "workflows"
    if not workflows_dir.is_dir():
        print("    No .github/workflows directory found")
        return False

    # Cache: (repo_slug, ref) -> (sha, version_tag)
    cache: dict[tuple[str, str], tuple[str, str] | None] = {}

    for wf_file in sorted(workflows_dir.glob("*.y*ml")):
        content = wf_file.read_text()
        new_content = content

        for match in USES_PATTERN.finditer(content):
            prefix = match.group(1)  # "uses: " (with any whitespace)
            action = match.group(2)  # "actions/checkout" or "owner/repo/.github/..."
            ref = match.group(3)  # "v4", "2025.10.0", "main", or a SHA

            # Strip trailing comment if the ref already has one
            # e.g., from a previous partial run: "v4 # v4.3.1" -> ref="v4"
            if " " in ref:
                ref = ref.split()[0]

            # Skip local actions and docker references
            if action.startswith("./") or action.startswith("docker://"):
                continue

            # Skip already-pinned refs
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

    # Final check via git diff in case write didn't change anything
    result = run("git", "diff", "--quiet", cwd=work_dir, check=False)
    return result.returncode != 0


def has_changes(work_dir: Path) -> bool:
    """Check if the working directory has uncommitted changes."""
    result = run("git", "diff", "--quiet", cwd=work_dir, check=False)
    return result.returncode != 0


def commit_changes(work_dir: Path, repo_name: str) -> None:
    """Stage and commit the pinning changes."""
    run("git", "add", ".github/workflows/", cwd=work_dir)

    # Check if there's anything staged
    result = run("git", "diff", "--cached", "--quiet", cwd=work_dir, check=False)
    if result.returncode == 0:
        print("    No staged changes to commit")
        return

    run(
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
    run("git", "push", "--set-upstream", "origin", BRANCH_NAME, "--force", cwd=work_dir)
    print("    Pushed branch to origin")


def open_pr(work_dir: Path, repo_name: str) -> str | None:
    """Open a PR and return the URL."""
    default_branch = get_default_branch(repo_name)

    # Check if a PR already exists for this branch
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
        f"{ORG}/{repo_name}",
        cwd=work_dir,
    ).strip()

    if existing and existing != "[]":
        prs = json.loads(existing)
        if prs:
            url = prs[0]["url"]
            print(f"    PR already exists: {url}")
            return url

    url = run_gh(
        "pr",
        "create",
        "--title",
        PR_TITLE,
        "--body",
        PR_BODY,
        "--base",
        default_branch,
        "--head",
        BRANCH_NAME,
        "--repo",
        f"{ORG}/{repo_name}",
        cwd=work_dir,
    ).strip()

    print(f"    PR opened: {url}")
    return url


def process_repo(
    repo_info: dict,
    *,
    dry_run: bool,
    no_push: bool,
    no_pr: bool,
    reset: bool,
) -> dict:
    """Process a single repo. Returns a summary dict."""
    repo_name = repo_info["name"]
    summary = {"name": repo_name, "status": "unknown", "pr_url": None}

    try:
        # Reset if requested
        if reset:
            reset_repo(repo_name)

        # Get a working directory
        work_dir = ensure_repo(repo_name)

        # Run the pinning command
        changed = pin_actions(work_dir)

        if not changed and not has_changes(work_dir):
            print("    No changes needed")
            summary["status"] = "no_changes"
            return summary

        if dry_run:
            # Show what would change
            diff = run("git", "diff", "--stat", cwd=work_dir, check=False)
            print(f"    [dry-run] Changes:\n{diff.stdout}")
            summary["status"] = "dry_run"
            return summary

        # Commit
        commit_changes(work_dir, repo_name)

        if no_push:
            summary["status"] = "committed"
            return summary

        # Push
        push_branch(work_dir)

        if no_pr:
            summary["status"] = "pushed"
            return summary

        # Open PR
        pr_url = open_pr(work_dir, repo_name)
        summary["status"] = "pr_opened"
        summary["pr_url"] = pr_url
        return summary

    except Exception as e:
        print(f"    ERROR: {e}")
        summary["status"] = f"error: {e}"
        return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pin GitHub Actions to commit SHAs across esphome repos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Make local changes only — no commit, push, or PR.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Commit locally but do not push or open a PR.",
    )
    parser.add_argument(
        "--no-pr",
        action="store_true",
        help="Commit and push but do not open a PR.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Discard prior local changes/worktrees before processing.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        action="append",
        default=None,
        dest="repos",
        help="Process specific repo(s) by name. Repeatable: --repo a --repo b",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Resolve effective flags — dry-run implies no-push and no-pr
    dry_run = args.dry_run
    no_push = args.no_push or dry_run
    no_pr = args.no_pr or no_push

    if dry_run:
        print("Mode: DRY RUN (local changes only)")
    elif no_push:
        print("Mode: COMMIT ONLY (no push, no PR)")
    elif no_pr:
        print("Mode: COMMIT + PUSH (no PR)")
    else:
        print("Mode: FULL (commit, push, open PR)")

    if args.reset:
        print("Reset: YES (prior changes will be discarded)")
    print()

    # Load repo list
    unpinned = get_unpinned_repos()
    if args.repos:
        repo_set = set(args.repos)
        unpinned = [r for r in unpinned if r["name"] in repo_set]
        missing = repo_set - {r["name"] for r in unpinned}
        if missing:
            print(
                f"Error: repos not found in unpinned list: {', '.join(sorted(missing))}"
            )
            print("Run check_sha_pinning.py first, or check the names.")
            sys.exit(1)

    print(f"Found {len(unpinned)} repos with unpinned actions\n")

    summaries = []
    for i, repo_info in enumerate(sorted(unpinned, key=lambda r: r["name"]), 1):
        name = repo_info["name"]
        count = len(repo_info["not_pinned"])
        print(f"[{i}/{len(unpinned)}] {name} ({count} unpinned actions)")

        summary = process_repo(
            repo_info,
            dry_run=dry_run,
            no_push=no_push,
            no_pr=no_pr,
            reset=args.reset,
        )
        summaries.append(summary)
        print()

    # Print summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for s in summaries:
        status = s["status"]
        pr = f" -> {s['pr_url']}" if s.get("pr_url") else ""
        print(f"  {s['name']}: {status}{pr}")


if __name__ == "__main__":
    main()
