# workflow-shas

Track and enforce [`gh actions-lock`](https://github.com/github/gh-actions-lock) adoption for GitHub Actions across a GitHub organization.

## How it works

With `gh actions-lock`, workflows keep readable version tags in `uses:` (for example `actions/checkout@v7.0.1`) and the exact commit SHA of every direct and transitive action is recorded in a machine-generated lockfile at `.github/workflows/actions.lock`. A per-repo `.github/workflows/verify-lockfile.yml` workflow runs `gh actions-lock --verify` on every pull request and push to catch drift or tampering.

The lockfile satisfies GitHub's `sha_pinning_required` setting, so enforcing that setting stays meaningful: it now means "the lockfile is present and complete" rather than "inline SHAs everywhere".

A repository is **adopted** (compliant) when, for every workflow under `.github/workflows/` that references an external action:

- `.github/workflows/actions.lock` exists, and
- that workflow is listed in the lockfile, and
- `.github/workflows/verify-lockfile.yml` exists.

Repositories with no workflows, or no external actions, have nothing to adopt. Deep correctness (moved or stale pins) is left to each repository's own `verify-lockfile.yml` run. Standalone `action.yml` files are not covered by `gh actions-lock` and are not part of the compliance check.

## Using as a GitHub Action

Other orgs can use this directly - no fork needed:

```yaml
name: Check Actions Lockfile Adoption

on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Check actions lockfile adoption
        uses: esphome/workflow-shas@main
        env:
          GH_TOKEN: ${{ secrets.YOUR_TOKEN }}
        with:
          command: check
          org: your-org
          tracking-repo: your-tracking-repo
```

### Inputs

| Input           | Required                     | Description                                       |
|-----------------|------------------------------|---------------------------------------------------|
| `command`       | Yes                          | Subcommand: `check`, `lock`, or `enforce`         |
| `org`           | Yes                          | GitHub organization to operate on                 |
| `tracking-repo` | For `check` (unless dry-run) | Repo name for the tracking issue                  |
| `repos`         | No                           | Comma-separated list of specific repos to process |
| `dry-run`       | No                           | Set to `"true"` to run without mutations          |

### Commands

**`check`** - Scan all repos, update the tracking issue, create/close sub-issues:

```yaml
- uses: esphome/workflow-shas@main
  with:
    command: check
    org: your-org
    tracking-repo: workflow-shas
```

**`enforce`** - Enable `sha_pinning_required` on repos where it's safe (no workflows, no external actions, or lockfile adopted):

```yaml
- uses: esphome/workflow-shas@main
  with:
    command: enforce
    org: your-org
```

**`lock`** - (CLI only) Clone repos that have not adopted the lockfile, run `gh actions-lock` (which writes `.github/workflows/actions.lock` and rewrites inline SHA pins back to version tags), add `.github/workflows/verify-lockfile.yml`, and open PRs. Best run locally since it needs a workspace for cloning and the `gh-actions-lock` extension (`gh extension install github/gh-actions-lock`).

### Token permissions

The `GH_TOKEN` environment variable must be set to a token with:
- `issues: write` on all org repos (for creating/closing sub-issues)
- `actions: write` on all org repos (for `enforce` command)
- `contents: read` on all org repos (for scanning workflows)
- `contents: write` and `pull-requests: write` on all org repos (for `lock` command)

A [GitHub App](https://docs.github.com/en/apps/creating-github-apps) installed on the org is recommended.

## Local CLI usage

```bash
uv run workflow-shas --org your-org check --tracking-repo workflow-shas
uv run workflow-shas --org your-org check --dry-run
uv run workflow-shas --org your-org enforce --dry-run
uv run workflow-shas --org your-org lock --repo some-repo --dry-run
```

## Development

```bash
uv venv
uv sync --group dev
.venv/bin/pytest --cov=workflow_shas --cov-report=term-missing
.venv/bin/ruff check && .venv/bin/ruff format --check
```

## Why lock actions?

Referencing actions by tag (e.g., `actions/checkout@v4`) is convenient but carries a supply-chain risk: tags are mutable and can be force-pushed to point at arbitrary commits. Locking every action to a commit SHA makes the reference immutable, and `gh actions-lock` does so without cluttering workflow files with raw SHAs.

- [github/gh-actions-lock](https://github.com/github/gh-actions-lock)
- [GitHub Blog: Four tips to keep your GitHub Actions workflows secure](https://github.blog/open-source/four-tips-to-keep-your-github-actions-workflows-secure/#use-specific-action-version-tags)
- [GitHub Docs: Security hardening for GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions)
