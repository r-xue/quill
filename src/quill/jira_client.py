import warnings
import requests
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
                issues = self.jira.search_issues(jql, maxResults=False)
                lookup: dict[str, Any] = {}
                for issue in issues:
                    gh_url = getattr(issue.fields, github_link_field, None)
                    if gh_url:
                        lookup[gh_url] = issue
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
                fallback_jql, maxResults=False, fields="summary,description"
            )
        except Exception as exc:
            logger.warning(f"Fallback JQL also failed: {exc}")
            return {}

        lookup = {}
        for issue in issues:
            summary = getattr(issue.fields, "summary", "") or ""
            # Primary: read GitHub URL from entity property (quill >= v4)
            gh_url = self.get_issue_property(issue.key, "quill-github-url")
            # Legacy fallback: parse from description footer (quill < v4)
            if not gh_url:
                description = getattr(issue.fields, "description", "") or ""
                gh_url = _extract_github_url(description)
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
                jql, maxResults=200, fields="summary,description"
            )
            for issue in issues:
                s = getattr(issue.fields, "summary", "") or ""
                if s.startswith(prefix):
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

    def update_issue(
        self,
        issue_key: str,
        summary: str,
        description: str,
        labels: Optional[list[str]] = None,
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
    Extract the GitHub issue URL from the footer line quill writes into
    every Jira description::

        _Originally filed on GitHub:_ [owner/repo#N|https://github.com/...]

    Returns the URL string or None if not found.
    """
    import re
    # Match the Jira wiki link format: [text|url]
    match = re.search(
        r"Originally filed on GitHub.*?\[.*?\|(https://github\.com/[^\]]+)\]",
        description,
    )
    if match:
        return match.group(1)
    return None
