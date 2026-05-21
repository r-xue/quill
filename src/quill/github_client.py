from datetime import datetime, timezone
from typing import Any, Optional
from github import Github
from quill.log import logger


class GitHubClient:
    def __init__(self, token: Optional[str] = None):
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
