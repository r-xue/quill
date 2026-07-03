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
            assert mock_jira_inst.search_issues.call_args[1]["fields"] == "summary,description,status"
            assert lookup == {"https://github.com/your-org/your-repo/issues/1": mock_issue}


class TestClosedIssueSync:
    @patch("quill.sync.compute_content_hash", return_value="hash123")
    def test_sync_closed_issue_without_status_attribute(self, mock_hash, v2_config):
        """Ensure syncing a closed issue doesn't crash if jira_issue.fields has no status attribute."""
        engine = SyncEngine(v2_config)
        
        mock_gh_issue = MagicMock()
        mock_gh_issue.number = 1
        mock_gh_issue.html_url = "https://github.com/your-org/your-repo/issues/1"
        mock_gh_issue.labels = []
        mock_gh_issue.state = "closed"
        mock_gh_issue.title = "Closed Issue"
        mock_gh_issue.body = "Body"

        # Create a mock Jira issue whose fields object explicitly lacks a 'status' attribute
        # simulating a PropertyHolder without status.
        class FakeFields:
            summary = "[your-repo#1] Closed Issue"
            description = "Body"

        mock_jira_issue = MagicMock()
        mock_jira_issue.key = "CAS-1"
        mock_jira_issue.fields = FakeFields()

        engine.jira_client.get_issue_property.return_value = "hash123"
        stats = {"created": 0, "updated": 0, "skipped": 0, "comments": 0, "errors": 0}

        # Should not raise AttributeError: 'FakeFields' object has no attribute 'status'
        engine._sync_issue(
            rc=v2_config.repos[0],
            issue=mock_gh_issue,
            lookup={mock_gh_issue.html_url: mock_jira_issue},
            dry_run=True,
            force=False,
            stats=stats,
        )
        assert stats["errors"] == 0


class TestProjectFieldsSync:
    def test_sync_project_fields_to_labels_and_panel(self, v2_config):
        """Verify GitHub Projects V2 fields are converted to structured Jira labels and added to panel."""
        v2_config.repos[0].sync_project_fields = True
        engine = SyncEngine(v2_config)

        mock_gh_issue = MagicMock()
        mock_gh_issue.number = 42
        mock_gh_issue.html_url = "https://github.com/your-org/your-repo/issues/42"
        mock_gh_issue.labels = []
        mock_gh_issue.state = "open"
        mock_gh_issue.title = "Project Issue"
        mock_gh_issue.body = "Some description"

        with patch.object(
            engine.gh_client,
            "get_issue_project_fields",
            return_value={"Priority": "High", "Team": "Core Infra"},
        ):
            with patch.object(engine, "_preview_issue") as mock_preview:
                stats = {"created": 0, "updated": 0, "skipped": 0, "comments": 0, "errors": 0}
                engine._sync_issue(
                    rc=v2_config.repos[0],
                    issue=mock_gh_issue,
                    lookup={},
                    dry_run=True,
                    force=False,
                    stats=stats,
                )
                assert stats["created"] == 1
                kwargs = mock_preview.call_args[1]
                assert "proj-priority-high" in kwargs["labels"]
                assert "proj-team-core-infra" in kwargs["labels"]
                assert "*Project:* *Priority:* High \\| *Team:* Core Infra" in kwargs["description"]


