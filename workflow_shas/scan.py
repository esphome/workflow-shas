"""Repo scanning and workflow analysis."""

from __future__ import annotations

import base64
import json
import re

from .gh import run_gh

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
USES_PATTERN = re.compile(r"uses:\s*([^@\s]+)@(\S+)")


def get_repos(org: str) -> list[dict]:
    """Get all non-archived repos in the organization."""
    data = json.loads(
        run_gh("repo", "list", org, "--limit", "200", "--json", "name,isArchived")
    )
    return [r for r in data if not r["isArchived"]]


def get_sha_pinning_required(org: str, repo_name: str) -> bool | None:
    """Check if the repo has sha_pinning_required enabled."""
    try:
        output = run_gh(
            "api",
            f"repos/{org}/{repo_name}/actions/permissions",
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


def get_workflow_files(org: str, repo_name: str) -> list[str]:
    """Get list of workflow YAML file names for a repo."""
    try:
        output = run_gh(
            "api",
            f"repos/{org}/{repo_name}/contents/.github/workflows",
            "--jq",
            ".[].name",
        )
        files = [f.strip() for f in output.strip().split("\n") if f.strip()]
        return [f for f in files if f.endswith((".yml", ".yaml"))]
    except RuntimeError:
        return []


def get_action_files(org: str, repo_name: str) -> list[str]:
    """Find all action.yml / action.yaml files anywhere in the repo."""
    try:
        output = run_gh(
            "api",
            f"repos/{org}/{repo_name}/git/trees/HEAD?recursive=1",
            "-q",
            '.tree[] | select(.type == "blob") | .path',
            "--paginate",
        )
    except RuntimeError:
        return []

    return [
        p.strip()
        for p in output.strip().split("\n")
        if p.strip().endswith(("/action.yml", "/action.yaml"))
        or p.strip() in ("action.yml", "action.yaml")
    ]


def get_file_content(org: str, repo_name: str, path: str) -> str:
    """Get raw file content from a repo (base64-decoded)."""
    try:
        raw = run_gh(
            "api", f"repos/{org}/{repo_name}/contents/{path}", "--jq", ".content"
        )
        return base64.b64decode(raw.replace("\n", "")).decode("utf-8")
    except (RuntimeError, Exception):
        return ""


def analyze_workflow(content: str) -> dict:
    """Analyze a workflow file for action references.

    Returns a dict with ``sha_pinned`` and ``not_pinned`` lists.
    """
    sha_pinned: list[str] = []
    not_pinned: list[str] = []

    for match in USES_PATTERN.finditer(content):
        action = match.group(1)
        ref = match.group(2)

        # Strip inline comments (e.g., "abc123  # v4.2" -> "abc123")
        if " " in ref:
            ref = ref.split()[0]

        if action.startswith("./") or action.startswith("docker://"):
            continue

        if SHA_PATTERN.match(ref):
            sha_pinned.append(f"{action}@{ref}")
        else:
            not_pinned.append(f"{action}@{ref}")

    return {"sha_pinned": sha_pinned, "not_pinned": not_pinned}


def check_repo(org: str, repo_name: str) -> dict:
    """Check a single repo for SHA pinning status.

    Returns a dict with keys: ``name``, ``has_workflows``, ``all_pinned``,
    ``sha_pinning_required``, ``sha_pinned``, ``not_pinned``,
    ``workflow_files``, ``action_files``.
    """
    sha_pinning_required = get_sha_pinning_required(org, repo_name)
    workflow_files = get_workflow_files(org, repo_name)
    action_files = get_action_files(org, repo_name)

    if not workflow_files and not action_files:
        return {
            "name": repo_name,
            "has_workflows": False,
            "all_pinned": None,
            "sha_pinning_required": sha_pinning_required,
            "sha_pinned": [],
            "not_pinned": [],
            "workflow_files": [],
            "action_files": [],
        }

    all_sha_pinned: list[str] = []
    all_not_pinned: list[str] = []

    for wf in workflow_files:
        path = f".github/workflows/{wf}"
        content = get_file_content(org, repo_name, path)
        if not content:
            continue
        result = analyze_workflow(content)
        all_sha_pinned.extend(result["sha_pinned"])
        all_not_pinned.extend(result["not_pinned"])

    for af in action_files:
        content = get_file_content(org, repo_name, af)
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
        "action_files": action_files,
    }
