# Field Mapping

This page documents how GitHub issue fields are mapped to Jira issue fields.

---

## Default Mapping

| GitHub Field | Jira Field | Conversion | Notes |
|---|---|---|---|
| `title` | `summary` | Prefixed with `[owner/repo #N]` | e.g., `[your-org/your-repo #42] Bug in tclean` |
| `body` (Markdown) | `description` (wiki markup) | Built-in Markdown→Jira converter | Headers, bold, italic, code blocks, links, lists, blockquotes |
| `state` (`open` / `closed`) | Status transition | Configurable | On close: attempts "Done" → "Closed" → "Resolved" |
| `labels` | `labels` | Direct copy + `github-synced` tag | Spaces in label names are replaced with hyphens |
| `html_url` | Custom field (`github_link_field`) | Direct copy | **Primary key** — e.g., `https://github.com/your-org/your-repo/issues/42` |
| `created_at` | Description footer | Informational | "Originally filed on GitHub" with link |
| `comments` | Comments | Attributed | `*@username* commented on GitHub (link):` |

---

## Markdown → Jira Wiki Markup

The built-in converter handles the following Markdown syntax:

| Markdown | Jira Wiki Markup |
|---|---|
| `# Heading 1` | `h1. Heading 1` |
| `## Heading 2` | `h2. Heading 2` |
| `**bold**` or `__bold__` | `*bold*` |
| `*italic*` or `_italic_` | `_italic_` |
| `` `inline code` `` | `{{inline code}}` |
| ` ```python\ncode\n``` ` | `{code:python}\ncode\n{code}` |
| `[text](url)` | `[text\|url]` |
| `- item` or `* item` | `* item` |
| `1. item` | `# item` |
| `> quote` | `{quote}quote{quote}` |

> **Note**: Complex Markdown features (tables, images, nested lists, task lists) are not converted and will appear as raw text. For most GitHub issues, the basic conversion covers the vast majority of formatting.

---

## Jira Issue Summary Format

The Jira `summary` field is formatted as:

```
[owner/repo #issue_number] Original GitHub Title
```

For example:

```
[your-org/your-repo #42] tclean crashes with cube imaging
```

This makes it easy to identify synced issues in Jira and search for them.

---

## Labels

GitHub labels are copied directly to Jira labels with two modifications:

1. **Spaces → hyphens**: Jira labels cannot contain spaces, so `high priority` becomes `high-priority`.
2. **`github-synced` tag**: Every synced issue gets an additional `github-synced` label, making it easy to filter synced issues in Jira.

---

## State Transitions

When a GitHub issue is **closed**, quill attempts to transition the Jira issue. It tries the following transition names in order:

1. `Done`
2. `Closed`
3. `Resolved`

The first available transition is used. If none are available (e.g., the issue is already in a terminal state, or your workflow uses different names), quill logs a warning with the available transitions.

> **Tip**: If your Jira workflow uses custom transition names, you'll see them in the warning log. Future versions of quill may support configurable transition names.

---

## Comments

GitHub comments are synced to Jira with an attribution header:

```
*@octocat* commented on GitHub (link):

The rest of the comment body, converted from Markdown to Jira wiki markup.
```

To prevent duplicates, quill checks if the GitHub comment's URL already appears in any existing Jira comment. Comments are synced only once — edits to existing GitHub comments are not propagated.

---

## Content Hash Entity Property

Every Jira issue created or updated by quill has an invisible JSON property attached to it via the Jira API:

`quill-content-hash: a1b2c3d4e5f67890...`

This is used for [change detection](architecture.md#change-detection) and is not visible in the Jira UI.
