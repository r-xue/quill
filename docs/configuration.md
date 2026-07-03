# Configuration Reference

`quill` uses a **4-layer hierarchical configuration** (same pattern as git, pip, and [ragdoll](https://github.com/nrao/ragdoll)):

| Priority | Source | Purpose |
|:---:|---|---|
| 1 (highest) | `QUILL_*` environment variables | Ephemeral overrides, CI/CD |
| 2a | `./quill.toml` | Project-level settings (safe to commit) |
| 2b | `./.env` | Project-level secrets (git-ignored) |
| 3 | `~/.quill/config.toml` | Personal defaults shared across projects |
| 4 (lowest) | Package defaults | Sensible fallbacks |

Most-specific scope wins. For example, `QUILL_DRY_RUN=true` overrides `dry_run = false` in your `quill.toml`.

---

## Quick Setup

```bash
# Generate template files
quill init
# → creates ./quill.toml and ./.env
```

---

## Config File Formats

### `./quill.toml` — Project Config

```toml
# Safe to commit — no secrets here
jira_server = "https://jira.example.com"
jira_verify_ssl = true
jira_default_issue_type = "Task"
jira_github_link_field = "customfield_10200"

dry_run = false
batch_size = 50

[[repos]]
owner = "your-org"
repo = "your-repo"
jira_project = "CAS"
sync_labels = true
sync_comments = true

[repos.issue_filter]
state = "open"
labels = ["bug", "enhancement"]
since = "2024-01-01"

[[repos]]
owner = "your-org"
repo = "pipeline"
jira_project = "PIPE"
```

### `./.env` — Project Secrets

```bash
# Git-ignored — never commit
QUILL_GITHUB_TOKEN=ghp_...
QUILL_JIRA_TOKEN=your_jira_pat
```

### `~/.quill/config.toml` — User Defaults

```toml
# Personal defaults shared across all projects
jira_server = "https://jira.example.com"
jira_verify_ssl = true
jira_github_link_field = "customfield_10200"
```

---

## Full Settings Reference

| Setting | Env Var | Type | Default | Description |
|---|---|---|---|---|
| `github_token` | `QUILL_GITHUB_TOKEN` | string | `null` | GitHub PAT. Optional for public repos; recommended for rate limits (60 → 5,000 req/hr). |
| `jira_server` | `QUILL_JIRA_SERVER` | string | `https://jira.example.com` | Base URL of your Jira Server/Data Center instance. |
| `jira_token` | `QUILL_JIRA_TOKEN` | string | `""` | Jira Personal Access Token with write access. |
| `jira_verify_ssl` | `QUILL_JIRA_VERIFY_SSL` | bool | `true` | Verify TLS certificates. Set `false` for self-signed certs. |
| `jira_default_issue_type` | `QUILL_JIRA_DEFAULT_ISSUE_TYPE` | string | `"Task"` | Jira issue type for new issues. |
| `jira_github_link_field` | `QUILL_JIRA_GITHUB_LINK_FIELD` | string | `"customfield_10200"` | Jira custom field ID for storing the GitHub URL. See [Jira Setup](jira-setup.md). |
| `dry_run` | `QUILL_DRY_RUN` | bool | `false` | Preview changes without writing. Also toggleable via `--dry-run`. |
| `batch_size` | `QUILL_BATCH_SIZE` | int | `50` | Max issues per sync run. |
| `repos` | *(TOML only)* | list | `[]` | List of repository configs (see below). |

### `[[repos]]` — Repository Configuration

These are defined in TOML as `[[repos]]` array-of-tables. They cannot be set via environment variables (nested structure).

| Key | Type | Default | Description |
|---|---|---|---|
| `owner` | string | *required* | GitHub org or username. |
| `repo` | string | *required* | Repository name. |
| `jira_project` | string | *required* | Jira project key (e.g., `CAS`). |
| `sync_labels` | bool | `true` | Copy GitHub labels → Jira labels. |
| `sync_comments` | bool | `true` | Sync comments with attribution. |
| `sync_project_fields` | bool | `false` | Fetch GitHub Projects V2 custom fields via GraphQL and sync as Jira labels (`proj-key-val`) and description panel. |
| `issue_filter.state` | string | `"all"` | `"open"`, `"closed"`, or `"all"`. |
| `issue_filter.labels` | list[str] | `[]` | Filter: only sync issues with these labels. |
| `issue_filter.since` | string | `null` | ISO date backfill cutoff (e.g., `"2024-01-01"`). |

---

## Environment Variable Override Examples

```bash
# One-off dry run
QUILL_DRY_RUN=true quill sync

# Override Jira server for testing
QUILL_JIRA_SERVER=https://jira-staging.example.com quill sync

# Use a different GitHub token
QUILL_GITHUB_TOKEN=ghp_testing123 quill check
```

---

## Where to Put What

| What | Where | Why |
|---|---|---|
| Jira server URL, issue type, repos | `quill.toml` | Non-secret project settings, safe to commit |
| GitHub token, Jira PAT | `.env` or env vars | Secrets — never commit |
| Personal Jira defaults (server, field ID) | `~/.quill/config.toml` | Shared across projects |
| CI/CD overrides | `QUILL_*` env vars | Ephemeral, per-run |
