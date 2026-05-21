# Jira Custom Field Setup

`quill` is **stateless** — it does not keep a local database. Instead, it stores the GitHub issue URL in a Jira custom field and uses JQL to discover which issues have already been synced.

This page explains how to set up that custom field.

---

## Why a Custom Field?

Every Jira issue created by quill needs a way to be linked back to its GitHub origin. By storing the GitHub issue URL (e.g., `https://github.com/your-org/your-repo/issues/42`) in a dedicated custom field, quill can:

1. **Discover existing synced issues** via JQL without any local state file.
2. **Work natively in CI/CD** (GitHub Actions) without caching or artifact hacks.
3. **Survive reinstalls** — the sync state lives in Jira itself.

---

## Step 1: Create the Custom Field

> **Note**: You need Jira **administrator** permissions to create custom fields. If you don't have admin access, ask your Jira admin to create the field for you.

### Jira Server / Data Center

1. Go to **⚙️ Administration → Issues → Custom Fields**.
2. Click **Add Custom Field**.
3. Select field type: **URL** (preferred) or **Text Field (single line)**.
4. Name it something descriptive: `GitHub Link` or `GitHub Issue URL`.
5. (Optional) Add a description: `"URL linking this issue to its source GitHub issue. Managed by quill sync tool."`
6. Choose the **screens** this field should appear on — add it to at least the **Default Screen** or the screen used by your target projects.
7. Click **Create**.

### Jira Cloud

1. Go to **⚙️ Settings → Issues → Custom Fields**.
2. Click **Create custom field**.
3. Select **URL** field type.
4. Name: `GitHub Link`.
5. Associate with relevant projects.

---

## Step 2: Find the Custom Field ID

After creating the field, you need its internal ID (e.g., `customfield_10200`).

### Method A: Via the Jira REST API

```bash
curl -s -H "Authorization: Bearer $JIRA_PAT" \
  "https://jira.example.com/rest/api/2/field" \
  | python3 -c "
import json, sys
fields = json.load(sys.stdin)
for f in fields:
    if 'github' in f['name'].lower():
        print(f['id'], '-', f['name'])
"
```

Example output:

```
customfield_10200 - GitHub Link
```

### Method B: Via the Jira Admin UI

1. Go to **⚙️ Administration → Issues → Custom Fields**.
2. Find your field and click **Configure** (or the ⚙️ gear icon).
3. Look at the URL in your browser — it will contain something like `customFieldId=10200`.
4. The field ID is `customfield_10200`.

---

## Step 3: Configure quill

Set the `github_link_field` value in your `quill.toml`:

```toml
jira_server = "https://jira.example.com"
jira_github_link_field = "customfield_10200"  # ← your field ID here
```

---

## Step 4: Verify

Run `quill check` to verify connectivity, then run a dry-run sync:

```bash
pixi run quill check
pixi run quill sync --dry-run
```

On the first real sync, quill will:

1. Fetch issues from GitHub.
2. Query Jira via JQL: `project = "CAS" AND cf[10200] is not EMPTY`.
3. Create new Jira issues with the `customfield_10200` set to the GitHub URL.
4. On subsequent runs, the JQL lookup finds existing issues, and quill only updates those that have changed.

---

## How Change Detection Works

When quill creates or updates a Jira issue, it attaches invisible JSON metadata to the issue using **Jira Entity Properties**. Specifically, it saves a property called `quill-content-hash` containing a SHA256 hash.

This is a SHA256 hash of the issue's title, body, state, and labels. On the next sync run, quill compares the hash stored in the Jira entity property with a freshly computed hash of the GitHub issue. If they differ, the issue is updated; if they match, it's skipped.

This means:

- **No local state file** is needed.
- **No database** is needed.
- Change detection is **fast** (hash comparison, not full-text diff).
- The hash is **invisible** in the Jira UI (HTML comments are not rendered).

---

## Permissions Summary

| Action | Required Permission |
|---|---|
| Create the custom field | Jira Administrator |
| Use quill to sync | Jira PAT with project write access |
| Read GitHub issues | GitHub PAT with `public_repo` scope (or none for public repos, but rate-limited) |

---

## FAQ

### Can I use an existing custom field?

Yes — if you already have a URL-type custom field that isn't used for anything else, you can repurpose it. Just set `github_link_field` to its ID.

### What if I don't have Jira admin access?

Ask your Jira admin to create a URL custom field named `GitHub Link` and tell you the field ID. You don't need admin access to *use* the field — only to *create* it.

### What if someone edits the custom field value in Jira?

Quill relies on the `github_link_field` containing the exact GitHub issue URL. If someone changes or clears it, quill will treat that issue as un-synced and may create a duplicate on the next run. Consider making the field read-only in Jira if possible.

### Can I use a Text field instead of a URL field?

Yes. A single-line text field works fine. The URL type is slightly nicer because Jira renders it as a clickable link, but functionally both work the same way.
