import pytest
from unittest.mock import MagicMock, patch
from quill.sync import SyncEngine
from quill.config import Settings


@pytest.fixture
def v2_config():
    """Minimal Settings for stateless v2 tests."""
    return Settings(
        github_token="fake-gh-token",
        jira_server="https://jira.example.com",
        jira_token="fake-jira-pat",
        jira_github_link_field="customfield_10200",
        repos=[
            {
                "owner": "your-org",
                "repo": "your-repo",
                "jira_project": "CAS",
                "sync_labels": True,
                "sync_comments": False,
            }
        ],
        dry_run=True,
        _env_file=None,
    )


class TestSyncEngineInit:
    @patch("quill.sync.JiraClient")
    @patch("quill.sync.GitHubClient")
    def test_engine_creates_clients(self, mock_gh, mock_jira, v2_config):
        engine = SyncEngine(v2_config)
        mock_gh.assert_called_once_with(token="fake-gh-token")
        mock_jira.assert_called_once_with(
            server="https://jira.example.com",
            token="fake-jira-pat",
            verify_ssl=True,
        )


class TestSyncEngineStatus:
    @patch("quill.sync.JiraClient")
    @patch("quill.sync.GitHubClient")
    def test_get_status_returns_counts(self, mock_gh_cls, mock_jira_cls, v2_config):
        mock_jira_inst = MagicMock()
        mock_jira_inst.get_synced_issues.return_value = {
            "https://github.com/your-org/your-repo/issues/1": MagicMock(),
            "https://github.com/your-org/your-repo/issues/2": MagicMock(),
        }
        mock_jira_cls.return_value = mock_jira_inst

        engine = SyncEngine(v2_config)
        counts = engine.get_status()

        assert counts["your-org/your-repo"] == 2


class TestJiraClientEmptyField:
    @patch("quill.jira_client.JIRA")
    def test_get_synced_issues_empty_field(self, mock_jira_class):
        from quill.jira_client import JiraClient
        mock_jira_inst = MagicMock()
        mock_jira_class.return_value = mock_jira_inst
        
        # Mock the fallback label search returning a mock issue
        mock_issue = MagicMock()
        mock_issue.key = "PROJ-1"
        mock_issue.fields.summary = "Test summary"
        mock_issue.fields.description = "Some description"
        mock_jira_inst.search_issues.return_value = [mock_issue]

        client = JiraClient("https://jira.example.com", "fake-token")
        
        # Mock get_issue_property to return the GitHub URL
        with patch.object(client, "get_issue_property", return_value="https://github.com/your-org/your-repo/issues/1"):
            lookup = client.get_synced_issues("PROJ", "")
            
            # Since the custom field is empty, it should skip the custom field search
            # and only call the fallback search once.
            mock_jira_inst.search_issues.assert_called_once()
            called_jql = mock_jira_inst.search_issues.call_args[0][0]
            assert 'labels = "github-synced"' in called_jql
            assert lookup == {"https://github.com/your-org/your-repo/issues/1": mock_issue}

