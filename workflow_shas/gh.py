"""GitHub CLI and subprocess helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def run_cmd(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run a subprocess command."""
    result = subprocess.run(
        args,
        cwd=cwd,
        capture_output=capture,
        text=True,
    )
    if check and result.returncode != 0:
        cmd = " ".join(args)
        stderr = result.stderr if capture else ""
        raise RuntimeError(f"Command failed: {cmd}\n{stderr}")
    return result


def run_gh(*args: str, cwd: Path | None = None) -> str:
    """Run a gh CLI command and return stdout."""
    return run_cmd("gh", *args, cwd=cwd).stdout


def gh_api_json(endpoint: str, **kwargs: str) -> dict | list:
    """Call the GitHub REST API and return parsed JSON."""
    args = ["api", endpoint]
    for k, v in kwargs.items():
        args.extend([f"--{k}", v])
    return json.loads(run_gh(*args))
