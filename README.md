# workflow-shas

Tracks GitHub Actions SHA pinning compliance across the [esphome](https://github.com/esphome) organization.

## How it works

A [daily workflow](.github/workflows/check.yml) scans all active repositories in the esphome organization and checks whether their GitHub Actions workflow files reference actions and reusable workflows by immutable commit SHA or by mutable tag/branch.

Results are published as a pinned [tracking issue](../../issues) in this repository, with a sub-issue opened in each non-compliant repo. When a repo becomes fully compliant, its sub-issue is automatically closed.

## Scripts

- **`check_sha_pinning.py`** — Scans all org repos and manages the tracking issue + sub-issues.
- **`pin_shas.py`** — Batch tool to clone repos, pin action refs to SHAs, and open PRs.

## Why SHA pinning?

Referencing actions by tag (e.g., `actions/checkout@v4`) is convenient but carries a supply-chain risk: tags are mutable and can be force-pushed to point at arbitrary commits. Pinning to a full 40-character commit SHA makes the reference immutable.

- [GitHub Blog: Four tips to keep your GitHub Actions workflows secure](https://github.blog/open-source/four-tips-to-keep-your-github-actions-workflows-secure/#use-specific-action-version-tags)
- [GitHub Docs: Security hardening for GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions)
