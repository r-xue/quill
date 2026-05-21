# CLI Reference

`quill` uses standard `argparse` subcommands. Configuration is auto-discovered from the 4-layer hierarchy — no `--config` flag needed.

---

## Global Options

| Flag | Description |
|---|---|
| `--version` | Print the quill version and exit. |
| `-h`, `--help` | Show help for any command. |

## Configuration Auto-Discovery

All commands automatically load settings from (highest priority first):

1. `QUILL_*` environment variables
2. `./quill.toml` (project config)
3. `./.env` (project secrets)
4. `~/.quill/config.toml` (user defaults)
5. Package defaults

No `--config` flag is needed. Just `cd` into your project directory and run.

---

## `quill sync`

Synchronize GitHub issues and comments to Jira.

```bash
quill sync [OPTIONS]
```

| Flag | Type | Default | Description |
|---|---|---|---|
| `-r`, `--repo` | string | *(all)* | Sync only a specific repository (format: `owner/repo`). Must match a `[[repos]]` entry in config. |
| `-d`, `--dry-run` | flag | `false` | Preview what would be created/updated without writing to Jira. |

### Examples

```bash
# Sync all configured repos
quill sync

# Sync only one repo
quill sync --repo your-org/your-repo

# Dry run
quill sync --dry-run

# Override via env var
QUILL_DRY_RUN=true quill sync
```

### What happens during sync

For each configured repository:

1. **Query Jira** via JQL to build a lookup of already-synced issues (keyed by GitHub URL in the custom field).
2. **Fetch GitHub issues** updated since the `since` date (or all issues if not set).
3. For each GitHub issue:
   - If **not found** in the Jira lookup → **create** a new Jira issue.
   - If **found** and the content hash differs → **update** the Jira issue.
   - If **found** and the hash matches → **skip** (no changes).
4. If the GitHub issue is **closed**, attempt to **transition** the Jira issue to "Done", "Closed", or "Resolved".
5. If `sync_comments` is enabled, sync any new GitHub comments to Jira.

---

## `quill check`

Validate credentials and connectivity to both the GitHub API and the Jira API.

```bash
quill check
```

No additional flags needed. Reads tokens from `.env` / env vars automatically.

### Example Output

```
Checking GitHub connection…
✓ GitHub — authenticated as @rxue

Checking Jira connection (https://jira.example.com)…
✓ Jira — authenticated as Remy Xue

✓ All checks passed.
```

---

## `quill status`

Show a table of synced issue counts per repository. This queries Jira directly via JQL — no local state is read.

```bash
quill status
```

### Example Output

```
      quill — Synced Issues per Repository
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ GitHub Repository   ┃ Jira Project ┃ Synced Issues ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ your-org/your-repo          │ CAS          │            42 │
│ your-org/another-repo       │ PIPE         │            17 │
└─────────────────────┴──────────────┴───────────────┘
```

---

## `quill init`

Generate template `quill.toml` and `.env` files in the current directory.

```bash
quill init
```

If either file already exists, quill prompts for confirmation before overwriting.

---

## Using with Pixi

If you installed quill with Pixi, prefix commands with `pixi run`:

```bash
pixi run quill sync --dry-run
pixi run quill check
pixi run quill status
```

Or use the shorthand tasks defined in `pyproject.toml`:

```bash
pixi run sync     # → quill sync
pixi run check    # → quill check
pixi run status   # → quill status
pixi run test     # → pytest
```
