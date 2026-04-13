# workflow-shas

Track and enforce GitHub Actions SHA pinning compliance across a GitHub organization.

## Using as a GitHub Action

Other orgs can use this directly — no fork needed:

```yaml
name: Check SHA Pinning Compliance

on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Check SHA pinning compliance
        uses: esphome/workflow-shas@main
        env:
          GH_TOKEN: ${{ secrets.YOUR_TOKEN }}
        with:
          command: check
          org: your-org
          tracking-repo: your-tracking-repo
```

### Inputs

| Input | Required | Description |
|-------|----------|-------------|
| `command` | Yes | Subcommand: `check`, `pin`, or `enforce` |
| `org` | Yes | GitHub organization to operate on |
| `tracking-repo` | For `check` | Repo name for the tracking issue |
| `repos` | No | Comma-separated list of specific repos to process |
| `dry-run` | No | Set to `"true"` to run without mutations |

### Commands

**`check`** — Scan all repos, update the tracking issue, create/close sub-issues:

```yaml
- uses: esphome/workflow-shas@main
  with:
    command: check
    org: your-org
    tracking-repo: workflow-shas
```

**`enforce`** — Enable `sha_pinning_required` on repos where it's safe:

```yaml
- uses: esphome/workflow-shas@main
  with:
    command: enforce
    org: your-org
```

**`pin`** — (CLI only) Clone repos, resolve action refs to SHAs, open PRs. Best run locally since it needs a workspace for cloning.

### Token permissions

The `GH_TOKEN` environment variable must be set to a token with:
- `issues: write` on all org repos (for creating/closing sub-issues)
- `actions: write` on all org repos (for `enforce` command)
- `contents: read` on all org repos (for scanning workflows)

A [GitHub App](https://docs.github.com/en/apps/creating-github-apps) installed on the org is recommended.

## Local CLI usage

```bash
uv run workflow-shas --org your-org check --tracking-repo workflow-shas
uv run workflow-shas --org your-org enforce --dry-run
uv run workflow-shas --org your-org pin --repo some-repo --dry-run
```

## Why SHA pinning?

Referencing actions by tag (e.g., `actions/checkout@v4`) is convenient but carries a supply-chain risk: tags are mutable and can be force-pushed to point at arbitrary commits. Pinning to a full 40-character commit SHA makes the reference immutable.

- [GitHub Blog: Four tips to keep your GitHub Actions workflows secure](https://github.blog/open-source/four-tips-to-keep-your-github-actions-workflows-secure/#use-specific-action-version-tags)
- [GitHub Docs: Security hardening for GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions)
