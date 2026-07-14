import json
import types
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional
from github import Github
from quill.log import logger


class GHIssueProxy:
    """Lightweight, zero-HTTP issue representation populated directly via GraphQL bulk query."""
    def __init__(self, node: dict[str, Any]):
        self.number = int(node["number"])
        self.title = node.get("title", "") or ""
        self.body = node.get("body", "") or ""
        self.state = node.get("state", "").lower()
        self.html_url = node.get("url", "")
        
        author_data = node.get("author") or {}
        self.user = types.SimpleNamespace(
            login=author_data.get("login", "unknown"),
            html_url=author_data.get("url", ""),
        )
        
        comments_data = node.get("comments") or {}
        self.comments = int(comments_data.get("totalCount", 0))
        
        labels_data = node.get("labels") or {}
        label_nodes = labels_data.get("nodes") or []
        self.labels = [types.SimpleNamespace(name=lbl.get("name", "")) for lbl in label_nodes if lbl.get("name")]
        
        created_str = node.get("createdAt", "")
        if created_str:
            try:
                self.created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except Exception:
                self.created_at = datetime.now(timezone.utc)
        else:
            self.created_at = datetime.now(timezone.utc)
            
        updated_str = node.get("updatedAt", "")
        if updated_str:
            try:
                self.updated_at = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            except Exception:
                self.updated_at = self.created_at
        else:
            self.updated_at = self.created_at

    @property
    def pull_request(self):
        return None


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
        if since:
            logger.info(f"Incremental sync: fetching issues updated since {since}")

        if self.token:
            try:
                query = """
                query($owner: String!, $name: String!, $cursor: String, $states: [IssueState!], $since: DateTime) {
                  repository(owner: $owner, name: $name) {
                    issues(first: 100, after: $cursor, filterBy: {states: $states, since: $since}) {
                      pageInfo {
                        hasNextPage
                        endCursor
                      }
                      nodes {
                        number
                        title
                        body
                        state
                        url
                        createdAt
                        updatedAt
                        author { login url }
                        comments { totalCount }
                        labels(first: 50) {
                          nodes { name }
                        }
                      }
                    }
                  }
                }
                """
                states_arg = []
                if state == "open":
                    states_arg = ["OPEN"]
                elif state == "closed":
                    states_arg = ["CLOSED"]
                elif state == "all":
                    states_arg = ["OPEN", "CLOSED"]

                since_arg = None
                if since:
                    cleaned_since = since.replace("Z", "+00:00")
                    dt_with_tz = datetime.fromisoformat(cleaned_since)
                    if dt_with_tz.tzinfo is None:
                        dt_with_tz = dt_with_tz.replace(tzinfo=timezone.utc)
                    since_arg = dt_with_tz.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                cursor = None
                has_next_page = True
                filtered_issues = []

                while has_next_page:
                    variables: dict[str, Any] = {
                        "owner": owner,
                        "name": repo_name,
                        "cursor": cursor,
                    }
                    if states_arg:
                        variables["states"] = states_arg
                    if since_arg:
                        variables["since"] = since_arg

                    data = self._execute_graphql(query, variables)
                    if not data or "repository" not in data or not data["repository"]:
                        break
                    issues_data = data["repository"].get("issues", {})
                    nodes = issues_data.get("nodes", [])
                    for node in nodes:
                        if not node or "number" not in node:
                            continue
                        proxy = GHIssueProxy(node)
                        if labels:
                            issue_labels = [label.name for label in proxy.labels]
                            if not any(label in issue_labels for label in labels):
                                continue
                        filtered_issues.append(proxy)

                    page_info = issues_data.get("pageInfo", {})
                    has_next_page = page_info.get("hasNextPage", False)
                    cursor = page_info.get("endCursor")

                logger.info(f"Found {len(filtered_issues)} issues to process in {full_name}")
                return filtered_issues
            except Exception as exc:
                logger.debug(f"GraphQL issue fetch failed ({exc}); falling back to PyGithub REST API…")

        repo = self.gh.get_repo(full_name)

        kwargs: dict[str, Any] = {"state": state}
        if since:
            cleaned_since = since.replace("Z", "+00:00")
            dt_with_tz = datetime.fromisoformat(cleaned_since)
            if dt_with_tz.tzinfo is None:
                dt_with_tz = dt_with_tz.replace(tzinfo=timezone.utc)
            dt_utc = dt_with_tz.astimezone(timezone.utc).replace(tzinfo=None)
            kwargs["since"] = dt_utc

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

    def get_repo_project_fields(self, owner: str, repo_name: str) -> dict[int, dict[str, str]]:
        """Fetch custom fields from GitHub Projects V2 for all issues in a repo via batch GraphQL.

        Returns:
            A dictionary mapping issue number to field dictionary (e.g., {48: {'Priority': 'High'}}).
        """
        if not self.token:
            return {}

        query = """
        query($owner: String!, $name: String!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            issues(first: 100, after: $cursor) {
              pageInfo {
                hasNextPage
                endCursor
              }
              nodes {
                number
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
        }
        """

        results: dict[int, dict[str, str]] = {}
        cursor = None
        has_next_page = True

        try:
            while has_next_page:
                variables = {"owner": owner, "name": repo_name, "cursor": cursor}
                req = urllib.request.Request(
                    "https://api.github.com/graphql",
                    data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "User-Agent": "quill",
                    },
                )
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                issues_data = data.get("data", {}).get("repository", {}).get("issues", {})
                nodes = issues_data.get("nodes", []) or []
                for issue_node in nodes:
                    if not issue_node:
                        continue
                    issue_num = issue_node.get("number")
                    if not issue_num:
                        continue

                    fields_map: dict[str, str] = {}
                    items = issue_node.get("projectItems", {}).get("nodes", []) or []
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
                                fields_map[field_name] = str(val)

                    if fields_map:
                        results[issue_num] = fields_map

                page_info = issues_data.get("pageInfo", {})
                has_next_page = page_info.get("hasNextPage", False)
                cursor = page_info.get("endCursor")

            return results
        except Exception as exc:
            logger.warning(f"Failed to batch fetch repo project fields via GraphQL: {exc}")
            return {}

    def get_repo_issue_parents(self, owner: str, repo_name: str) -> dict[int, str]:
        """Fetch native GitHub sub-issue parent URLs via batch GraphQL.

        Returns:
            A dictionary mapping issue number to the parent issue's GitHub HTML URL.
        """
        if not self.token:
            return {}

        query = """
        query($owner: String!, $name: String!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            issues(first: 100, after: $cursor) {
              pageInfo {
                hasNextPage
                endCursor
              }
              nodes {
                number
                parent {
                  ... on Issue {
                    number
                    repository {
                      name
                      owner { login }
                    }
                  }
                }
              }
            }
          }
        }
        """
        results: dict[int, str] = {}
        cursor = None
        has_next_page = True

        try:
            while has_next_page:
                variables = {"owner": owner, "name": repo_name, "cursor": cursor}
                req = urllib.request.Request(
                    "https://api.github.com/graphql",
                    data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "User-Agent": "quill",
                    },
                )
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                issues_data = data.get("data", {}).get("repository", {}).get("issues", {})
                for node in issues_data.get("nodes", []) or []:
                    if not node:
                        continue
                    num = node.get("number")
                    parent_node = node.get("parent")
                    if num and isinstance(parent_node, dict):
                        p_num = parent_node.get("number")
                        p_repo = parent_node.get("repository", {}).get("name")
                        p_owner = parent_node.get("repository", {}).get("owner", {}).get("login")
                        if p_num and p_repo and p_owner:
                            results[num] = f"https://github.com/{p_owner}/{p_repo}/issues/{p_num}"

                page_info = issues_data.get("pageInfo", {})
                has_next_page = page_info.get("hasNextPage", False)
                cursor = page_info.get("endCursor")
        except Exception as exc:
            logger.warning(f"Batch GraphQL parent lookup failed for {owner}/{repo_name}: {exc}")
        return results

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

    def get_issue_parent_url(self, issue: Any) -> Optional[str]:
        """Fetch the parent issue URL for a single issue via GraphQL."""
        if not self.token or not getattr(issue, "html_url", None):
            return None
        parts = issue.html_url.split("/")
        if len(parts) >= 7 and parts[-2] == "issues":
            owner, repo_name, num_str = parts[-4], parts[-3], parts[-1]
            try:
                num = int(num_str)
                parents = self.get_repo_issue_parents(owner, repo_name)
                return parents.get(num)
            except Exception:
                pass
        return None
