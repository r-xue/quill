import re
from typing import Optional, Any
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from quill.config import Settings, GitHubRepoConfig
from quill.github_client import GitHubClient
from quill.jira_client import JiraClient
from quill.mapper import (
    markdown_to_jira,
    format_comment,
    compute_content_hash,
    embed_hash_footer,
    extract_hash_footer,
)
from quill.log import logger, redact_token

_console = Console()


class SyncEngine:
    def __init__(self, config: Settings):
        self.config = config

        gh_tok = redact_token(config.github_token)
        jira_tok = redact_token(config.jira_token)
        logger.info(
            f"Initializing SyncEngine — GitHub: {gh_tok}, Jira: {jira_tok}"
        )

        self.gh_client = GitHubClient(token=config.github_token)
        self.jira_client = JiraClient(
            server=config.jira_server,
            token=config.jira_token,
            verify_ssl=config.jira_verify_ssl,
        )

    # ── Public API ────────────────────────────────────────────────────────

    def sync_all(self, repo_override: Optional[str] = None, force: bool = False):
        """Sync all configured repositories or a specific one.

        Args:
            repo_override: Specific repository to sync in `owner/repo` format.
                If None, syncs all configured repositories.
            force: If True, skip the stored content hash and re-sync every
                ticket regardless of whether GitHub content changed.
        """
        repos = self.config.repos
        if repo_override:
            repos = [r for r in repos if r.full_name == repo_override]
            if not repos:
                logger.error(
                    f"Repository '{repo_override}' not found in configuration."
                )
                return

        dry_run = self.config.dry_run
        if dry_run:
            logger.info(
                "[bold yellow]*** DRY-RUN MODE — no changes will be written "
                "to Jira ***[/bold yellow]"
            )
        if force:
            logger.info(
                "[bold yellow]*** FORCE MODE — ignoring stored hashes, "
                "all tickets will be updated ***[/bold yellow]"
            )

        # Pre-fetch Jira lookups once per project so repos sharing the same
        # Jira project (e.g. all 12 repos → "GITHUB") don't repeat the
        # expensive JQL query.  The lookup maps GitHub URL → Jira issue.
        github_link_field = self.config.jira_github_link_field
        project_lookup_cache: dict[str, dict[str, Any]] = {}
        if not dry_run:
            projects_needed = {rc.jira_project for rc in repos}
            for project in projects_needed:
                logger.info(f"Pre-fetching Jira lookup for project {project}…")
                project_lookup_cache[project] = self.jira_client.get_synced_issues(
                    project=project,
                    github_link_field=github_link_field,
                )
                logger.info(
                    f"Cached {len(project_lookup_cache[project])} synced issues for {project}"
                )

        for repo_config in repos:
            try:
                self._sync_repo(
                    repo_config,
                    dry_run=dry_run,
                    force=force,
                    project_lookup_cache=project_lookup_cache,
                )
            except Exception:
                logger.exception(
                    f"Failed to sync repository {repo_config.full_name}"
                )

    # ── Per-repo sync ─────────────────────────────────────────────────────

    def _sync_repo(
        self,
        rc: GitHubRepoConfig,
        dry_run: bool = False,
        force: bool = False,
        project_lookup_cache: Optional[dict[str, dict[str, Any]]] = None,
    ):
        logger.info(
            f"[bold blue]Syncing {rc.full_name} → Jira {rc.jira_project}[/bold blue]"
        )

        # Step 1 — use cached Jira lookup (fetched once per project in sync_all)
        if not dry_run:
            if project_lookup_cache and rc.jira_project in project_lookup_cache:
                existing_lookup = project_lookup_cache[rc.jira_project]
            else:
                # Direct call fallback (e.g. when _sync_repo is called standalone)
                github_link_field = self.config.jira_github_link_field
                existing_lookup = self.jira_client.get_synced_issues(
                    project=rc.jira_project,
                    github_link_field=github_link_field,
                )
        else:
            existing_lookup = {}

        # Step 1.5 — zero-cost description hash check + batch pre-fetch fallback
        if not dry_run and existing_lookup:
            unresolved_keys = []
            for iss in existing_lookup.values():
                if getattr(iss, "_quill_cached_hash", None) is not None:
                    continue
                desc = getattr(getattr(iss, "fields", None), "description", "") or ""
                footer_hash = extract_hash_footer(desc)
                if footer_hash:
                    setattr(iss, "_quill_cached_hash", footer_hash)
                else:
                    unresolved_keys.append(iss.key)

            if unresolved_keys:
                logger.debug(
                    f"Description hash missed for {len(unresolved_keys)} issues; "
                    f"batch fetching properties…"
                )
                cached_hashes = self.jira_client.batch_get_issue_properties(
                    unresolved_keys, "quill-content-hash"
                )
                for iss in existing_lookup.values():
                    if iss.key in cached_hashes:
                        setattr(iss, "_quill_cached_hash", cached_hashes[iss.key])

        # Step 1.8 — optimization #1: batch pre-fetch GitHub Projects V2 custom fields
        repo_project_fields: Optional[dict[int, dict[str, str]]] = None
        if rc.sync_project_fields:
            logger.info("Batch fetching GitHub Projects V2 custom fields via GraphQL…")
            repo_project_fields = self.gh_client.get_repo_project_fields(rc.owner, rc.repo)

        # Step 2 — fetch GitHub issues
        gh_issues = self.gh_client.get_issues(
            owner=rc.owner,
            repo_name=rc.repo,
            since=rc.issue_filter.since,
            state=rc.issue_filter.state,
            labels=rc.issue_filter.labels or None,
        )

        stats = {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "comments": 0,
            "errors": 0,
        }

        for issue in gh_issues:
            try:
                self._sync_issue(
                    rc,
                    issue,
                    existing_lookup,
                    dry_run,
                    force,
                    stats,
                    repo_project_fields=repo_project_fields,
                )
            except Exception as exc:
                stats["errors"] += 1
                logger.error(
                    f"Error syncing issue #{issue.number} ({issue.title}): {exc}"
                )

        logger.info(
            f"[bold green]Done {rc.full_name}:[/bold green] "
            f"Created={stats['created']}  Updated={stats['updated']}  "
            f"Skipped={stats['skipped']}  Comments={stats['comments']}  "
            f"Errors={stats['errors']}"
        )

    # ── Per-issue sync ────────────────────────────────────────────────────

    def _sync_issue(
        self,
        rc: GitHubRepoConfig,
        issue: Any,
        lookup: dict[str, Any],
        dry_run: bool,
        force: bool,
        stats: dict[str, int],
        repo_project_fields: Optional[dict[int, dict[str, str]]] = None,
    ):
        gh_url: str = issue.html_url
        issue_labels = [label.name for label in issue.labels]

        project_fields: dict[str, str] = {}
        if rc.sync_project_fields:
            if repo_project_fields is not None and issue.number in repo_project_fields:
                project_fields = repo_project_fields[issue.number]
            else:
                project_fields = self.gh_client.get_issue_project_fields(issue)
            for k, v in sorted(project_fields.items()):
                clean_k = re.sub(r"[^a-zA-Z0-9]+", "-", k.lower()).strip("-")
                clean_v = re.sub(r"[^a-zA-Z0-9]+", "-", v.lower()).strip("-")
                if clean_k and clean_v:
                    issue_labels.append(f"proj-{clean_k}-{clean_v}")

        # Option 1 & 3: Extract Milestone title and due date
        milestone_title = None
        due_date_str = None
        if getattr(rc, "sync_milestones", True):
            milestone_obj = getattr(issue, "milestone", None)
            if milestone_obj:
                milestone_title = getattr(milestone_obj, "title", None)
                due_on = getattr(milestone_obj, "due_on", None)
                if due_on:
                    if hasattr(due_on, "strftime"):
                        due_date_str = due_on.strftime("%Y-%m-%d")
                    else:
                        due_date_str = str(due_on)[:10]

                if milestone_title:
                    clean_ms = re.sub(
                        r"[^a-zA-Z0-9]+", "-", milestone_title.lower()
                    ).strip("-")
                    if clean_ms:
                        issue_labels.append(f"milestone-{clean_ms}")

        current_hash = compute_content_hash(
            issue.title, issue.body, issue.state, issue_labels
        )

        # Convert body to Jira markup and append metadata
        jira_body = markdown_to_jira(issue.body)

        # Metadata panel — surfaces original author/date since Jira Reporter
        # is always the API token owner. No panel title to keep it compact.
        created_at = issue.created_at.strftime("%Y-%m-%d") if issue.created_at else "?"
        author_login = issue.user.login if issue.user else "unknown"
        author_url = f"https://github.com/{author_login}"

        proj_parts = []
        if milestone_title:
            ms_display = f"*{milestone_title}*"
            if due_date_str:
                ms_display += f" (Due: {due_date_str})"
            proj_parts.append(f"*Milestone:* {ms_display}")

        if project_fields:
            formatted_fields = " \\| ".join(
                f"*{k}:* {v}" for k, v in sorted(project_fields.items())
            )
            proj_parts.append(f"*Project:* {formatted_fields}")

        proj_line = ""
        if proj_parts:
            proj_line = " \\| ".join(proj_parts) + "\n"

        header = (
            f"{{panel:borderStyle=solid|borderColor=#ccc|bgColor=#f5f5f5}}\n"
            f"*Source:* [{rc.full_name}#{issue.number}|{gh_url}] \\| "
            f"*Author:* [{author_login}|{author_url}] \\| "
            f"*Created:* {created_at}\n"
            f"{proj_line}"
            f"{{panel}}\n\n"
        )

        jira_description = embed_hash_footer(header + jira_body, current_hash)

        summary = f"[{rc.repo}#{issue.number}] {issue.title}"
        all_labels = issue_labels + ["github-synced"]

        jira_issue = lookup.get(gh_url)
        github_link_field = self.config.jira_github_link_field

        # Per-issue fallback: if the batch JQL lookup missed this issue
        # (e.g. customfield_10200 not on screen so URL was never stored),
        # search by the deterministic summary prefix before creating.
        if jira_issue is None and not dry_run:
            summary_prefix = f"[{rc.repo}#{issue.number}]"
            jira_issue = self.jira_client.find_by_summary_prefix(
                rc.jira_project, summary_prefix
            )
            if jira_issue:
                logger.debug(
                    f"Found existing issue {jira_issue.key} via summary prefix "
                    f"(customfield fallback)."
                )

        if jira_issue is None:
            # ── NEW ──────────────────────────────────────────────────
            logger.info(f"Issue #{issue.number} is new. Creating in Jira…")
            if not dry_run:
                jira_key = self.jira_client.create_issue(
                    project=rc.jira_project,
                    summary=summary,
                    description=jira_description,
                    issue_type=self.config.jira_default_issue_type,
                    labels=all_labels,
                    github_link_field=github_link_field,
                    github_url=gh_url,
                    due_date=due_date_str,
                )
                self.jira_client.add_remote_link(
                    issue_key=jira_key,
                    github_url=gh_url,
                    title=f"{rc.full_name}#{issue.number}",
                )
                # Store hash and GitHub URL invisibly via entity properties
                self.jira_client.set_issue_property(
                    jira_key, "quill-content-hash", current_hash
                )
                self.jira_client.set_issue_property(
                    jira_key, "quill-github-url", gh_url
                )
                logger.info(f"Created {jira_key} for GH #{issue.number}")
            else:
                jira_key = None
                self._preview_issue(
                    action="CREATE",
                    project=rc.jira_project,
                    summary=summary,
                    issue_type=self.config.jira_default_issue_type,
                    labels=all_labels,
                    gh_state=issue.state,
                    gh_url=gh_url,
                    description=jira_description,
                )
            stats["created"] += 1

        else:
            # ── EXISTS — check for changes ─────────────────────────────────
            jira_key = jira_issue.key

            # Primary: read hash from pre-fetched cache or description comment (zero HTTP cost)
            existing_hash = getattr(jira_issue, "_quill_cached_hash", None)
            if existing_hash is None:
                existing_hash = extract_hash_footer(
                    getattr(jira_issue.fields, "description", "") or ""
                )
                if existing_hash is not None:
                    setattr(jira_issue, "_quill_cached_hash", existing_hash)
            # Fallback: query Jira entity property
            if existing_hash is None:
                existing_hash = self.jira_client.get_issue_property(
                    jira_key, "quill-content-hash"
                )
                if existing_hash is not None:
                    setattr(jira_issue, "_quill_cached_hash", existing_hash)

            # --force: ignore stored hash and always update
            if force:
                existing_hash = None

            if existing_hash != current_hash:
                logger.info(
                    f"Issue #{issue.number} changed (hash mismatch). "
                    f"Updating {jira_key}…"
                )
                if not dry_run:
                    self.jira_client.update_issue(
                        issue_key=jira_key,
                        summary=summary,
                        description=jira_description,
                        labels=all_labels,
                        due_date=due_date_str,
                    )
                    self.jira_client.set_issue_property(
                        jira_key, "quill-content-hash", current_hash
                    )
                    self.jira_client.set_issue_property(
                        jira_key, "quill-github-url", gh_url
                    )
                else:
                    self._preview_issue(
                        action="UPDATE",
                        project=rc.jira_project,
                        summary=summary,
                        issue_type=self.config.jira_default_issue_type,
                        labels=all_labels,
                        gh_state=issue.state,
                        gh_url=gh_url,
                        description=jira_description,
                        jira_key=jira_key,
                    )
                stats["updated"] += 1
            else:
                logger.debug(f"Issue #{issue.number} unchanged → skip.")
                stats["skipped"] += 1

        # ── State transition (close) ─────────────────────────────────
        if issue.state == "closed":
            is_already_closed = False
            if jira_issue is not None:
                status_obj = getattr(jira_issue.fields, "status", None)
                status_name = getattr(status_obj, "name", "").lower() if status_obj else ""
                if status_name in ("done", "closed", "resolved"):
                    is_already_closed = True

            if not is_already_closed:
                if not dry_run and jira_key:
                    for transition in ("Done", "Closed", "Resolved"):
                        if self.jira_client.transition_issue(jira_key, transition):
                            break
                elif dry_run:
                    display_key = jira_key or f"NEW-ISSUE(#{issue.number})"
                    logger.info(
                        f"[Dry Run] Would transition {display_key} to Done/Closed"
                    )

        # ── Comment sync ─────────────────────────────────────────────
        if rc.sync_comments and getattr(issue, "comments", 0) > 0:
            if jira_key:
                self._sync_comments(rc, issue, jira_key, dry_run, stats)
            elif dry_run:
                logger.info(f"[Dry Run] Would sync comments for NEW-ISSUE(#{issue.number})")

    # ── Dry-run preview ───────────────────────────────────────────────────

    @staticmethod
    def _preview_issue(
        action: str,
        project: str,
        summary: str,
        issue_type: str,
        labels: list[str],
        gh_state: str,
        gh_url: str,
        description: str,
        jira_key: str | None = None,
    ):
        """Render a rich panel showing what the Jira ticket would look like."""
        # Truncate description for display (strip the hash footer)
        desc_lines = description.split("\n")
        # Remove the invisible hash footer line for display
        display_lines = [
            ln for ln in desc_lines if not ln.strip().startswith("<!-- quill:")
        ]
        desc_preview = "\n".join(display_lines[:15])
        if len(display_lines) > 15:
            desc_preview += f"\n  … ({len(display_lines) - 15} more lines)"

        # Build metadata table
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Field", style="dim", width=16)
        table.add_column("Value")

        if jira_key:
            table.add_row("Jira Key", f"[cyan]{jira_key}[/cyan]")
        table.add_row("Project", f"[cyan]{project}[/cyan]")
        table.add_row("Issue Type", issue_type)
        table.add_row("Summary", f"[bold]{summary}[/bold]")
        table.add_row(
            "Labels",
            ", ".join(f"[green]{lb}[/green]" for lb in labels) if labels else "(none)",
        )
        table.add_row(
            "GH State",
            f"[green]{gh_state}[/green]" if gh_state == "open" else f"[red]{gh_state}[/red]",
        )
        table.add_row("GitHub URL", gh_url)

        color = "green" if action == "CREATE" else "yellow"
        title = f"[bold {color}]DRY RUN — {action}[/bold {color}]"

        _console.print()
        _console.print(Panel(table, title=title, border_style=color, width=100))
        _console.print(
            Panel(desc_preview, title="Description Preview", border_style="dim", width=100)
        )

    # ── Comment sync ──────────────────────────────────────────────────────

    def _sync_comments(
        self,
        rc: GitHubRepoConfig,
        issue: Any,
        jira_key: str,
        dry_run: bool,
        stats: dict[str, int],
    ):
        existing_jira_comments: list[str] = []
        if not dry_run:
            try:
                existing_jira_comments = self.jira_client.get_existing_comments(
                    jira_key
                )
            except Exception as exc:
                logger.error(f"Failed to fetch Jira comments for {jira_key}: {exc}")
                return

        gh_comments = self.gh_client.get_comments(
            owner=rc.owner,
            repo_name=rc.repo,
            issue_number=issue.number,
        )

        for comment in gh_comments:
            comment_sig = comment.html_url
            already_posted = any(
                comment_sig in body for body in existing_jira_comments
            )
            if not already_posted:
                formatted = format_comment(
                    comment.user.login, comment.html_url, comment.body
                )
                if not dry_run:
                    self.jira_client.add_comment(jira_key, formatted)
                else:
                    logger.info(
                        f"[Dry Run] Would add comment by @{comment.user.login} "
                        f"to {jira_key}"
                    )
                stats["comments"] += 1

    # ── Status query (for CLI `status` command) ───────────────────────────

    def get_status(self) -> dict[str, int]:
        """Query Jira for the count of synced issues per project.

        Returns:
            A mapping from the GitHub repository full name to the synced issue count.
        """
        github_link_field = self.config.jira_github_link_field
        result: dict[str, int] = {}
        for rc in self.config.repos:
            lookup = self.jira_client.get_synced_issues(
                project=rc.jira_project,
                github_link_field=github_link_field,
            )
            result[rc.full_name] = len(lookup)
        return result

