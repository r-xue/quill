# GitHub Actions Integration

`quill` is designed to work natively with GitHub Actions — no state caching needed.

---

## Workflow File

Create `.github/workflows/sync-to-jira.yml` in your **own repository** (not in the target repos, since you don't have admin access):

```yaml
name: Sync GitHub Issues to Jira

on:
  schedule:
    - cron: '0 */4 * * *'              # every 4 hours
  workflow_dispatch:                     # manual trigger
    inputs:
      repo:
        description: 'Specific repo (e.g. your-org/your-repo), or "all"'
        default: 'all'
      dry_run:
        description: 'Dry run mode'
        type: boolean
        default: false

jobs:
  sync:
    runs-on: ubuntu-latest               # or self-hosted if Jira is behind firewall
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install quill
        run: pip install -e .

      - name: Run sync
        env:
          GITHUB_TOKEN: ${{ secrets.GH_SYNC_TOKEN }}
          JIRA_PAT: ${{ secrets.JIRA_PAT }}
        run: |
          quill sync \
            ${{ github.event.inputs.repo != 'all' && format('--repo {0}', github.event.inputs.repo) || '' }} \
            ${{ github.event.inputs.dry_run == 'true' && '--dry-run' || '' }}
```

---

## Key Points

### No state caching required

Since quill is stateless, every run queries Jira via JQL to discover existing synced issues. You **do not** need:

- `actions/cache` for a state file
- Artifact uploads/downloads
- Git commits to persist state

This makes the workflow simple and reliable.

### Secrets setup

In your repository settings (**Settings → Secrets and variables → Actions**), add:

| Secret | Value |
|---|---|
| `GH_SYNC_TOKEN` | A GitHub PAT with `public_repo` scope. Do **not** use `${{ secrets.GITHUB_TOKEN }}` because it only has access to the current repo. |
| `JIRA_PAT` | Your Jira Personal Access Token. |

### Schedule

The `cron: '0 */4 * * *'` schedule runs every 4 hours. Adjust as needed:

| Schedule | Cron Expression |
|---|---|
| Every hour | `0 * * * *` |
| Every 4 hours | `0 */4 * * *` |
| Every 12 hours | `0 */12 * * *` |
| Daily at midnight UTC | `0 0 * * *` |
| Weekdays only, 9 AM UTC | `0 9 * * 1-5` |

### Manual trigger

The `workflow_dispatch` trigger lets you manually run a sync from the GitHub Actions UI with optional inputs:

- **repo**: Specify a single repo (e.g., `your-org/your-repo`) or `all`
- **dry_run**: Enable dry-run mode to preview changes

---

## Network Access

> **⚠️ Important**: If your Jira instance is behind a corporate firewall, GitHub-hosted runners (`ubuntu-latest`) cannot reach it. You must use a [self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners) deployed inside your network.

To use a self-hosted runner, change the workflow:

```yaml
jobs:
  sync:
    runs-on: self-hosted    # ← instead of ubuntu-latest
```

---

## Monitoring

GitHub Actions provides built-in monitoring:

- **Run history**: See all past sync runs in the Actions tab.
- **Failure notifications**: GitHub sends email notifications on workflow failures.
- **Logs**: Full logs of each sync run are available in the Actions UI.

For additional monitoring, you can add a step to post a summary to Slack or Teams after the sync completes.

---

## Centralized Hub Architecture (`quill-automation`)

If you want to keep your target software repositories (e.g., `your-org/target-repo`) clean without adding synchronization workflows or `quill.toml` configs to every repository, we strongly recommend adopting the **Centralized Hub Repository** pattern.

### Setup Steps:

1. **Create a Dedicated Automation Repository**: Create a repository (e.g., `your-org/quill-automation`) containing only your central `quill.toml` and `.github/workflows/sync.yml`.
2. **Configure Central Secrets**: Store your credentials in this repository's GitHub Secrets (`JIRA_SERVER`, `JIRA_TOKEN`, `GH_PAT`).
3. **Workflow Example (`.github/workflows/sync.yml`)**:
   ```yaml
   name: Centralized Jira Issue Sync

   on:
     schedule:
       - cron: '0 * * * *'        # Run hourly at minute 0
     workflow_dispatch:           # Manual UI trigger

   jobs:
     sync:
       runs-on: ubuntu-latest     # Or self-hosted runner if Jira is internal
       steps:
         - name: Checkout automation hub repository
           uses: actions/checkout@v4

         - name: Set up Python
           uses: actions/setup-python@v5
           with:
             python-version: '3.12'

         - name: Install Quill Sync
           run: pip install git+https://github.com/r-xue/quill.git

         - name: Run Centralized Synchronization
           env:
             QUILL_JIRA_SERVER: ${{ secrets.JIRA_SERVER }}
             QUILL_JIRA_TOKEN: ${{ secrets.JIRA_TOKEN }}
             QUILL_GITHUB_TOKEN: ${{ secrets.GH_PAT }}
           run: |
             quill sync
   ```

By using this architecture, target software repositories remain completely unpolluted, security secrets are isolated to one central hub repo, and onboarding new repositories requires only adding lines to a single centralized `quill.toml`.
