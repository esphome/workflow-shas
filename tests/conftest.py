"""Shared test helpers: fake ``gh`` routing and fake repo state."""

from __future__ import annotations

import base64
import subprocess
from collections.abc import Callable
from typing import Any

import pytest


class GhRouter:
    """Callable stand-in for ``run_gh`` that routes on argument prefixes."""

    def __init__(self) -> None:
        self.routes: list[tuple[tuple[str, ...], Any]] = []
        self.calls: list[tuple[str, ...]] = []

    def add(self, *prefix: str, result: Any = "") -> GhRouter:
        """Route calls starting with ``prefix`` to ``result``.

        ``result`` may be a string, an exception (raised), or a callable
        receiving the full argument tuple.
        """
        self.routes.append((prefix, result))
        return self

    def __call__(self, *args: str, **kwargs: Any) -> str:
        self.calls.append(args)
        for prefix, result in self.routes:
            if args[: len(prefix)] == prefix:
                if isinstance(result, Exception):
                    raise result
                if callable(result):
                    return result(args)
                return result
        raise AssertionError(f"Unrouted gh call: {args}")

    def called_with(self, *prefix: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[: len(prefix)] == prefix]


@pytest.fixture
def router() -> GhRouter:
    return GhRouter()


def cp(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    """Build a CompletedProcess."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def make_repo_gh(
    *,
    workflows: dict[str, str] | None = None,
    lockfile: str = "",
    action_files: list[str] | None = None,
    sha_required: str = "false",
) -> Callable[..., str]:
    """Build a fake ``run_gh`` serving the state of a single repo."""
    workflows = workflows or {}
    action_files = action_files or []

    def fake(*args: str, **kwargs: Any) -> str:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint.endswith("/actions/permissions"):
            if sha_required == "error":
                raise RuntimeError("boom")
            return sha_required + "\n"
        if endpoint.endswith("/contents/.github/workflows"):
            if not workflows:
                raise RuntimeError("404")
            return "\n".join(workflows) + "\n"
        if "git/trees/HEAD" in endpoint:
            return "\n".join(action_files) + "\n"
        marker = "/contents/"
        if marker in endpoint:
            path = endpoint.split(marker, 1)[1]
            if path == ".github/workflows/actions.lock":
                content = lockfile
            elif path.startswith(".github/workflows/"):
                content = workflows.get(path.rsplit("/", 1)[1], "")
            else:
                content = ""
            if not content:
                raise RuntimeError("404")
            return base64.b64encode(content.encode()).decode()
        raise AssertionError(f"Unrouted gh call: {args}")

    return fake
