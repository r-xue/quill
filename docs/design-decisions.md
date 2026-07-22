# Design Decisions

This document records the key technology and architecture choices made for quill, the reasoning behind each, and what alternatives were considered.

---

## 0. Why Build quill at All?

**Decision**: Build a custom tool instead of using an existing GitHub Marketplace Action or commercial integration.

### Existing solutions evaluated

| Solution | What it does | Why it didn't work |
|---|---|---|
| **GitHub for Jira (Atlassian official app)** | Deep two-way integration | **Cloud-only**. Requires Atlassian Connect and Jira Cloud. Does not support on-premise Jira Server or Data Center. |
| **GitHub Marketplace: "Sync GitHub issues to Jira"** (various) | Issue sync Actions | Designed for Jira Cloud, not self-hosted instances. |
| **Unito** | Two-way sync between many tools | Commercial SaaS; expensive per-seat pricing; data routed through a third party; not suitable for air-gapped or on-premise Jira |
| **Zapier / Make (Integromat)** | Workflow automation | Cloud-based; cannot reach on-premise Jira behind a firewall |
| **GitHub App (self-hosted)** | Custom App on your own server | Requires running a persistent server; webhook management; significant ops overhead for a simple sync task |
| **Jira's built-in DVCS connector** | Links commits and branches | Read-only; shows activity in Jira but does not create or sync issues |

### Why not the official "GitHub for Jira" app?

The [GitHub for Jira app](https://github.com/atlassian/github-for-jira) is Atlassian's first-party integration and is the obvious first choice to evaluate. Here is why it was ruled out:

**1. Jira Cloud only — architectural incompatibility**

GitHub for Jira is built on [Atlassian Connect](https://developer.atlassian.com/cloud/jira/platform/atlassian-connect-overview/), a framework that only works with Jira Cloud. It establishes a trust relationship between `github.com` and `your-instance.atlassian.net`. YourOrg runs Jira Data Center (`open-jira.example.com`) behind a firewall — Atlassian Connect cannot reach it, and Jira DC does not support the Connect app framework.

**2. What it actually syncs (not issues)**

Even if you could install it on Jira DC, GitHub for Jira focuses on **development activity** — commits, branches, pull requests, and deployments — not issues. It surfaces GitHub PR status inside a Jira ticket but does not:
- Create Jira tickets from GitHub issues
- Sync issue title, body, labels, or state
- Map GitHub repos to Jira projects

It is a developer workflow tool, not an issue sync tool.

**3. Requires admin access on the GitHub org**

Installing GitHub for Jira requires GitHub organization admin approval (OAuth app installation). For repos that YourOrg watches but does not own, this is not possible.

**4. No control over field mapping**

The app maps to a fixed set of Jira fields. It cannot set custom fields (like `customfield_10200` for the GitHub URL) or apply project-specific label mappings. quill's mapper is fully configurable per-repo.

**5. No dry-run or audit mode**

There is no way to preview what GitHub for Jira would create before it runs. quill's `--dry-run` mode with rich ticket previews was a deliberate design requirement.

### The core blockers

Two fundamental constraints ruled out every off-the-shelf option:

1. **On-premise Jira** — YourOrg runs Jira Data Center behind a firewall. Every commercial integration (Atlassian's own, Unito, Zapier) is SaaS-first and assumes Jira Cloud or a publicly reachable endpoint.

2. **Repos we don't own** — quill needs to pull from public GitHub repos (e.g., `your-org/your-repo`, `your-org/another-repo`) that we watch but don't administer. This rules out GitHub Apps and webhook-based approaches which require admin access to install.

3. **No external data routing** — YourOrg's security posture does not allow issue content (potentially containing internal project notes) to be routed through a third-party SaaS cloud.

### Why a CLI tool over a GitHub App

| Approach | Pros | Cons |
|---|---|---|
| **GitHub App (webhook)** | Real-time; event-driven | Requires a persistent server; admin access to install webhooks on target repos; complex auth (JWT + installation tokens) |
| **Scheduled CLI / GitHub Actions** (chosen) | Simple; no persistent server; runs in your own CI; works on repos you don't own | Polling-based (minutes of latency) |

For issue tracking, **minutes of latency is acceptable**. Real-time sync via webhooks would add significant operational complexity for no meaningful user benefit.

### Summary

quill exists because:
- All commercial/SaaS integrations require Jira Cloud or outbound internet access from Jira
- Webhook-based approaches require admin access to the source GitHub repos
- A simple scheduled CLI tool running in GitHub Actions perfectly fits the use case with zero infrastructure overhead

---

## 1. Stateless Architecture (No Local Database)

**Decision**: Store sync state in Jira itself (custom field + entity properties) instead of a local database.

**Alternatives considered**:

| Approach | Pros | Cons |
|---|---|---|
| **SQLite state file** (v1) | Fast lookups, simple queries | Must persist between runs; CI/CD nightmare (needs `actions/cache`); multi-machine conflicts; file can be lost or corrupted |
| **Git-committed state** | Versioned, survives reinstalls | Pollutes commit history; merge conflicts; grows with issue count |
| **Redis/external DB** | Fast, shared, queryable | Over-engineered for this use case; adds infrastructure dependency |
| **Jira custom field + entity properties** (chosen) | Zero local state; CI/CD native; multi-machine safe; survives reinstalls | Requires one-time Jira admin setup for custom field |

**Why we switched**: quill v1 used SQLite. It worked locally but broke in GitHub Actions because runners are ephemeral — the state file was lost between runs. Caching hacks (`actions/cache`, artifact uploads) were fragile. The stateless design eliminates the problem entirely.

---

## 2. Change Detection via Pre-fetched Entity Properties

**Decision**: Store the SHA256 content hash in Jira issue entity properties (`quill-content-hash`) and pre-fetch it via the `properties` parameter during batch JQL lookups.

**Evolution & Alternatives considered**:

| Approach | Pros | Cons |
|---|---|---|
| **Full-text diff** | Detects any change | Expensive; Markdown→Jira conversion is lossy, so round-trip comparison breaks |
| **`updated_at` timestamp** | Simple | Requires storing "last synced at" somewhere (back to state problem); GitHub timestamps change on label edits too |
| **HTML comment footer in description** (v10) | No extra API calls; self-contained | While hidden in basic wiki rendering, the comment `<!-- quill:sha256:... -->` leaked into external presentation layers (e.g. mirror projects, markdown presentation boards) |
| **Pre-fetched entity properties** (chosen in v11) | Completely clean issue description; 100% invisible to users in all presentation layers; **zero extra HTTP calls** when pre-fetched via `search_issues(properties=...)` | Requires passing `properties` parameter to Jira REST search API |

**Why this approach**: In earlier versions (v10), quill embedded `<!-- quill:sha256:... -->` as an HTML comment in the Jira description. However, users viewing tickets in markdown mirror boards or external presentation tools could see the plain text comment. By storing the hash in Jira issue entity properties (`quill-content-hash`) and requesting `properties="quill-content-hash,quill-github-url"` during the batch JQL query (`search_issues`), quill achieves exact change detection with **zero additional HTTP calls** and leaves the Jira description completely clean.

---

## 3. Navigating Issue Hierarchy Mismatches

GitHub's issue-tracking capabilities support flexibly deep parent-child relationships (e.g., issues containing sub-issues, which contain sub-sub-issues). Jira, conversely, enforces a strict 3-tier hierarchy: **Epic → Task → Subtask**.

**Decision**: quill maps GitHub's flexible relationships into Jira's rigid 3-tier system. However, this fundamental architectural mismatch introduces several inherent quirks and operational limitations:

1. **Hierarchy Depth Limitations**: Because Jira natively supports only three tiers, GitHub issue relationships that are four or more levels deep cannot be perfectly translated. Deeper nesting will inevitably be flattened, truncated, or presented inaccurately on the Jira side. Teams are encouraged to restrict their GitHub hierarchies to a maximum of three layers to maintain 1:1 parity.

2. **Structural Rigidity & Ticket Regeneration**: The Jira REST API does not allow in-place promotion or demotion of issue types across architectural boundaries (e.g., you cannot trivially update a Task to become a Subtask via standard field edits; it often requires a specialized conversion process or recreation). If a parent-child relationship changes significantly in GitHub (such as demoting a standalone issue into a sub-issue), the sync tool may be forced to delete and regenerate the Jira ticket to satisfy Jira's structural constraints.
   - **Warning**: This regeneration process assigns a new Jira issue key and could lead to the loss of Jira-exclusive metadata (like recent sprint assignments or manual edits). Users should minimize shuffling parent-child relationships in GitHub after they have been synced.

3. **Inferred Relationships**: A two-layer hierarchy in GitHub could logically map to either `Epic → Task` or `Task → Subtask`. The automation must rely on specific heuristics (like looking for "Epic" headers or analyzing the tip of the hierarchy tree) to determine the correct Jira issue types. This inference can occasionally lead to ambiguity.

4. **Label Propagation**: Labels applied to a top-level parent issue in GitHub are generally translated to the corresponding Epic in Jira, but they are not automatically propagated down to the child Tasks or Subtasks. This can impact filtering and visibility on Jira Scrum/Kanban boards if those boards rely heavily on task-level label queries.

---

## 4. python-jira over atlassian-python-api

**Decision**: Use [python-jira](https://github.com/pycontribs/jira) (`jira` package) as the Jira client library.

**Alternatives considered**:

| Library | Pros | Cons |
|---|---|---|
| **python-jira** (chosen) | Rich object model (`issue.fields.summary`); 12+ years maturity; consistent with ragdoll project; PAT auth built in | Heavier dependency; oauthlib transitive dep |
| **atlassian-python-api** | Maintained by Atlassian; lighter (requests only); better Cloud support | Dict-based responses (`issue["fields"]["summary"]`); less Pythonic |
| **Raw requests** | Zero abstraction overhead; full control | Must handle pagination, auth, error handling manually; more code to maintain |

**Why python-jira**: Consistency with the [ragdoll](https://github.com/nrao/ragdoll) project (same author, same Jira instance) was the deciding factor. Both projects share the same `jira >= 3.8` dependency, same PAT auth pattern, and same object model. Using a different library would mean maintaining two different Jira abstractions.

---

## 5. PyGithub over github3.py and ghapi

**Decision**: Use [PyGithub](https://github.com/PyGithub/PyGithub) for GitHub API access.

**Alternatives considered**:

| Library | Pros | Cons |
|---|---|---|
| **PyGithub** (chosen) | Most popular (6k+ stars); complete REST API coverage; good docs; works unauthenticated for public repos | Synchronous only; heavy object model |
| **github3.py** | Clean API; well-designed | Smaller community; less actively maintained |
| **ghapi** | Fast; auto-generated from OpenAPI spec; lightweight | Less intuitive API; sparse docs |
| **httpx + raw API** | Full control; async capable | Must handle pagination, rate limits, auth manually |

**Why PyGithub**: It's the most mature and widely-used Python GitHub library. For quill's read-only use case (fetching issues and comments from public repos), PyGithub's synchronous model is perfectly adequate. The rich object model (`issue.title`, `issue.labels`, `comment.user.login`) makes the mapper code clean and readable.

---

## 6. pydantic-settings for Configuration

**Decision**: Use [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) with a 4-layer hierarchical configuration.

**Alternatives considered**:

| Approach | Pros | Cons |
|---|---|---|
| **Plain YAML + manual env var interpolation** (v1) | Simple; familiar | No hierarchy; manual `${VAR}` regex; single config file |
| **python-dotenv + dataclasses** | Lightweight; stdlib | No TOML support; no cascading config |
| **dynaconf** | Powerful; many formats | Heavy dependency; complex API |
| **pydantic-settings** (chosen) | Built-in TOML + .env + env var support; type validation; cascading sources; consistent with ragdoll | Requires pydantic as transitive dependency |

**Why pydantic-settings**: The ragdoll project already established the 4-layer pattern (env vars → project TOML → .env → user TOML → defaults). Adopting the same approach for quill means:
- Consistent developer experience across projects
- Tokens can live in `.env` (git-ignored) or `~/.quill/config.toml` (user-level)
- CI/CD overrides work naturally via `QUILL_*` env vars
- No custom interpolation code needed

### Configuration Precedence

```
1. QUILL_* env vars          ← highest (CI/CD overrides)
2. ./quill.toml              ← project settings (committed)
3. ./.env                      ← project secrets (git-ignored)
4. ~/.quill/config.toml      ← user defaults (shared across projects)
5. Package defaults            ← sensible fallbacks
```

---

## 7. TOML over YAML

**Decision**: Use TOML for configuration files instead of YAML.

| Format | Pros | Cons |
|---|---|---|
| **YAML** (v1) | Familiar; widely used | Whitespace-sensitive; implicit typing gotchas (`no` → `False`); requires PyYAML dependency |
| **TOML** (chosen) | Explicit typing; built into Python 3.11+ (`tomllib`); `pydantic-settings` native support; consistent with `pyproject.toml` and pixi | Less familiar to some; array-of-tables syntax (`[[repos]]`) is unusual |
| **JSON** | Universal; no ambiguity | No comments; verbose |
| **INI** | Simple | No nested structures; no lists |

**Why TOML**: Switching to TOML eliminated the PyYAML dependency, aligned with the Python ecosystem standard (`pyproject.toml`, `pixi.toml`), and enabled native `pydantic-settings` TOML source support without custom loaders.

---

## 8. argparse over click

**Decision**: Use stdlib `argparse` instead of `click` for the CLI.

| Library | Pros | Cons |
|---|---|---|
| **click** (v1) | Beautiful API; decorators; auto help | External dependency; adds to install size |
| **argparse** (chosen) | Zero dependencies (stdlib); adequate for quill's 4 subcommands; universally available | More verbose; less elegant |
| **typer** | Modern; type-hint driven | Depends on click; adds two dependencies |

**Why argparse**: quill has exactly 4 subcommands (`sync`, `check`, `status`, `init`) with a handful of flags. This is well within argparse's sweet spot. Dropping click removed one dependency and simplified the install. For a tool this small, the ergonomic difference is negligible.

---

## 9. Rich for Terminal Output

**Decision**: Use [Rich](https://github.com/Textualize/rich) for logging and terminal output.

| Approach | Pros | Cons |
|---|---|---|
| **Plain print/logging** | Zero dependencies | Ugly; no color; hard to read |
| **Rich** (chosen) | Beautiful tables, panels, colors; `RichHandler` for logging; token redaction | Adds ~3 MB dependency |
| **colorama** | Cross-platform color | Manual formatting; Windows-focused |

**Why Rich**: The dry-run preview panels, status tables, and colored log output significantly improve the user experience. Rich is already widely adopted in the Python ecosystem and the ragdoll project uses it too.

---

## 10. Built-in Markdown Converter over pandoc

**Decision**: Use a custom regex-based Markdown → Jira wiki markup converter instead of an external tool.

| Approach | Pros | Cons |
|---|---|---|
| **Custom regex converter** (chosen) | Zero dependencies; covers 90% of GitHub issue formatting; fast | Doesn't handle tables, images, nested lists, task lists |
| **pandoc** | Complete; handles everything | Requires system binary install; heavy; overkill for issue descriptions |
| **markdown-to-jira (npm)** | Purpose-built | Requires Node.js; cross-language dependency |
| **atlassian-python-api converter** | Built-in | Tied to a specific Jira library |

**Why custom**: GitHub issues primarily use headings, bold/italic, code blocks, links, and lists. The built-in converter handles all of these with ~30 lines of regex. Adding a pandoc dependency for the rare table or image in an issue body is not worth the installation complexity, especially for CI/CD environments.

---

## 11. Pixi for Environment Management

**Decision**: Use [Pixi](https://pixi.sh/) for Python environment and task management.

| Tool | Pros | Cons |
|---|---|---|
| **Pixi** (chosen) | Conda + pip unified; reproducible lockfile; task runner; consistent with ragdoll | Newer; smaller community |
| **pip + venv** | Universal; simple | No lockfile; no task runner; manual venv activation |
| **Poetry** | Lockfile; dependency resolution | Slow resolver; doesn't support conda |
| **PDM** | PEP-compliant; fast | Less ecosystem adoption |

**Why Pixi**: The ragdoll project already uses Pixi, and its config merges cleanly into `pyproject.toml` via `[tool.pixi.*]` sections. Having one tool for environment setup, dependency locking, and task running (`pixi run sync`, `pixi run test`) reduces friction.

---

## 12. One-Way Sync (GitHub → Jira)

**Decision**: Sync is strictly one-directional. GitHub is the source of truth.

| Direction | Pros | Cons |
|---|---|---|
| **One-way GH → Jira** (chosen) | Simple; no conflict resolution; safe for repos you don't own | Jira-side edits can be overwritten |
| **Two-way sync** | Complete; teams can work in either tool | Conflict resolution is extremely complex; requires webhook access on both sides; needs locking/versioning |
| **Jira → GH** | Jira as source of truth | Requires Jira admin for webhooks; less common workflow |

**Why one-way**: quill's primary use case is syncing issues from **public repos you don't own** to an on-premise Jira. Two-way sync is fundamentally impossible here (you can't write to someone else's GitHub repo). Even for owned repos, two-way sync introduces conflict resolution complexity that's disproportionate to the value.

---

## Summary

| Choice | Selected | Primary Reason |
|---|---|---|
| Architecture | Stateless (Jira custom field + JQL) | CI/CD native, no state file to manage |
| Change detection | SHA256 hash in description footer | Invisible, self-contained, no extra fields |
| Jira library | python-jira | Consistency with ragdoll |
| GitHub library | PyGithub | Most mature, rich object model |
| Config system | pydantic-settings (4-layer TOML) | Consistency with ragdoll, hierarchical |
| Config format | TOML | Native pydantic-settings support, no PyYAML dep |
| CLI framework | argparse (stdlib) | Zero dependencies, adequate for 4 commands |
| Terminal output | Rich | Beautiful dry-run previews and tables |
| Markdown converter | Custom regex | Zero dependencies, covers 90% of cases |
| Environment manager | Pixi | Consistency with ragdoll, reproducible |
| Sync direction | One-way (GH → Jira) | Can't write to repos you don't own |
