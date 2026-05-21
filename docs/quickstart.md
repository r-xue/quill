# Quick Start

Get `quill` running in 5 minutes.

## Prerequisites

- **Python** ≥ 3.10
- **Pixi** ([install guide](https://pixi.sh/latest/#installation)) — or plain `pip`
- A **Jira Personal Access Token** with write access to the target project(s)
- A **Jira custom field** for storing the GitHub issue URL (see [Jira Setup](jira-setup.md))
- (Optional) A **GitHub Personal Access Token** — not required for public repos, but recommended for rate limits

## 1. Clone & Install

```bash
git clone https://github.com/your-org/quill.git
cd quill
pixi install
```

Or with pip:

```bash
pip install -e .
```

## 2. Generate Config Files

```bash
pixi run quill init
```

This creates two files:

| File | Purpose | Commit? |
|---|---|---|
| `quill.toml` | Project settings (repos, Jira server, etc.) | ✅ Yes |
| `.env` | Secrets (tokens) | ❌ No (git-ignored) |

## 3. Edit Configuration

**`quill.toml`** — set your Jira server and repos:

```toml
jira_server = "https://jira.example.com"
jira_github_link_field = "customfield_10200"

[[repos]]
owner = "your-org"
repo = "your-repo"
jira_project = "CAS"

[repos.issue_filter]
state = "open"
since = "2024-01-01"
```

**`.env`** — add your tokens:

```bash
QUILL_JIRA_TOKEN=your_jira_pat
# Optional: QUILL_GITHUB_TOKEN=ghp_...
```

Or set them as environment variables directly:

```bash
export QUILL_JIRA_TOKEN=your_jira_pat
```

## 4. Test Connectivity

```bash
pixi run quill check
```

You should see green checkmarks for both GitHub and Jira.

## 5. Dry-Run Sync

Preview what quill would do:

```bash
pixi run quill sync --dry-run
```

## 6. Real Sync

```bash
pixi run quill sync
```

## Configuration Precedence

Settings are resolved from four layers (highest priority first):

1. `QUILL_*` environment variables
2. `./quill.toml` + `./.env` (project)
3. `~/.quill/config.toml` (user defaults)
4. Package defaults

See the full [Configuration Reference](configuration.md) for details.

## Next Steps

- [Configuration Reference](configuration.md) — all settings explained
- [Jira Setup](jira-setup.md) — create the custom field
- [GitHub Actions](github-actions.md) — automate scheduled syncing
