import concurrent.futures
import os
import re
import threading
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

    def _get_max_workers(self, task_count: int) -> int:
        """Determine worker thread count capped by config concurrency and available CPU cores."""
        if task_count <= 1:
            return 1
        configured = getattr(self.config, "sync_concurrency", 4)
        cpus = os.cpu_count() or 1
        # Cap by configured concurrency, and also cap by vCPUs if vCPUs is below configured value
        worker_limit = min(configured, cpus) if cpus < configured else configured
        return max(1, min(worker_limit, task_count))

    # ── Orchestrator ────────────────────────────────────────────────────────

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

        # Pre-fetch GitHub issues across all repos in parallel (max 4 concurrent)
        # to eliminate sequential network wait time across multiple repos.
        gh_data_cache: dict[str, tuple[list[Any], Optional[dict[int, dict[str, str]]]]] = {}
        if len(repos) > 1:
            logger.info(f"Pre-fetching GitHub issues for {len(repos)} repositories in parallel…")
            def _fetch_gh(rc: GitHubRepoConfig):
                p_fields = None
                if rc.sync_project_fields:
                    p_fields = self.gh_client.get_repo_project_fields(rc.owner, rc.repo)
                p_parents = None
                if getattr(rc, "sync_parent_links", True):
                    p_parents = self.gh_client.get_repo_issue_parents(rc.owner, rc.repo)
                iss = self.gh_client.get_issues(
                    owner=rc.owner,
                    repo_name=rc.repo,
                    since=rc.issue_filter.since,
                    state=rc.issue_filter.state,
                    labels=rc.issue_filter.labels or None,
                )
                return rc.full_name, (iss, p_fields, p_parents)

            with concurrent.futures.ThreadPoolExecutor(max_workers=self._get_max_workers(len(repos))) as executor:
                for full_name, data in executor.map(_fetch_gh, repos):
                    gh_data_cache[full_name] = data

        for repo_config in repos:
            try:
                self._sync_repo(
                    repo_config,
                    dry_run=dry_run,
                    force=force,
                    project_lookup_cache=project_lookup_cache,
                    pre_fetched_gh_data=gh_data_cache.get(repo_config.full_name),
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
        pre_fetched_gh_data: Optional[tuple[list[Any], Optional[dict[int, dict[str, str]]]]] = None,
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

        # Step 1.5 — zero-cost description hash check (memory only, no HTTP calls)
        if not dry_run and existing_lookup:
            for iss in existing_lookup.values():
                if getattr(iss, "_quill_cached_hash", None) is None:
                    loaded_hash = self.jira_client.get_issue_property_from_issue(
                        iss, "quill-content-hash"
                    )
                    if loaded_hash:
                        setattr(iss, "_quill_cached_hash", loaded_hash)
                    else:
                        desc = getattr(getattr(iss, "fields", None), "description", "") or ""
                        footer_hash = extract_hash_footer(desc)
                        if footer_hash:
                            setattr(iss, "_quill_cached_hash", footer_hash)
                if getattr(iss, "_quill_cached_comments_count", None) is None:
                    loaded_cnt = self.jira_client.get_issue_property_from_issue(
                        iss, "quill-synced-comments-count"
                    )
                    if loaded_cnt is not None:
                        try:
                            setattr(iss, "_quill_cached_comments_count", int(loaded_cnt))
                        except Exception:
                            pass

        # Step 1.8 & Step 2 — fetch or use pre-fetched GitHub issues and custom fields
        if pre_fetched_gh_data is not None:
            if len(pre_fetched_gh_data) == 3:
                gh_issues, repo_project_fields, repo_parents = pre_fetched_gh_data
            else:
                gh_issues, repo_project_fields = pre_fetched_gh_data
                repo_parents = None
        else:
            repo_project_fields = None
            if rc.sync_project_fields:
                logger.info("Batch fetching GitHub Projects V2 custom fields via GraphQL…")
                repo_project_fields = self.gh_client.get_repo_project_fields(rc.owner, rc.repo)
            repo_parents = None
            if getattr(rc, "sync_parent_links", True):
                logger.info("Batch fetching GitHub issue parents via GraphQL…")
                repo_parents = self.gh_client.get_repo_issue_parents(rc.owner, rc.repo)

            gh_issues = self.gh_client.get_issues(
                owner=rc.owner,
                repo_name=rc.repo,
                since=rc.issue_filter.since,
                state=rc.issue_filter.state,
                labels=rc.issue_filter.labels or None,
            )

        # Step 2.5 — targeted high-speed parallel property pre-fetch for matching repo issues ONLY
        if not dry_run and existing_lookup and gh_issues:
            keys_for_hash = set()
            keys_for_comments = set()
            for issue in gh_issues:
                gh_url = getattr(issue, "html_url", "")
                iss = existing_lookup.get(gh_url)
                if not iss:
                    summary_prefix = f"[{rc.repo}#{issue.number}]"
                    for candidate in existing_lookup.values():
                        if getattr(getattr(candidate, "fields", None), "summary", "").startswith(summary_prefix):
                            iss = candidate
                            break
                if iss:
                    setattr(iss, "_quill_cached_hash_checked", True)
                    if getattr(iss, "_quill_cached_hash", None) is None:
                        keys_for_hash.add(iss.key)
                    if getattr(iss, "_quill_cached_comments_count", None) is None and getattr(issue, "comments", 0) > 0:
                        keys_for_comments.add(iss.key)

            max_workers = self._get_max_workers(max(len(keys_for_hash), len(keys_for_comments), 1))
            if keys_for_hash:
                logger.debug(f"Batch fetching hash properties for {len(keys_for_hash)} repo issues…")
                cached_hashes = self.jira_client.batch_get_issue_properties(list(keys_for_hash), "quill-content-hash", max_workers=max_workers)
                for iss in existing_lookup.values():
                    if iss.key in cached_hashes:
                        setattr(iss, "_quill_cached_hash", cached_hashes[iss.key])
            if keys_for_comments:
                logger.debug(f"Batch fetching comments-count properties for {len(keys_for_comments)} repo issues…")
                cached_comments = self.jira_client.batch_get_issue_properties(list(keys_for_comments), "quill-synced-comments-count", max_workers=max_workers)
                for iss in existing_lookup.values():
                    if iss.key in cached_comments and cached_comments[iss.key] is not None:
                        try:
                            setattr(iss, "_quill_cached_comments_count", int(cached_comments[iss.key]))
                        except Exception:
                            pass

        stats = {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "comments": 0,
            "errors": 0,
        }
        level1_epics, level2_tasks, level3_subtasks = self._classify_hierarchy_levels(
            gh_issues, repo_parents, current_repo_full_name=rc.full_name, repo_project_fields=repo_project_fields
        )
        def _tier_sort_key(iss: Any) -> int:
            if iss.number in level1_epics:
                return 1
            if iss.number in level2_tasks:
                return 2
            if iss.number in level3_subtasks:
                return 3
            return 2
        gh_issues.sort(key=_tier_sort_key)

        pending_parent_links: dict[str, tuple[str, str, bool]] = {}

        stats_lock = threading.Lock()
        def _process_single_issue(issue: Any):
            try:
                self._sync_issue(
                    rc,
                    issue,
                    existing_lookup,
                    dry_run,
                    force,
                    stats,
                    repo_project_fields=repo_project_fields,
                    repo_parents=repo_parents,
                    pending_parent_links=pending_parent_links,
                    level1_epics=level1_epics,
                    level2_tasks=level2_tasks,
                    level3_subtasks=level3_subtasks,
                    stats_lock=stats_lock,
                )
            except Exception as exc:
                with stats_lock:
                    stats["errors"] += 1
                logger.error(
                    f"Error syncing issue #{issue.number} ({issue.title}): {exc}"
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=self._get_max_workers(len(gh_issues))) as executor:
            list(executor.map(_process_single_issue, gh_issues))

        # Step 4 — sync parent relationships
        self._sync_parent_relationships(rc, pending_parent_links, existing_lookup, dry_run)
        return stats

    @staticmethod
    def _classify_hierarchy_levels(
        gh_issues: list[Any],
        repo_parents: Optional[dict[int, str]],
        current_repo_full_name: Optional[str] = None,
        repo_project_fields: Optional[dict[int, dict[str, str]]] = None,
    ) -> tuple[set[int], set[int], set[int]]:
        """Classify issues in a repo into Level 1 (Epics), Level 2 (Tasks), and Level 3 (Sub-tasks)."""
        level1_epics: set[int] = set()
        level2_tasks: set[int] = set()
        level3_subtasks: set[int] = set()

        def _is_explicit_epic(issue_obj: Any, issue_num: int) -> bool:
            labels = [getattr(lbl, "name", str(lbl)) for lbl in getattr(issue_obj, "labels", [])]
            if any(lbl.lower() == "epic" for lbl in labels):
                return True
            title = getattr(issue_obj, "title", str(issue_obj))
            if re.search(r"^\s*\[?epic\]?:?|\s*\[epic\]|^\s*epic\s*[:\-]", title, re.IGNORECASE):
                return True
            if repo_project_fields and issue_num in repo_project_fields:
                for k, v in repo_project_fields[issue_num].items():
                    if k.lower() in ("type", "issue type", "kind") and str(v).lower() == "epic":
                        return True
            return False

        if not repo_parents:
            # If there's no parent hierarchy, check for explicit Epic labels/fields/titles or default to Task
            for issue in gh_issues:
                num = issue.number
                if _is_explicit_epic(issue, num):
                    level1_epics.add(num)
                else:
                    level2_tasks.add(num)
            return level1_epics, level2_tasks, level3_subtasks

        parent_numbers: set[int] = set()
        child_numbers: set[int] = set(repo_parents.keys())
        children_by_parent: dict[int, set[int]] = {}

        for child_num, pref in repo_parents.items():
            if not pref:
                continue
            if current_repo_full_name and "/" in pref:
                # If pref contains a slash (e.g. cross-repo URL like https://github.com/casangi/RADPS-roadmap/issues/118),
                # ensure it points to the current repository before adding its ID to parent_numbers.
                if f"/{current_repo_full_name}/" not in pref and not pref.startswith(f"{current_repo_full_name}/"):
                    continue
            clean_num = re.sub(r"[^0-9]", "", pref.split("/")[-1] if "/" in pref else pref)
            if clean_num and clean_num.isdigit():
                p_num = int(clean_num)
                parent_numbers.add(p_num)
                children_by_parent.setdefault(p_num, set()).add(child_num)

        # Pass 1: Identify Level 1 Epics
        # An issue is classified as a Level 1 Epic if:
        # 1. It explicitly has an 'epic' label, project field, or '[Epic]' / 'Epic:' in title
        # 2. It has children and no parent inside the repository (root container)
        # 3. It has children AND any of its children also have children (i.e. depth >= 3 tree where this node is the container)
        for issue in gh_issues:
            num = issue.number
            has_parent = num in child_numbers
            has_children = num in parent_numbers
            is_explicit = _is_explicit_epic(issue, num)

            if is_explicit or (has_children and not has_parent):
                level1_epics.add(num)
            elif has_children and any(c in parent_numbers for c in children_by_parent.get(num, set())):
                level1_epics.add(num)

        # Pass 2: Classify Level 2 Tasks vs Level 3 Sub-tasks consistently by parent tier
        for issue in gh_issues:
            num = issue.number
            if num in level1_epics:
                continue

            has_parent = num in child_numbers
            has_children = num in parent_numbers

            if not has_parent:
                level2_tasks.add(num)
                continue

            pref = repo_parents.get(num, "")
            clean_p = re.sub(r"[^0-9]", "", pref.split("/")[-1] if "/" in pref else pref)
            p_num = int(clean_p) if clean_p and clean_p.isdigit() else -1

            if p_num in level1_epics:
                # Direct children of Epics MUST always be Level 2 Tasks
                level2_tasks.add(num)
            elif has_children:
                # If this node itself has children (and parent wasn't Level 1 Epic), treat as Level 2 Task
                # and promote parent to Level 1 Epic so all siblings are at Level 2 Tasks
                level2_tasks.add(num)
                if p_num != -1 and p_num not in child_numbers:
                    level1_epics.add(p_num)
                    # Also move any already-processed siblings under p_num from level3_subtasks to level2_tasks
                    for sibling in children_by_parent.get(p_num, set()):
                        if sibling in level3_subtasks:
                            level3_subtasks.remove(sibling)
                            level2_tasks.add(sibling)
            elif p_num in level2_tasks or (p_num in parent_numbers and p_num in child_numbers):
                level3_subtasks.add(num)
            else:
                level2_tasks.add(num)

        return level1_epics, level2_tasks, level3_subtasks

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
        repo_parents: Optional[dict[int, str]] = None,
        pending_parent_links: Optional[dict[str, Any]] = None,
        level1_epics: Optional[set[int]] = None,
        level2_tasks: Optional[set[int]] = None,
        level3_subtasks: Optional[set[int]] = None,
        stats_lock: Optional[Any] = None,
    ):
        """Process a single GitHub issue: create or update corresponding Jira issue."""
        def _inc_stat(k: str):
            if stats_lock:
                with stats_lock:
                    stats[k] += 1
            else:
                stats[k] += 1
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

        target_issue_type = self.config.jira_default_issue_type
        if level1_epics and issue.number in level1_epics:
            target_issue_type = "Epic"
        elif any(lbl.lower() == "epic" for lbl in issue_labels):
            target_issue_type = "Epic"
        elif repo_project_fields and issue.number in repo_project_fields:
            for k, v in repo_project_fields[issue.number].items():
                if k.lower() in ("type", "issue type", "kind") and str(v).lower() == "epic":
                    target_issue_type = "Epic"
                    break

        parent_jira_key_for_create = None
        parent_jira_key_for_migration = None
        if repo_parents and issue.number in repo_parents:
            pref = repo_parents[issue.number]
            if pref:
                p_key = self._resolve_parent_jira_key(pref, lookup, rc.jira_project)
                if p_key:
                    if level3_subtasks and issue.number in level3_subtasks:
                        target_issue_type = self.jira_client.discover_subtask_issue_type()
                        parent_jira_key_for_create = p_key
                    parent_jira_key_for_migration = p_key

        if jira_issue is None:
            # ── NEW ──────────────────────────────────────────────────
            logger.info(f"Issue #{issue.number} is new. Creating in Jira (type: {target_issue_type})…")
            if not dry_run:
                jira_key = self.jira_client.create_issue(
                    project=rc.jira_project,
                    summary=summary,
                    description=jira_description,
                    issue_type=target_issue_type,
                    labels=all_labels,
                    github_link_field=github_link_field,
                    github_url=gh_url,
                    due_date=due_date_str,
                    parent_key=parent_jira_key_for_create,
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
                self.jira_client.set_issue_property(
                    jira_key, "quill-synced-labels", all_labels
                )
                if lookup is not None:
                    import types
                    if stats_lock:
                        with stats_lock:
                            lookup[gh_url] = types.SimpleNamespace(key=jira_key)
                    else:
                        lookup[gh_url] = types.SimpleNamespace(key=jira_key)
                logger.info(f"Created {jira_key} for GH #{issue.number}")
            else:
                jira_key = None
                self._preview_issue(
                    action="CREATE",
                    project=rc.jira_project,
                    summary=summary,
                    issue_type=target_issue_type,
                    labels=all_labels,
                    gh_state=issue.state,
                    gh_url=gh_url,
                    description=jira_description,
                )
            _inc_stat("created")

        else:
            # ── EXISTS — check for changes ─────────────────────────────────
            jira_key = jira_issue.key

            # Check if issue type migration is needed (e.g., demoting Epic to Task or converting to Sub-task)
            curr_issue_type = getattr(getattr(jira_issue.fields, "issuetype", None), "name", "")
            if curr_issue_type and target_issue_type and curr_issue_type.lower() != target_issue_type.lower():
                is_target_subtask = "sub-task" in target_issue_type.lower() or "subtask" in target_issue_type.lower()
                if curr_issue_type.lower() == "epic" and target_issue_type.lower() == "task" and not parent_jira_key_for_migration and not parent_jira_key_for_create:
                    logger.debug(f"Issue {jira_key} is currently an Epic without parent; skipping demotion to Task to preserve hierarchy container.")
                elif not (is_target_subtask and not parent_jira_key_for_create):
                    if not dry_run:
                        res_type = self.jira_client.update_issue_type(jira_key, target_issue_type, parent_key=parent_jira_key_for_migration or parent_jira_key_for_create)
                        if isinstance(res_type, str):
                            jira_key = res_type
                            try:
                                jira_issue = self.jira_client.jira.issue(jira_key)
                            except Exception:
                                pass
                            if lookup is not None:
                                import types
                                if stats_lock:
                                    with stats_lock:
                                        lookup[gh_url] = types.SimpleNamespace(key=jira_key)
                                else:
                                    lookup[gh_url] = types.SimpleNamespace(key=jira_key)
                    else:
                        logger.info(f"[Dry Run] Would migrate {jira_key} issue type from '{curr_issue_type}' to '{target_issue_type}'")

            if ((target_issue_type and target_issue_type.lower() == "epic") or (curr_issue_type and curr_issue_type.lower() == "epic")) and not dry_run:
                self.jira_client.set_epic_name(jira_key, summary)

            # Primary: read hash from pre-fetched cache or issue property (zero HTTP cost)
            existing_hash = getattr(jira_issue, "_quill_cached_hash", None)
            if existing_hash is None:
                existing_hash = self.jira_client.get_issue_property_from_issue(
                    jira_issue, "quill-content-hash"
                )
                if existing_hash is not None:
                    setattr(jira_issue, "_quill_cached_hash", existing_hash)
            if existing_hash is None:
                existing_hash = extract_hash_footer(
                    getattr(jira_issue.fields, "description", "") or ""
                )
                if existing_hash is not None:
                    setattr(jira_issue, "_quill_cached_hash", existing_hash)
            # Fallback: query Jira entity property ONLY if not batch-checked during pre-fetch
            if existing_hash is None and not getattr(jira_issue, "_quill_cached_hash_checked", False):
                existing_hash = self.jira_client.get_issue_property(
                    jira_key, "quill-content-hash"
                )
                if existing_hash is not None:
                    setattr(jira_issue, "_quill_cached_hash", existing_hash)

            # --force: ignore stored hash and always update
            if force:
                existing_hash = None

            if existing_hash != current_hash:
                merged_labels = self._merge_jira_labels(jira_issue, jira_key, all_labels)
                logger.info(
                    f"Issue #{issue.number} changed (hash mismatch). "
                    f"Updating {jira_key}…"
                )
                if not dry_run:
                    self.jira_client.update_issue(
                        issue_key=jira_key,
                        summary=summary,
                        description=jira_description,
                        labels=merged_labels,
                        due_date=due_date_str,
                    )
                    self.jira_client.set_issue_property(
                        jira_key, "quill-content-hash", current_hash
                    )
                    self.jira_client.set_issue_property(
                        jira_key, "quill-github-url", gh_url
                    )
                    self.jira_client.set_issue_property(
                        jira_key, "quill-synced-labels", all_labels
                    )
                else:
                    self._preview_issue(
                        action="UPDATE",
                        project=rc.jira_project,
                        summary=summary,
                        issue_type=self.config.jira_default_issue_type,
                        labels=merged_labels,
                        gh_state=issue.state,
                        gh_url=gh_url,
                        description=jira_description,
                        jira_key=jira_key,
                    )
                _inc_stat("updated")
            else:
                logger.debug(f"Issue #{issue.number} unchanged → skip.")
                _inc_stat("skipped")

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

        # ── Parent / Epic relationship check ─────────────────────────
        if getattr(rc, "sync_parent_links", True):
            parent_ref = None
            if repo_parents and issue.number in repo_parents:
                parent_ref = repo_parents[issue.number]
            elif repo_project_fields and issue.number in repo_project_fields:
                for k, v in repo_project_fields[issue.number].items():
                    if k.lower() in ("parent", "parent issue", "epic", "epic link") and v:
                        parent_ref = v
                        break
            elif rc.sync_project_fields:
                pfields = self.gh_client.get_issue_project_fields(issue)
                for k, v in pfields.items():
                    if k.lower() in ("parent", "parent issue", "epic", "epic link") and v:
                        parent_ref = v
                        break
            if parent_ref and (jira_key or dry_run) and pending_parent_links is not None:
                promote_flag = True
                clean_p = re.sub(r"[^0-9]", "", parent_ref.split("/")[-1] if "/" in parent_ref else parent_ref)
                p_num = int(clean_p) if clean_p and clean_p.isdigit() else -1
                if (level3_subtasks and issue.number in level3_subtasks) or (level2_tasks and p_num in level2_tasks) or (p_num != -1 and p_num in repo_parents):
                    promote_flag = False
                if pending_parent_links is not None:
                    if stats_lock:
                        with stats_lock:
                            pending_parent_links[gh_url] = (jira_key or f"GH #{issue.number}", parent_ref, promote_flag)
                    else:
                        pending_parent_links[gh_url] = (jira_key or f"GH #{issue.number}", parent_ref, promote_flag)

        # ── Comment sync ─────────────────────────────────────────────
        if rc.sync_comments and getattr(issue, "comments", 0) > 0:
            if jira_key:
                self._sync_comments(rc, issue, jira_key, dry_run, stats, jira_issue=jira_issue, force=force, stats_lock=stats_lock)
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

    def _merge_jira_labels(
        self, jira_issue: Any, jira_key: str, all_labels: list[str]
    ) -> list[str]:
        """Preserve any custom labels added on the Jira side while updating Quill-managed labels.

        Subtracts previously synced labels (stored in the 'quill-synced-labels' entity property)
        and Quill label prefixes from the existing Jira labels to find Jira-originated labels.
        """
        existing_jira_labels = getattr(getattr(jira_issue, "fields", None), "labels", None) or []
        if not isinstance(existing_jira_labels, list):
            existing_jira_labels = []

        last_synced_labels = getattr(jira_issue, "_quill_cached_synced_labels", None)
        if last_synced_labels is None:
            last_synced_labels = self.jira_client.get_issue_property_from_issue(
                jira_issue, "quill-synced-labels"
            )
            if last_synced_labels is not None:
                setattr(jira_issue, "_quill_cached_synced_labels", last_synced_labels)
        if last_synced_labels is None:
            last_synced_labels = self.jira_client.get_issue_property(
                jira_key, "quill-synced-labels"
            )
            if last_synced_labels is not None:
                setattr(jira_issue, "_quill_cached_synced_labels", last_synced_labels)

        if isinstance(last_synced_labels, list):
            last_synced_set = set(last_synced_labels)
            jira_originated_labels = [
                lb for lb in existing_jira_labels
                if lb not in last_synced_set
                and not lb.startswith(("proj-", "milestone-"))
                and lb != "github-synced"
            ]
        else:
            # Fallback for tickets that haven't been updated since the entity property was introduced
            jira_originated_labels = [
                lb for lb in existing_jira_labels
                if not lb.startswith(("proj-", "milestone-"))
                and lb != "github-synced"
                and lb not in all_labels
            ]

        merged = sorted(set(all_labels + jira_originated_labels))
        return merged

    def _resolve_parent_jira_key(
        self, parent_ref: str, lookup: dict[str, Any], project: str
    ) -> Optional[str]:
        if not parent_ref:
            return None
        parent_ref = parent_ref.strip()
        if re.match(r"^[A-Z][A-Z0-9]+-\d+$", parent_ref):
            return parent_ref

        if parent_ref in lookup:
            p_issue = lookup[parent_ref]
            if getattr(p_issue, "key", None):
                return p_issue.key

        clean_num = re.sub(r"[^0-9]", "", parent_ref)
        if clean_num:
            for rc in self.config.repos:
                if rc.jira_project == project:
                    url = f"https://github.com/{rc.owner}/{rc.repo}/issues/{clean_num}"
                    if url in lookup:
                        p_issue = lookup[url]
                        if getattr(p_issue, "key", None):
                            return p_issue.key

        if clean_num:
            for rc in self.config.repos:
                if rc.jira_project == project:
                    prefix = f"[{rc.repo}#{clean_num}]"
                    p_issue = self.jira_client.find_by_summary_prefix(project, prefix)
                    if p_issue and getattr(p_issue, "key", None):
                        url = f"https://github.com/{rc.owner}/{rc.repo}/issues/{clean_num}"
                        lookup[url] = p_issue
                        return p_issue.key
        return None

    def _sync_parent_relationships(
        self,
        rc: GitHubRepoConfig,
        pending_parent_links: dict[str, Any],
        lookup: dict[str, Any],
        dry_run: bool,
    ):
        if not getattr(rc, "sync_parent_links", True) or not pending_parent_links:
            return

        logger.info(f"Syncing parent/epic relationships for {len(pending_parent_links)} issues in {rc.full_name} in parallel…")
        def _process_parent_link(item: tuple[str, Any]):
            child_gh_url, link_tuple = item
            child_jira_key, parent_ref = link_tuple[0], link_tuple[1]
            promote_to_epic = link_tuple[2] if len(link_tuple) > 2 else True
            parent_jira_key = self._resolve_parent_jira_key(
                parent_ref, lookup, rc.jira_project
            )
            if not parent_jira_key:
                if not dry_run:
                    logger.debug(f"Could not resolve Jira key for parent reference '{parent_ref}' of {child_jira_key}")
                return

            if not dry_run:
                self.jira_client.set_parent_issue(
                    issue_key=child_jira_key,
                    parent_key=parent_jira_key,
                    epic_link_field=self.config.jira_epic_link_field,
                    promote_to_epic=promote_to_epic,
                )
            else:
                logger.info(
                    f"[Dry Run] Would link {child_jira_key} to parent/epic {parent_jira_key} (promote={promote_to_epic})"
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=self._get_max_workers(len(pending_parent_links))) as executor:
            list(executor.map(_process_parent_link, pending_parent_links.items()))

    # ── Comment sync ──────────────────────────────────────────────────────

    def _sync_comments(
        self,
        rc: GitHubRepoConfig,
        issue: Any,
        jira_key: str,
        dry_run: bool,
        stats: dict[str, int],
        jira_issue: Any = None,
        force: bool = False,
        stats_lock: Optional[Any] = None,
    ):
        def _inc_stat(k: str):
            if stats_lock:
                with stats_lock:
                    stats[k] += 1
            else:
                stats[k] += 1

        if not force and jira_issue is not None:
            cached_cnt = getattr(jira_issue, "_quill_cached_comments_count", None)
            if cached_cnt is not None and cached_cnt == getattr(issue, "comments", 0):
                return

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
                _inc_stat("comments")

        if not dry_run:
            try:
                self.jira_client.set_issue_property(
                    jira_key, "quill-synced-comments-count", str(getattr(issue, "comments", 0))
                )
                if jira_issue is not None:
                    setattr(jira_issue, "_quill_cached_comments_count", int(getattr(issue, "comments", 0)))
            except Exception:
                pass

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

