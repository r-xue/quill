# 🪶 Quill (Query-based Unified Issue Lightweight Linker)

`quill` syncs GitHub issues and comments from **multiple public repositories** to an **on-premise Jira** instance. It is fully **stateless** — no local database or state file needed.

## How the stateless design works

Each synced Jira issue gets the original GitHub issue URL stored in a Jira **custom field** (e.g., `customfield_10200`). On every sync run, `quill` queries Jira via JQL to discover which issues are already synced, then creates or updates as needed.

A content hash is embedded as an invisible footer in the Jira description (`<!-- quill:sha256:... -->`), enabling fast change detection without full-text diffing.

## Features

- **Multi-repository sync**: Map multiple GitHub repos to specific Jira projects.
- **Stateless**: No local database — works natively in GitHub Actions without caching hacks.
- **Incremental updates**: SHA256 content hashes detect changes to title, body, labels, or state.
- **State transitions**: Optionally closes Jira issues when GitHub issues are closed.
- **Comment sync**: Comments are attributed (e.g., "*@user* commented on GitHub").
- **Dry-run mode**: Preview changes without writing to Jira.
- **Minimal dependencies**: PyGithub, python-jira, pydantic, pyyaml, rich, and stdlib argparse.

## Installation

```bash
pixi install
```

This creates a Conda environment, installs all dependencies, and links the `quill` package in editable mode.

## Configuration

`quill` is configured via a YAML file (see `quill.yaml.example`):

```yaml
github:
  token: ${GITHUB_TOKEN}
  repos:
    - owner: your-org
      repo: your-repo
      jira_project: CAS
      sync_labels: true
      sync_comments: true
      issue_filter:
        labels: ["bug", "enhancement"]
        state: open
        since: "2024-01-01"

jira:
  server: https://jira.example.com
  token: ${JIRA_PAT}
  verify_ssl: true
  default_issue_type: Task
  github_link_field: customfield_10200   # custom field for GitHub URL

sync:
  dry_run: false
  batch_size: 50
```

### Jira custom field setup

You need a custom field in Jira (type: URL or Text) to store the GitHub issue link. Set `github_link_field` in the config to the field's internal ID (e.g., `customfield_10200`). Ask your Jira admin if you don't have one.

## Usage

```bash
# Generate a template config
pixi run quill init

# Test connectivity to GitHub and Jira
pixi run quill check

# Run synchronization
pixi run quill sync

# Sync a specific repo only
pixi run quill sync --repo your-org/your-repo

# Dry run — preview without writing
pixi run quill sync --dry-run

# Show synced issue counts (queries Jira)
pixi run quill status
```

Or using pixi tasks directly:

```bash
pixi run sync
pixi run check
pixi run status
pixi run test
```
