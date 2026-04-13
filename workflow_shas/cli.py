"""CLI entrypoint with subcommands: check, pin, enforce."""

from __future__ import annotations

import argparse
from pathlib import Path

from .commands import check, enforce, pin


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="workflow-shas",
        description="Track and enforce GitHub Actions SHA pinning across a GitHub organization.",
    )
    parser.add_argument(
        "--org",
        required=True,
        help="GitHub organization to operate on.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- check ---
    check_parser = subparsers.add_parser(
        "check",
        help="Scan repos and manage tracking issues.",
    )
    check_parser.add_argument(
        "--tracking-repo",
        required=True,
        help="Repo name (not full slug) for the tracking issue (e.g. 'workflow-shas').",
    )
    check_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan only, don't create/update issues.",
    )
    check_parser.add_argument(
        "--repo",
        type=str,
        action="append",
        default=None,
        dest="repos",
        help="Process specific repo(s). Repeatable: --repo a --repo b",
    )

    # --- pin ---
    pin_parser = subparsers.add_parser(
        "pin",
        help="Pin action refs to SHAs and open PRs.",
    )
    pin_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Parent directory for repo clones/worktrees (default: cwd).",
    )
    pin_parser.add_argument(
        "--results-file",
        type=Path,
        default=Path.cwd() / "results.json",
        help="Path to results.json from a previous check run.",
    )
    pin_parser.add_argument(
        "--repo",
        type=str,
        action="append",
        default=None,
        dest="repos",
        help="Process specific repo(s). Repeatable: --repo a --repo b",
    )
    pin_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Make local changes only — no commit, push, or PR.",
    )
    pin_parser.add_argument(
        "--no-push",
        action="store_true",
        help="Commit locally but do not push or open a PR.",
    )
    pin_parser.add_argument(
        "--no-pr",
        action="store_true",
        help="Commit and push but do not open a PR.",
    )
    pin_parser.add_argument(
        "--reset",
        action="store_true",
        help="Discard prior local changes/worktrees before processing.",
    )

    # --- enforce ---
    enforce_parser = subparsers.add_parser(
        "enforce",
        help="Enable sha_pinning_required on safe repos.",
    )
    enforce_parser.add_argument(
        "--repo",
        type=str,
        action="append",
        default=None,
        dest="repos",
        help="Process specific repo(s). Repeatable: --repo a --repo b",
    )
    enforce_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List repos that would be changed without making changes.",
    )

    args = parser.parse_args(argv)

    if args.command == "check":
        check.run(
            org=args.org,
            tracking_repo=args.tracking_repo,
            repos=args.repos,
            dry_run=args.dry_run,
        )
    elif args.command == "pin":
        pin.run(
            org=args.org,
            workspace=args.workspace,
            results_file=args.results_file,
            repos=args.repos,
            dry_run=args.dry_run,
            no_push=args.no_push,
            no_pr=args.no_pr,
            reset=args.reset,
        )
    elif args.command == "enforce":
        enforce.run(
            org=args.org,
            repos=args.repos,
            dry_run=args.dry_run,
        )
