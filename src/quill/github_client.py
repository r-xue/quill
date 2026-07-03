import json
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional
from github import Github
from quill.log import logger


class GitHubClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token
        if token:
            self.gh = Github(token)
        else:
            self.gh = Github()

    def get_issues(
        self,
        owner: str,
        repo_name: str,
        since: Optional[str] = None,
        state: str = "all",
        labels: Optional[list[str]] = None,
    ) -> list[Any]:
        """Fetch issues from the specified repository.

        Filters out Pull Requests from the results.

        Args:
            owner: The GitHub repository owner/organization.
            repo_name: The GitHub repository name.
            since: ISO date string to fetch issues updated after this date.
            state: Issue state filter.
            labels: List of labels to filter the issues by.

        Returns:
            A list of GitHub issue objects.
        """
        full_name = f"{owner}/{repo_name}"
        logger.info(f"Fetching issues from GitHub repository: {full_name}")
        repo = self.gh.get_repo(full_name)

        kwargs: dict[str, Any] = {"state": state}
        if since:
            cleaned_since = since.replace("Z", "+00:00")
            dt_with_tz = datetime.fromisoformat(cleaned_since)
            if dt_with_tz.tzinfo is None:
                dt_with_tz = dt_with_tz.replace(tzinfo=timezone.utc)
            dt_utc = dt_with_tz.astimezone(timezone.utc).replace(tzinfo=None)
            kwargs["since"] = dt_utc
            logger.info(f"Incremental sync: fetching issues updated since {since}")

        issues = repo.get_issues(**kwargs)
        filtered_issues = []

        for issue in issues:
            # PyGithub returns both issues and PRs — filter out PRs
            if issue.pull_request is not None:
                continue

            # Local filter by labels if specified
            if labels:
                issue_labels = [label.name for label in issue.labels]
                if not any(label in issue_labels for label in labels):
                    continue

            filtered_issues.append(issue)

        logger.info(f"Found {len(filtered_issues)} issues to process in {full_name}")
        return filtered_issues

    def get_comments(
        self,
        owner: str,
        repo_name: str,
        issue_number: int,
        since: Optional[str] = None,
    ) -> list[Any]:
        """Fetch comments for a specific issue.

        Args:
            owner: The GitHub repository owner/organization.
            repo_name: The GitHub repository name.
            issue_number: The GitHub issue ID number.
            since: ISO date string to fetch comments created after this date.

        Returns:
            A list of GitHub comment objects.
        """
        full_name = f"{owner}/{repo_name}"
        repo = self.gh.get_repo(full_name)
        issue = repo.get_issue(number=issue_number)

        kwargs = {}
        if since:
            cleaned_since = since.replace("Z", "+00:00")
            dt_with_tz = datetime.fromisoformat(cleaned_since)
            dt_utc = dt_with_tz.astimezone(timezone.utc).replace(tzinfo=None)
            kwargs["since"] = dt_utc

        comments = issue.get_comments(**kwargs)
        return list(comments)

    def get_issue_project_fields(self, issue: Any) -> dict[str, str]:
        """Fetch custom fields from GitHub Projects V2 for the given issue via GraphQL.

        Returns:
            A mapping from field name to string value (e.g., {'Priority': 'High'}).
        """
        if not self.token:
            return {}
        node_id = getattr(issue, "node_id", None)
        if not node_id:
            return {}

        query = """
        query($nodeId: ID!) {
          node(id: $nodeId) {
            ... on Issue {
              projectItems(first: 10) {
                nodes {
                  fieldValues(first: 20) {
                    nodes {
                      ... on ProjectV2ItemFieldTextValue {
                        text
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldNumberValue {
                        number
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldDateValue {
                        date
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldIterationValue {
                        title
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """

        try:
            req = urllib.request.Request(
                "https://api.github.com/graphql",
                data=json.dumps({"query": query, "variables": {"nodeId": node_id}}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "User-Agent": "quill",
                },
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            node = data.get("data", {}).get("node", {})
            if not node:
                return {}

            project_fields: dict[str, str] = {}
            items = node.get("projectItems", {}).get("nodes", []) or []
            for item in items:
                if not item:
                    continue
                field_vals = item.get("fieldValues", {}).get("nodes", []) or []
                for fv in field_vals:
                    if not fv:
                        continue
                    field_meta = fv.get("field") or {}
                    field_name = field_meta.get("name")
                    if not field_name or field_name.lower() in ("title",):
                        continue

                    val = (
                        fv.get("text")
                        or fv.get("name")
                        or fv.get("date")
                        or fv.get("title")
                    )
                    if val is None and "number" in fv and fv["number"] is not None:
                        val = str(fv["number"])

                    if val:
                        project_fields[field_name] = str(val)

            return project_fields
        except Exception as exc:
            logger.debug(f"Failed to fetch GraphQL project fields for #{getattr(issue, 'number', '?')}: {exc}")
            return {}
