"""Repo scanning and workflow analysis."""

from __future__ import annotations

import base64
import json
import re

import yaml

from .gh import run_gh

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
USES_PATTERN = re.compile(r"uses:\s*([^@\s]+)@(\S+)")

WORKFLOWS_DIR = ".github/workflows"
LOCKFILE_PATH = f"{WORKFLOWS_DIR}/actions.lock"
VERIFY_WORKFLOW_NAME = "verify-lockfile.yml"


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


def get_lockfile(org: str, repo_name: str) -> str:
    """Get the raw ``actions.lock`` content for a repo ("" if absent)."""
    return get_file_content(org, repo_name, LOCKFILE_PATH)


def parse_lockfile(content: str) -> dict[str, set[str]]:
    """Parse an ``actions.lock`` file.

    Returns the ``workflows`` map as ``{workflow_path: {owner/repo@ref, ...}}``.
    Empty or malformed content yields an empty dict.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return {}

    if not isinstance(data, dict):
        return {}

    workflows = data.get("workflows")
    if not isinstance(workflows, dict):
        return {}

    result: dict[str, set[str]] = {}
    for path, deps in workflows.items():
        if isinstance(deps, list):
            result[str(path)] = {str(d) for d in deps}
        else:
            result[str(path)] = set()
    return result


def check_repo(org: str, repo_name: str) -> dict:
    """Check a single repo for ``gh actions-lock`` adoption.

    Returns a dict with keys: ``name``, ``has_workflows``,
    ``external_workflows``, ``has_lockfile``, ``has_verify_workflow``,
    ``locked_workflows``, ``uncovered_workflows``, ``compliant``,
    ``sha_pinning_required``, ``workflow_files``, ``action_files``.

    ``compliant`` is ``None`` when there is nothing to adopt (no workflow
    references an external action), otherwise ``True`` when the lockfile
    covers every workflow with external actions and the verify workflow
    exists.
    """
    sha_pinning_required = get_sha_pinning_required(org, repo_name)
    workflow_files = get_workflow_files(org, repo_name)
    action_files = get_action_files(org, repo_name)

    external_workflows: list[str] = []
    for wf in workflow_files:
        path = f"{WORKFLOWS_DIR}/{wf}"
        content = get_file_content(org, repo_name, path)
        if not content:
            continue
        result = analyze_workflow(content)
        if result["sha_pinned"] or result["not_pinned"]:
            external_workflows.append(path)
    external_workflows.sort()

    lockfile_content = get_lockfile(org, repo_name)
    has_lockfile = bool(lockfile_content)
    locked = parse_lockfile(lockfile_content)
    locked_workflows = sorted(locked)
    uncovered_workflows = [p for p in external_workflows if p not in locked]
    has_verify_workflow = VERIFY_WORKFLOW_NAME in workflow_files

    compliant: bool | None
    if not external_workflows:
        compliant = None
    else:
        compliant = has_lockfile and has_verify_workflow and not uncovered_workflows

    return {
        "name": repo_name,
        "has_workflows": bool(workflow_files),
        "external_workflows": external_workflows,
        "has_lockfile": has_lockfile,
        "has_verify_workflow": has_verify_workflow,
        "locked_workflows": locked_workflows,
        "uncovered_workflows": uncovered_workflows,
        "compliant": compliant,
        "sha_pinning_required": sha_pinning_required,
        "workflow_files": workflow_files,
        "action_files": action_files,
    }
