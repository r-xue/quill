import warnings
import requests
import concurrent.futures
from typing import Optional, Any, Union
from jira import JIRA
from quill.log import logger


class JiraClient:
    def __init__(self, server: str, token: str, verify_ssl: bool = True):
        self.server = server
        self._token = token
        self._verify_ssl = verify_ssl
        options = {"verify": verify_ssl}
        # Suppress the non-actionable "Unable to gather applicationlinks" warning
        # that python-jira emits when the user lacks Jira admin permission.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*applicationlinks.*")
            self.jira = JIRA(server=server, token_auth=token, options=options)

    def test_connection(self) -> str:
        """Verify credentials and connection.

        Returns:
            The display name of the authenticated user.
        """
        user = self.jira.myself()
        return user.get("displayName", "Authenticated User")

    def discover_epic_link_field_id(self, default: Optional[str] = "customfield_10014") -> Optional[str]:
        """Dynamically discover the real custom field ID for 'Epic Link' on this Jira server."""
        if hasattr(self, "_discovered_epic_link_field") and self._discovered_epic_link_field is not None:
            return self._discovered_epic_link_field
        try:
            for f in self.jira.fields():
                if f.get("name") == "Epic Link" and f.get("id", "").startswith("customfield_"):
                    self._discovered_epic_link_field = f["id"]
                    logger.debug(f"Discovered Epic Link custom field ID: {self._discovered_epic_link_field}")
                    return self._discovered_epic_link_field
        except Exception as exc:
            logger.debug(f"Could not query Jira field metadata ({exc}); using default {default}")
        self._discovered_epic_link_field = default
        return self._discovered_epic_link_field

    def discover_epic_name_field_id(self, default: Optional[str] = "customfield_10001") -> Optional[str]:
        """Dynamically discover the real custom field ID for 'Epic Name' on this Jira server."""
        if hasattr(self, "_discovered_epic_name_field") and self._discovered_epic_name_field is not None:
            return self._discovered_epic_name_field
        try:
            for f in self.jira.fields():
                if f.get("name") == "Epic Name" and f.get("id", "").startswith("customfield_"):
                    self._discovered_epic_name_field = f["id"]
                    logger.debug(f"Discovered Epic Name custom field ID: {self._discovered_epic_name_field}")
                    return self._discovered_epic_name_field
        except Exception as exc:
            logger.debug(f"Could not query Jira field metadata ({exc}); using default {default}")
        self._discovered_epic_name_field = default
        return self._discovered_epic_name_field

    def set_epic_name(self, issue_key: str, epic_name: str) -> bool:
        """Set the Epic Name custom field on a Jira Epic issue safely."""
        epic_name_field = self.discover_epic_name_field_id()
        if not epic_name_field or not epic_name:
            return False
        truncated_name = epic_name[:250]
        try:
            issue = self.jira.issue(issue_key)
            curr_val = getattr(issue.fields, epic_name_field, None)
            if curr_val == truncated_name:
                return True
            issue.update(fields={epic_name_field: truncated_name})
            logger.debug(f"Set Epic Name on {issue_key} to '{truncated_name}'")
            return True
        except Exception as exc:
            logger.debug(f"Could not set Epic Name ({epic_name_field}) on {issue_key}: {exc}")
            return False

    def discover_subtask_issue_type(self, default: str = "Sub-task") -> str:
        """Dynamically discover the real issue type name for sub-tasks on this Jira server."""
        if hasattr(self, "_discovered_subtask_type") and self._discovered_subtask_type is not None:
            return self._discovered_subtask_type
        try:
            issue_types = self.jira.issue_types()
            # 1. First priority: exact standard names ('Sub-task' or 'Subtask') that are marked as subtask
            for t in issue_types:
                if getattr(t, "subtask", False) and t.name.lower() in ("sub-task", "subtask"):
                    self._discovered_subtask_type = t.name
                    logger.debug(f"Discovered exact Sub-task issue type name: {self._discovered_subtask_type}")
                    return self._discovered_subtask_type
            # 2. Second priority: any issue type literally named 'Sub-task' or 'Subtask'
            for t in issue_types:
                if t.name.lower() in ("sub-task", "subtask"):
                    self._discovered_subtask_type = t.name
                    logger.debug(f"Discovered Sub-task issue type name by string match: {self._discovered_subtask_type}")
                    return self._discovered_subtask_type
            # 3. Fallback: any issue type marked subtask=True (e.g. 'Engineering Sub-Task')
            for t in issue_types:
                if getattr(t, "subtask", False):
                    self._discovered_subtask_type = t.name
                    logger.debug(f"Discovered fallback subtask issue type name: {self._discovered_subtask_type}")
                    return self._discovered_subtask_type
        except Exception as exc:
            logger.debug(f"Could not query Jira issue types ({exc}); using default {default}")
        self._discovered_subtask_type = default
        return self._discovered_subtask_type

    # ── Stateless lookup via JQL ──────────────────────────────────────────

    @staticmethod
    def get_issue_property_from_issue(issue: Any, property_key: str) -> Any | None:
        """Extract a property value pre-fetched inside a search_issues response object."""
        raw = getattr(issue, "raw", None)
        if isinstance(raw, dict):
            props = raw.get("properties")
            if isinstance(props, dict):
                prop = props.get(property_key)
                if isinstance(prop, dict) and "value" in prop:
                    return prop["value"]
                elif prop is not None and not isinstance(prop, dict):
                    return prop
        return None

    def get_synced_issues(
        self, project: str, github_link_field: str
    ) -> dict[str, Any]:
        """Query Jira for synced issues by custom field.

        Falls back to a label/summary scan if the custom field is disabled
        or not available on the project's screen scheme.

        Args:
            project: The Jira project key.
            github_link_field: The custom field ID for the GitHub link.

        Returns:
            A mapping from GitHub URL to the Jira Issue object.
        """
        if github_link_field:
            jql = (
                f'project = "{project}" AND cf[{_cf_id(github_link_field)}] is not EMPTY'
            )
            logger.info(f"JQL lookup: {jql}")
            try:
                issues = self.jira.search_issues(
                    jql,
                    maxResults=False,
                    fields=f"summary,description,status,labels,issuetype,parent,{github_link_field}",
                    properties="quill-content-hash,quill-github-url,quill-synced-labels,quill-synced-comments-count",
                )
                lookup: dict[str, Any] = {}
                for issue in issues:
                    setattr(issue, "_quill_cached_hash_checked", True)
                    gh_url = getattr(issue.fields, github_link_field, None)
                    if gh_url:
                        lookup[gh_url] = issue
                        cached_hash = self.get_issue_property_from_issue(
                            issue, "quill-content-hash"
                        )
                        if cached_hash:
                            setattr(issue, "_quill_cached_hash", cached_hash)
                        cached_synced_labels = self.get_issue_property_from_issue(
                            issue, "quill-synced-labels"
                        )
                        if cached_synced_labels is not None:
                            setattr(issue, "_quill_cached_synced_labels", cached_synced_labels)
                        cached_comments_count = self.get_issue_property_from_issue(
                            issue, "quill-synced-comments-count"
                        )
                        if cached_comments_count is not None:
                            try:
                                setattr(issue, "_quill_cached_comments_count", int(cached_comments_count))
                            except Exception:
                                pass
                if lookup:
                    return lookup
                # Custom field returned nothing — could be field not set yet.
                # Fall through to summary-based scan as a safety net.
                logger.debug(
                    f"Custom field lookup returned 0 results for {project}; "
                    "trying summary-prefix scan."
                )
            except Exception as exc:
                logger.warning(
                    f"Custom field JQL failed ({exc}); falling back to summary scan."
                )

        # ── Fallback: scan by the 'github-synced' label ─────────────────
        # Every quill-created issue gets this label, making it a reliable
        # anchor for deduplication when customfield_10200 is unavailable.
        # Bracket chars in JQL text search are invalid, so we can't search
        # by summary prefix directly in the batch query.
        fallback_jql = (
            f'project = "{project}" AND labels = "github-synced" '
            f'ORDER BY created DESC'
        )
        logger.info(f"JQL fallback (label-based): {fallback_jql}")
        try:
            issues = self.jira.search_issues(
                fallback_jql,
                maxResults=False,
                fields="summary,description,status,labels,issuetype,parent",
                properties="quill-content-hash,quill-github-url,quill-synced-labels,quill-synced-comments-count",
            )
        except Exception as exc:
            logger.warning(f"Fallback JQL also failed: {exc}")
            return {}

        lookup = {}
        unresolved_issues = []  # issues where description parsing didn't find a URL
        for issue in issues:
            setattr(issue, "_quill_cached_hash_checked", True)
            summary = getattr(issue.fields, "summary", "") or ""
            cached_hash = self.get_issue_property_from_issue(
                issue, "quill-content-hash"
            )
            if cached_hash:
                setattr(issue, "_quill_cached_hash", cached_hash)
            cached_synced_labels = self.get_issue_property_from_issue(
                issue, "quill-synced-labels"
            )
            if cached_synced_labels is not None:
                setattr(issue, "_quill_cached_synced_labels", cached_synced_labels)
            cached_comments_count = self.get_issue_property_from_issue(
                issue, "quill-synced-comments-count"
            )
            if cached_comments_count is not None:
                try:
                    setattr(issue, "_quill_cached_comments_count", int(cached_comments_count))
                except Exception:
                    pass

            # Primary: check pre-fetched entity property (zero HTTP cost)
            gh_url = self.get_issue_property_from_issue(issue, "quill-github-url")
            if not gh_url:
                # Secondary: extract GitHub URL from description text
                description = getattr(issue.fields, "description", "") or ""
                gh_url = _extract_github_url(description)
            if gh_url:
                lookup[gh_url] = issue
            else:
                unresolved_issues.append((issue, summary))

        # Secondary: batch-fetch entity properties only for unresolved issues
        if unresolved_issues:
            unresolved_keys = [iss.key for iss, _ in unresolved_issues]
            logger.debug(
                f"Description parsing missed {len(unresolved_keys)} issues; "
                f"batch-fetching quill-github-url properties…"
            )
            url_map = self.batch_get_issue_properties(unresolved_keys, "quill-github-url")
            for issue, summary in unresolved_issues:
                gh_url = url_map.get(issue.key)
                if gh_url:
                    lookup[gh_url] = issue
                else:
                    # No URL found — key by summary for per-issue fallback match
                    lookup[f"__summary__{summary}"] = issue
        return lookup

    def find_by_summary_prefix(self, project: str, prefix: str) -> Any:
        """Find a single Jira issue whose summary starts with the prefix.

        Used as a per-issue fallback when the batch lookup misses an issue.
        Uses label + Python-side filtering because Jira JQL text search
        cannot handle bracket characters in `summary ~` queries.

        Args:
            project: The Jira project key.
            prefix: The summary prefix string to search for.

        Returns:
            The matched Jira issue object, or None if not found.
        """
        jql = (
            f'project = "{project}" AND labels = "github-synced" '
            f'ORDER BY created DESC'
        )
        try:
            issues = self.jira.search_issues(
                jql,
                maxResults=200,
                fields="summary,description,status,labels",
                properties="quill-content-hash,quill-github-url,quill-synced-labels",
            )
            for issue in issues:
                s = getattr(issue.fields, "summary", "") or ""
                if s.startswith(prefix):
                    cached_hash = self.get_issue_property_from_issue(
                        issue, "quill-content-hash"
                    )
                    if cached_hash:
                        setattr(issue, "_quill_cached_hash", cached_hash)
                    cached_synced_labels = self.get_issue_property_from_issue(
                        issue, "quill-synced-labels"
                    )
                    if cached_synced_labels is not None:
                        setattr(issue, "_quill_cached_synced_labels", cached_synced_labels)
                    return issue
        except Exception as exc:
            logger.debug(f"Summary prefix lookup failed: {exc}")
        return None


    # ── CRUD ──────────────────────────────────────────────────────────────

    def create_issue(
        self,
        project: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        labels: Optional[list[str]] = None,
        github_link_field: Optional[str] = None,
        github_url: Optional[str] = None,
        due_date: Optional[str] = None,
        parent_key: Optional[str] = None,
    ) -> str:
        """Create a new issue in Jira.

        The custom field is set via a separate update call after creation to
        work around Jira screen scheme restrictions.

        Args:
            project: The Jira project key.
            summary: The issue title.
            description: The issue body text.
            issue_type: The Jira issue type.
            labels: A list of labels to apply to the issue.
            github_link_field: The custom field ID for storing the GitHub URL.
            github_url: The actual GitHub URL to store.
            due_date: Optional due date string in YYYY-MM-DD format.
            parent_key: Optional Jira parent key when creating a Sub-task.

        Returns:
            The newly created issue key.
        """
        issue_dict: dict[str, Any] = {
            "project": {"key": project},
            "summary": summary,
            "description": description,
            "issuetype": {"name": issue_type},
        }
        if labels:
            # Jira labels cannot contain spaces.
            issue_dict["labels"] = [label.replace(" ", "-") for label in labels]

        is_subtask_type = "sub-task" in issue_type.lower() or "subtask" in issue_type.lower()
        if parent_key and is_subtask_type:
            issue_dict["parent"] = {"key": parent_key}

        logger.info(f"Creating Jira issue in project {project} (type: {issue_type})…")
        try:
            new_issue = self.jira.create_issue(fields=issue_dict)
        except Exception as exc:
            if is_subtask_type:
                logger.warning(f"Could not create '{summary[:40]}' as Sub-task under {parent_key} ({exc}); falling back to standard Task…")
                issue_dict["issuetype"] = {"name": "Task"}
                issue_dict.pop("parent", None)
                new_issue = self.jira.create_issue(fields=issue_dict)
            else:
                raise
        key = new_issue.key

        if issue_type.lower() == "epic":
            self.set_epic_name(key, summary)

        # Set the custom field via edit (bypasses create-screen restrictions)
        if github_link_field and github_url:
            self.set_custom_field(key, github_link_field, github_url)

        if due_date:
            self.set_due_date(key, due_date)

        return key

    def set_custom_field(self, issue_key: str, field_id: str, value: str):
        """
        Set a single custom field on an existing issue via the edit endpoint.

        The REST API ``PUT /rest/api/2/issue/{key}`` does not enforce screen
        scheme restrictions, so fields that are missing from the Create screen
        can still be set this way.
        """
        logger.info(f"Setting {field_id} on {issue_key}…")
        try:
            issue = self.jira.issue(issue_key)
            issue.update(fields={field_id: value})
        except Exception as exc:
            # Non-fatal — the issue was created; the custom field just won't
            # be set, so JQL lookups by that field won't find this issue.
            logger.warning(
                f"Could not set {field_id} on {issue_key}: {exc}. "
                f"JQL deduplication will fall back to summary matching."
            )

    def set_due_date(self, issue_key: str, due_date: str):
        """Set the due date on a Jira issue safely via the edit endpoint."""
        logger.info(f"Setting duedate ({due_date}) on {issue_key}…")
        try:
            issue = self.jira.issue(issue_key)
            issue.update(fields={"duedate": due_date})
        except Exception as exc:
            logger.warning(
                f"Could not set duedate on {issue_key}: {exc}. "
                f"Ensure the Due Date field is enabled on the edit screen."
            )

    def _get_issue_type_obj(self, type_name: str) -> Any | None:
        """Helper to find an IssueType object by name."""
        try:
            for t in self.jira.issue_types():
                if t.name.lower() == type_name.lower():
                    return t
        except Exception:
            pass
        return None

    def update_issue_type(
        self, issue_key: str, new_type: str, parent_key: Optional[str] = None
    ) -> Union[bool, str]:
        """Update an existing Jira issue's issue type (or re-create as sub-task and delete stale top-level task)."""
        try:
            issue = self.jira.issue(issue_key)
            curr_type = getattr(getattr(issue.fields, "issuetype", None), "name", "")
            if curr_type.lower() == new_type.lower():
                return False
            logger.info(f"Migrating issue type for {issue_key} from '{curr_type}' to '{new_type}'…")
            if parent_key:
                try:
                    issue.update(fields={"parent": {"key": parent_key}})
                except Exception as exc_p:
                    logger.debug(f"Could not pre-set parent {parent_key} on {issue_key}: {exc_p}")
            fields_to_update: dict[str, Any] = {"issuetype": {"name": new_type}}
            new_type_obj = self._get_issue_type_obj(new_type)
            if new_type_obj and getattr(new_type_obj, "id", None):
                fields_to_update["issuetype"] = {"id": str(new_type_obj.id), "name": new_type}

            if parent_key:
                try:
                    p_obj = self.jira.issue(parent_key)
                    fields_to_update["parent"] = {"key": parent_key, "id": str(p_obj.id)}
                except Exception:
                    fields_to_update["parent"] = {"key": parent_key}

            try:
                issue.update(fields=fields_to_update)
                if new_type.lower() == "epic":
                    summary = getattr(issue.fields, "summary", str(issue_key))
                    self.set_epic_name(issue_key, summary)
                return True
            except Exception as exc1:
                if parent_key and new_type_obj and getattr(new_type_obj, "id", None):
                    try:
                        # Try legacy server fields (`parentIssueKey` / `parentIssueId`)
                        p_id = fields_to_update.get("parent", {}).get("id")
                        alt_fields = {"issuetype": {"id": str(new_type_obj.id)}}
                        if p_id:
                            alt_fields["parentIssueId"] = p_id
                        else:
                            alt_fields["parentIssueKey"] = parent_key
                        issue.update(fields=alt_fields)
                        return True
                    except Exception:
                        pass
                raise exc1
        except Exception as exc:
            err_msg = getattr(exc, "text", str(exc)).split("\n")[0]
            is_target_subtask = "sub-task" in new_type.lower() or "subtask" in new_type.lower()
            if parent_key and is_target_subtask:
                # Jira Server REST API forbids in-place conversion from top-level Task -> Sub-task across tiers.
                # If delete_stale is enabled, re-create as true subtask under parent and delete old stale ticket!
                new_key = self.recreate_as_subtask(
                    old_issue_key=issue_key,
                    new_type=new_type,
                    parent_key=parent_key,
                    github_link_field=getattr(self, "_last_github_link_field", None),
                )
                if new_key:
                    return new_key
                logger.debug(
                    f"Note: {issue_key} is linked to parent {parent_key} via 'parent' field/hierarchy, "
                    f"but Jira Server REST API prohibits converting top-level Task into a Sub-task ({err_msg})."
                )
            else:
                logger.warning(f"Could not migrate issue type for {issue_key} to '{new_type}': {err_msg}")
            return False

    def delete_issue(self, issue_key: str) -> bool:
        """Permanently delete an existing Jira issue."""
        try:
            issue = self.jira.issue(issue_key)
            issue.delete()
            logger.info(f"Deleted stale/legacy Jira issue {issue_key}")
            return True
        except Exception as exc:
            err_msg = getattr(exc, "text", str(exc)).split("\n")[0]
            logger.warning(f"Could not delete issue {issue_key}: {err_msg}")
            return False

    def recreate_as_subtask(
        self,
        old_issue_key: str,
        new_type: str,
        parent_key: str,
        github_link_field: Optional[str] = None,
    ) -> Optional[str]:
        """
        Re-create an existing top-level Task as a Sub-task under parent_key and delete the stale old issue.
        """
        try:
            logger.info(f"Re-creating {old_issue_key} as '{new_type}' under parent {parent_key} and deleting stale top-level issue…")
            old_issue = self.jira.issue(old_issue_key)
            summary = getattr(old_issue.fields, "summary", str(old_issue_key))
            description = getattr(old_issue.fields, "description", "")
            project = getattr(getattr(old_issue.fields, "project", None), "key", "GITHUB")
            labels = getattr(old_issue.fields, "labels", [])
            due_date = getattr(old_issue.fields, "duedate", None)

            # Retrieve properties before deleting
            gh_url = self.get_issue_property_from_issue(old_issue, "quill-github-url")
            content_hash = self.get_issue_property_from_issue(old_issue, "quill-content-hash")
            synced_labels = self.get_issue_property_from_issue(old_issue, "quill-synced-labels")

            new_key = self.create_issue(
                project=project,
                summary=summary,
                description=description,
                issue_type=new_type,
                labels=labels,
                github_link_field=github_link_field,
                github_url=gh_url,
                due_date=due_date,
                parent_key=parent_key,
            )

            if gh_url:
                try:
                    self.add_remote_link(
                        issue_key=new_key,
                        github_url=gh_url,
                        title=f"GitHub #{gh_url.rstrip('/').split('/')[-1]}",
                    )
                except Exception as exc_rl:
                    logger.debug(f"Could not add remote link on re-created {new_key}: {exc_rl}")

            if content_hash:
                self.set_issue_property(new_key, "quill-content-hash", content_hash)
            if gh_url:
                self.set_issue_property(new_key, "quill-github-url", gh_url)
            if synced_labels:
                self.set_issue_property(new_key, "quill-synced-labels", synced_labels)

            # Delete the stale ticket rather than archiving
            try:
                old_issue.delete()
                logger.info(f"Successfully re-created {old_issue_key} -> {new_key} and deleted stale top-level ticket.")
            except Exception as exc_del:
                logger.warning(f"Re-created {new_key}, but could not delete stale ticket {old_issue_key}: {exc_del}")

            return new_key
        except Exception as exc:
            err_msg = getattr(exc, "text", str(exc)).split("\n")[0]
            logger.warning(f"Could not re-create {old_issue_key} as sub-task: {err_msg}")
            return None

    def update_issue(
        self,
        issue_key: str,
        summary: str,
        description: str,
        labels: Optional[list[str]] = None,
        due_date: Optional[str] = None,
    ):
        """
        Update an existing Jira issue's summary, description, and labels.
        """
        fields: dict[str, Any] = {
            "summary": summary,
            "description": description,
        }
        if labels is not None:
            fields["labels"] = [label.replace(" ", "-") for label in labels]

        logger.info(f"Updating Jira issue {issue_key}…")
        issue = self.jira.issue(issue_key)
        issue.update(fields=fields)

        if getattr(getattr(issue.fields, "issuetype", None), "name", "").lower() == "epic":
            self.set_epic_name(issue_key, summary)

        if due_date:
            self.set_due_date(issue_key, due_date)

    def set_parent_issue(
        self,
        issue_key: str,
        parent_key: str,
        epic_link_field: Optional[str] = "customfield_10014",
        promote_to_epic: bool = True,
    ) -> bool:
        """Link an issue to its parent or epic in Jira.

        Attempts 'Epic Link' custom field first (`customfield_10014`), then falls back
        to native 'parent' field (for subtasks & Team-Managed Cloud projects), and
        finally creates an Issue Link ('Parent / Child', 'Epic-Story Link', or 'Relates').
        """
        if issue_key == parent_key:
            return False

        logger.info(f"Linking {issue_key} -> parent/epic {parent_key}…")
        try:
            issue = self.jira.issue(issue_key)
        except Exception as exc:
            logger.error(f"Failed to fetch issue {issue_key} for parent linking: {exc}")
            return False

        # Check if already linked via parent field
        curr_parent = getattr(issue.fields, "parent", None)
        if getattr(curr_parent, "key", None) == parent_key or str(curr_parent) == parent_key:
            return False

        # Check if already linked via epic link field
        epic_field_id = self.discover_epic_link_field_id(epic_link_field)
        if epic_field_id:
            curr_epic = getattr(issue.fields, epic_field_id, None)
            if getattr(curr_epic, "key", None) == parent_key or str(curr_epic) == parent_key:
                return False

        # Check if parent is an Epic before linking; if not, attempt to promote it ONLY when promote_to_epic is True
        is_parent_epic = False
        try:
            parent_issue = self.jira.issue(parent_key)
            parent_type = getattr(getattr(parent_issue.fields, "issuetype", None), "name", "")
            parent_summary = getattr(parent_issue.fields, "summary", "") or f"{parent_key}"
            if parent_type.lower() == "epic":
                is_parent_epic = True
                # Ensure its Epic Name custom field is populated cleanly
                self.set_epic_name(parent_key, parent_summary)
            elif promote_to_epic:
                logger.info(f"Parent issue {parent_key} currently has issue type '{parent_type}' (not 'Epic'). Promoting to 'Epic' so Epic Link / Agile hierarchy works…")
                try:
                    parent_issue.update(fields={"issuetype": {"name": "Epic"}})
                    logger.info(f"Successfully promoted {parent_key} to issue type 'Epic'.")
                    self.set_epic_name(parent_key, parent_summary)
                    is_parent_epic = True
                except Exception as exc_up:
                    logger.debug(f"Could not automatically promote {parent_key} to 'Epic' ({exc_up}). Epic Link attempts may fail if parent must be an Epic.")
            else:
                logger.debug(f"Parent issue {parent_key} has issue type '{parent_type}'. Skipping Epic promotion to preserve strict Task/Sub-task hierarchy.")
        except Exception as exc_pf:
            logger.debug(f"Could not inspect parent issue {parent_key}: {exc_pf}")

        # Check if child issue itself is currently an Epic (an Epic cannot be added to another Epic via Epic Link)
        child_type = getattr(getattr(issue.fields, "issuetype", None), "name", "").lower()
        if child_type == "epic":
            logger.debug(f"Child issue {issue_key} is currently an Epic. Skipping Attempt 1 (Epic Link) and linking via native parent field / issue link.")
            is_parent_epic = False
            promote_to_epic = False

        # Attempt 1: Epic Link / Agile API (required for Epics on Jira Data Center, Server, & Cloud Classic)
        # Only run Attempt 1 if the target parent is actually an Epic or we attempted promoting it
        epic_linked = False
        if is_parent_epic or promote_to_epic:
            # 1a. Try Jira Agile / Software REST API (`add_issues_to_epic`) directly
            try:
                self.jira.add_issues_to_epic(epic_id=parent_key, issue_keys=[issue_key])
                logger.info(f"Linked {issue_key} to epic {parent_key} via Jira Agile API")
                epic_linked = True
            except Exception as exc_agile:
                logger.debug(f"Could not link {issue_key} -> {parent_key} via Agile API ({exc_agile}); trying custom field '{epic_field_id}'…")

            # 1b. Try Epic Link custom field (`epic_field_id`) via standard REST API
            if not epic_linked and epic_field_id:
                try:
                    issue.update(fields={epic_field_id: parent_key})
                    logger.info(f"Linked {issue_key} to epic {parent_key} via '{epic_field_id}'")
                    epic_linked = True
                except Exception as exc_cf:
                    err_msg = getattr(exc_cf, "text", str(exc_cf)).split("\n")[0]
                    logger.info(f"Could not link {issue_key} -> {parent_key} via Epic Link field '{epic_field_id}' ({err_msg}); trying native 'parent' field…")

        if epic_linked:
            # Also attempt setting native 'parent' field for universal compatibility across Jira Cloud views
            try:
                issue.update(fields={"parent": {"key": parent_key}})
            except Exception:
                pass
            return True

        # Attempt 2: native 'parent' field (for subtasks, Team-Managed Cloud projects, or Parent Link)
        try:
            issue.update(fields={"parent": {"key": parent_key}})
            logger.info(f"Linked {issue_key} to parent {parent_key} via 'parent' field")
            return True
        except Exception as exc2:
            logger.debug(f"Could not link {issue_key} -> {parent_key} via 'parent' field ({exc2}); trying Issue Links…")

        # Attempt 3: Issue Link (Parent / Child or Epic-Story Link or relates to)
        for link_type in ("Parent / Child", "Epic-Story Link", "Relates", "relates to"):
            try:
                self.jira.create_issue_link(type=link_type, inboundIssue=issue_key, outboundIssue=parent_key)
                logger.info(f"Linked {issue_key} to {parent_key} via '{link_type}' issue link")
                return True
            except Exception:
                continue

        logger.warning(f"Failed to link {issue_key} -> {parent_key} using parent, {epic_link_field}, or issue links.")
        return False

    # ── Comments ──────────────────────────────────────────────────────────

    def get_existing_comments(self, issue_key: str) -> list[str]:
        """
        Return comment bodies for a Jira issue.
        """
        issue = self.jira.issue(issue_key)
        comments = self.jira.comments(issue)
        return [c.body for c in comments]

    def add_comment(self, issue_key: str, body: str):
        logger.info(f"Adding comment to Jira issue {issue_key}…")
        self.jira.add_comment(issue_key, body)

    # ── Issue entity properties (invisible metadata) ───────────────────────
    # Jira entity properties are key-value JSON stored on an issue via the
    # REST API.  They are completely invisible in the Jira UI and don't
    # require any screen scheme configuration.
    # Endpoint: /rest/api/2/issue/{issueKey}/properties/{propertyKey}

    def get_issue_property(self, issue_key: str, property_key: str) -> Any:
        """
        Retrieve the value of an issue entity property, or None if not set.
        """
        url = f"{self.server}/rest/api/2/issue/{issue_key}/properties/{property_key}"
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                verify=self._verify_ssl,
            )
            if resp.status_code == 200:
                return resp.json().get("value")
            return None
        except Exception:
            return None

    def set_issue_property(self, issue_key: str, property_key: str, value: Any):
        """
        Store a JSON value as an issue entity property.
        Silently ignored on failure (property is non-critical metadata).
        """
        url = f"{self.server}/rest/api/2/issue/{issue_key}/properties/{property_key}"
        try:
            resp = requests.put(
                url,
                json=value,
                headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
                verify=self._verify_ssl,
            )
            if not resp.ok:
                logger.debug(
                    f"Could not set property {property_key} on {issue_key}: "
                    f"HTTP {resp.status_code} — {resp.text[:200]}"
                )
        except Exception as exc:
            logger.debug(f"set_issue_property failed for {issue_key}: {exc}")

    def batch_get_issue_properties(self, issue_keys: list[str], property_key: str) -> dict[str, Any]:
        """Fetch an entity property for multiple issues in parallel using a polite read-only thread pool."""
        if not issue_keys:
            return {}
        results: dict[str, Any] = {}
        # GitHub standard runners (ubuntu-latest) have 2 vCPUs. Using max_workers=4 is gentle on
        # network firewalls while still providing a 4x I/O speedup over serial requests.
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(issue_keys))) as executor:
            future_to_key = {
                executor.submit(self.get_issue_property, key, property_key): key
                for key in issue_keys
            }
            for future in concurrent.futures.as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    val = future.result()
                    if val is not None:
                        results[key] = val
                except Exception:
                    pass
        return results

    # ── Remote links ──────────────────────────────────────────────────────

    def add_remote_link(
        self,
        issue_key: str,
        github_url: str,
        title: str,
    ):
        """
        Add a remote issue link pointing back to the GitHub issue.

        Shows up in the Jira UI as a native "Links" entry with the GitHub
        favicon.  Uses ``globalId`` for idempotency — calling this twice
        with the same URL will not create duplicates on Jira Data Center.
        """
        logger.info(f"Adding remote link to {issue_key} → {github_url}")
        self.jira.add_remote_link(
            issue_key,
            destination={
                "url": github_url,
                "title": title,
                "icon": {
                    "url16x16": "https://github.com/favicon.ico",
                    "title": "GitHub",
                },
            },
            globalId=f"quill={github_url}",
            relationship="GitHub Issue",
        )

    # ── Transitions ───────────────────────────────────────────────────────

    def transition_issue(self, issue_key: str, transition_name: str) -> bool:
        """
        Transition an issue to a new state by transition name.
        Returns True if successful, False otherwise.
        """
        logger.info(f"Attempting to transition {issue_key} to '{transition_name}'…")
        transitions = self.jira.transitions(issue_key)
        available: list[str] = []
        transition_id = None
        for t in transitions:
            name = t.get("name", "")
            available.append(name)
            if name.lower() == transition_name.lower():
                transition_id = t.get("id")
                break

        if transition_id:
            self.jira.transition_issue(issue_key, transition_id)
            logger.info(f"Transitioned {issue_key} → '{transition_name}'.")
            return True
        else:
            logger.warning(
                f"Transition '{transition_name}' unavailable for {issue_key}. "
                f"Available: {available}"
            )
            return False


def _cf_id(field_name: str) -> str:
    """
    Extract the numeric ID from a ``customfield_NNNNN`` string so it can be
    used in JQL ``cf[NNNNN]`` syntax.
    """
    if field_name.startswith("customfield_"):
        return field_name.replace("customfield_", "")
    return field_name


def _extract_github_url(description: str) -> str | None:
    """
    Extract the GitHub issue URL from the Jira description.

    Supports two formats written by different quill versions:

    Modern (v5+) — panel header::

        *Source:* [owner/repo#N|https://github.com/owner/repo/issues/N]

    Legacy (< v5) — description footer::

        _Originally filed on GitHub:_ [owner/repo#N|https://github.com/...]

    Returns the URL string or None if not found.
    """
    import re
    # Modern panel header: *Source:* [text|url]
    match = re.search(
        r"\*Source:\*\s*\[.*?\|(https://github\.com/[^\]]+)\]",
        description,
    )
    if match:
        return match.group(1)
    # Legacy footer: _Originally filed on GitHub:_ [text|url]
    match = re.search(
        r"Originally filed on GitHub.*?\[.*?\|(https://github\.com/[^\]]+)\]",
        description,
    )
    if match:
        return match.group(1)
    return None
