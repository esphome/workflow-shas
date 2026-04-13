"""Enable SHA pinning enforcement on repos where it's safe to do so.

"Safe" means the repo either has no workflows, has workflows with no
external action references, or is already fully SHA-pinned.
"""

from __future__ import annotations

from ..gh import run_gh
from ..scan import check_repo, get_repos


def enable_sha_pinning(org: str, repo_name: str) -> None:
    """Enable sha_pinning_required for a repo via the Actions permissions API."""
    run_gh(
        "api",
        "--method",
        "PUT",
        f"repos/{org}/{repo_name}/actions/permissions",
        "-F",
        "enabled=true",
        "-f",
        "allowed_actions=all",
        "-F",
        "sha_pinning_required=true",
    )


# ---------------------------------------------------------------------------
# Subcommand entry point
# ---------------------------------------------------------------------------


def run(
    org: str,
    *,
    repos: list[str] | None = None,
    dry_run: bool = False,
) -> None:
    """Run the enforce command."""
    if repos:
        repo_names = repos
        print(f"Checking {len(repo_names)} specified repos...\n")
    else:
        print("Fetching repository list...")
        all_repos = get_repos(org)
        repo_names = sorted(r["name"] for r in all_repos)
        print(f"Found {len(repo_names)} active repositories in {org}\n")

    safe: list[tuple[dict, str]] = []
    for i, name in enumerate(sorted(repo_names), 1):
        print(f"  [{i}/{len(repo_names)}] {name}...", end=" ", flush=True)
        result = check_repo(org, name)

        if result["sha_pinning_required"]:
            print("already enforced")
            continue

        if not result["has_workflows"]:
            print("no workflows -> safe")
            safe.append((result, "no workflows"))
        elif result["all_pinned"] is None:
            print("no external actions -> safe")
            safe.append((result, "no external actions"))
        elif result["all_pinned"]:
            print("fully pinned -> safe")
            safe.append((result, "fully pinned"))
        else:
            print("has unpinned actions -> skip")

    print()

    if not safe:
        print("No repos need enforcement enabled.")
        return

    print(f"{len(safe)} repos safe to enforce:\n")

    errors = 0
    for result, reason in sorted(safe, key=lambda x: x[0]["name"]):
        name = result["name"]
        if dry_run:
            print(f"  [dry-run] {name} ({reason})")
        else:
            try:
                enable_sha_pinning(org, name)
                print(f"  {name} ({reason}) -- enabled")
            except RuntimeError as e:
                print(f"  {name} ({reason}) -- ERROR: {e}")
                errors += 1

    if dry_run:
        print(f"\n[dry-run] Would enable enforcement on {len(safe)} repos.")
    else:
        enabled = len(safe) - errors
        print(f"\nEnabled enforcement on {enabled}/{len(safe)} repos.")
