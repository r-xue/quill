import warnings
import requests
import concurrent.futures
from typing import Optional, Any
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
                    fields=f"summary,description,status,labels,{github_link_field}",
                    properties="quill-content-hash,quill-github-url,quill-synced-labels",
                )
                lookup: dict[str, Any] = {}
                for issue in issues:
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
                fields="summary,description,status,labels",
                properties="quill-content-hash,quill-github-url,quill-synced-labels",
            )
        except Exception as exc:
            logger.warning(f"Fallback JQL also failed: {exc}")
            return {}

        lookup = {}
        unresolved_issues = []  # issues where description parsing didn't find a URL
        for issue in issues:
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

        logger.info(f"Creating Jira issue in project {project}…")
        new_issue = self.jira.create_issue(fields=issue_dict)
        key = new_issue.key

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

        if due_date:
            self.set_due_date(issue_key, due_date)

    def set_parent_issue(
        self,
        issue_key: str,
        parent_key: str,
        epic_link_field: Optional[str] = "customfield_10014",
    ) -> bool:
        """Link an issue to its parent or epic in Jira.

        Attempts native 'parent' field first (modern Jira Cloud & subtasks), then
        falls back to 'Epic Link' custom field (Jira Server/Data Center epics),
        and finally creates a 'Parent / Child' or 'Epic-Story Link' issue link.
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
        if epic_link_field:
            curr_epic = getattr(issue.fields, epic_link_field, None)
            if getattr(curr_epic, "key", None) == parent_key or str(curr_epic) == parent_key:
                return False

        # Attempt 1: native 'parent' field
        try:
            issue.update(fields={"parent": {"key": parent_key}})
            logger.info(f"Linked {issue_key} to parent {parent_key} via 'parent' field")
            return True
        except Exception as exc1:
            logger.debug(f"Could not link {issue_key} -> {parent_key} via 'parent' field ({exc1}); trying Epic Link field…")

        # Attempt 2: Epic Link field
        if epic_link_field:
            try:
                issue.update(fields={epic_link_field: parent_key})
                logger.info(f"Linked {issue_key} to epic {parent_key} via '{epic_link_field}'")
                return True
            except Exception as exc2:
                logger.debug(f"Could not link {issue_key} -> {parent_key} via '{epic_link_field}' ({exc2}); trying Issue Links…")

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
