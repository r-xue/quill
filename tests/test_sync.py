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
            assert mock_jira_inst.search_issues.call_args[1]["fields"] == "summary,description,status,labels,issuetype,parent"
            assert mock_jira_inst.search_issues.call_args[1]["properties"] == "quill-content-hash,quill-github-url,quill-synced-labels,quill-synced-comments-count"
            assert lookup == {"https://github.com/your-org/your-repo/issues/1": mock_issue}


@patch("quill.sync.JiraClient")
@patch("quill.sync.GitHubClient")
class TestClosedIssueSync:
    @patch("quill.sync.compute_content_hash", return_value="hash123")
    def test_sync_closed_issue_without_status_attribute(self, mock_hash, mock_gh_cls, mock_jira_cls, v2_config):
        """Ensure syncing a closed issue doesn't crash if jira_issue.fields has no status attribute."""
        engine = SyncEngine(v2_config)
        
        mock_gh_issue = MagicMock()
        mock_gh_issue.number = 1
        mock_gh_issue.html_url = "https://github.com/your-org/your-repo/issues/1"
        mock_gh_issue.labels = []
        mock_gh_issue.milestone = None
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


@patch("quill.sync.JiraClient")
@patch("quill.sync.GitHubClient")
class TestProjectFieldsSync:
    def test_sync_project_fields_to_labels_and_panel(self, mock_gh_cls, mock_jira_cls, v2_config):
        """Verify GitHub Projects V2 fields are converted to structured Jira labels and added to panel."""
        v2_config.repos[0].sync_project_fields = True
        engine = SyncEngine(v2_config)

        mock_gh_issue = MagicMock()
        mock_gh_issue.number = 42
        mock_gh_issue.html_url = "https://github.com/your-org/your-repo/issues/42"
        mock_gh_issue.labels = []
        mock_gh_issue.milestone = None
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


@patch("quill.sync.JiraClient")
@patch("quill.sync.GitHubClient")
class TestMergeJiraLabels:
    def test_merge_labels_with_entity_property(self, mock_gh_cls, mock_jira_cls, v2_config):
        engine = SyncEngine(v2_config)
        mock_jira_issue = MagicMock()
        mock_jira_issue.fields.labels = ["github-synced", "proj-status-in-progress", "bug", "need-qa", "jira-custom"]
        mock_jira_issue._quill_cached_synced_labels = ["github-synced", "proj-status-in-progress", "bug"]

        all_labels = ["github-synced", "proj-status-done"]
        merged = engine._merge_jira_labels(mock_jira_issue, "CAS-1", all_labels)
        assert merged == ["github-synced", "jira-custom", "need-qa", "proj-status-done"]

    def test_merge_labels_fallback_without_entity_property(self, mock_gh_cls, mock_jira_cls, v2_config):
        engine = SyncEngine(v2_config)
        mock_jira_issue = MagicMock()
        mock_jira_issue.fields.labels = ["github-synced", "proj-status-in-progress", "bug", "need-qa"]
        mock_jira_issue._quill_cached_synced_labels = None
        engine.jira_client.get_issue_property_from_issue.return_value = None
        engine.jira_client.get_issue_property.return_value = None

        all_labels = ["github-synced", "proj-status-done", "bug"]
        merged = engine._merge_jira_labels(mock_jira_issue, "CAS-1", all_labels)
        assert merged == ["bug", "github-synced", "need-qa", "proj-status-done"]


@patch("quill.sync.JiraClient")
@patch("quill.sync.GitHubClient")
class TestSyncParentRelationships:
    def test_resolve_parent_jira_key_direct_key(self, mock_gh_cls, mock_jira_cls, v2_config):
        engine = SyncEngine(v2_config)
        resolved = engine._resolve_parent_jira_key("CAS-100", {}, "CAS")
        assert resolved == "CAS-100"

    def test_resolve_parent_jira_key_via_lookup(self, mock_gh_cls, mock_jira_cls, v2_config):
        engine = SyncEngine(v2_config)
        mock_parent_issue = MagicMock()
        mock_parent_issue.key = "CAS-12"
        lookup = {"https://github.com/your-org/your-repo/issues/12": mock_parent_issue}

        resolved = engine._resolve_parent_jira_key("#12", lookup, "CAS")
        assert resolved == "CAS-12"

    def test_sync_parent_relationships(self, mock_gh_cls, mock_jira_cls, v2_config):
        engine = SyncEngine(v2_config)
        mock_parent_issue = MagicMock()
        mock_parent_issue.key = "CAS-12"
        lookup = {"https://github.com/your-org/your-repo/issues/12": mock_parent_issue}
        pending = {"https://github.com/your-org/your-repo/issues/42": ("CAS-42", "#12", False)}

        with patch.object(engine.jira_client, "set_parent_issue") as mock_set_parent:
            engine._sync_parent_relationships(v2_config.repos[0], pending, lookup, dry_run=False)
            mock_set_parent.assert_called_once_with(
                issue_key="CAS-42",
                parent_key="CAS-12",
                epic_link_field="customfield_10014",
                promote_to_epic=False,
            )


class TestHierarchyClassification:
    def test_epic_container_hierarchy_classification(self):
        """Test that parent container issue #39 (even if linked to parent) is classified as Level 1 Epic, and all children (#120-#123) are Level 2 Tasks without splitting."""
        import types
        issues = [
            types.SimpleNamespace(number=39, labels=[]),
            types.SimpleNamespace(number=120, labels=[]),
            types.SimpleNamespace(number=121, labels=[]),
            types.SimpleNamespace(number=122, labels=[]),
            types.SimpleNamespace(number=123, labels=[]),
            types.SimpleNamespace(number=200, labels=[]),
        ]
        repo_parents = {
            39: "10",  # #39 is linked to some higher goal #10
            120: "39", # #120 has child #200
            121: "39",
            122: "39",
            123: "39",
            200: "120",
        }

        level1_epics, level2_tasks, level3_subtasks = SyncEngine._classify_hierarchy_levels(
            gh_issues=issues,
            repo_parents=repo_parents,
        )
        assert 39 in level1_epics
        assert {120, 121, 122, 123}.issubset(level2_tasks)
        assert 200 in level3_subtasks


class TestEpicNamePrePopulation:
    @patch("quill.jira_client.JIRA")
    def test_create_issue_includes_epic_name(self, mock_jira_class):
        from quill.jira_client import JiraClient
        mock_jira = MagicMock()
        mock_jira.fields.return_value = [{"name": "Epic Name", "id": "customfield_10011"}]
        mock_jira_class.return_value = mock_jira
        client = JiraClient("http://jira.test", "user", "pass")
        client.jira = mock_jira

        client.create_issue("PROJ", "Epic Summary", "Description", "Epic")
        mock_jira.create_issue.assert_called_once()
        called_fields = mock_jira.create_issue.call_args[1]["fields"]
        assert called_fields["customfield_10011"] == "Epic Summary"

    @patch("quill.jira_client.JIRA")
    def test_update_issue_type_to_epic_includes_epic_name(self, mock_jira_class):
        import types
        from quill.jira_client import JiraClient
        mock_jira = MagicMock()
        mock_jira.fields.return_value = [{"name": "Epic Name", "id": "customfield_10011"}]
        mock_issue = MagicMock()
        mock_issue.fields = types.SimpleNamespace(issuetype=types.SimpleNamespace(name="Task"), summary="Existing Issue")
        mock_jira.issue.return_value = mock_issue
        mock_jira.issue_types.return_value = [types.SimpleNamespace(name="Epic", id="10000")]
        mock_jira_class.return_value = mock_jira

        client = JiraClient("http://jira.test", "user", "pass")
        client.jira = mock_jira

        client.update_issue_type("PROJ-1", "Epic")
        mock_issue.update.assert_called_once()
        called_fields = mock_issue.update.call_args[1]["fields"]
        assert called_fields["issuetype"] == {"id": "10000", "name": "Epic"}
        assert called_fields["customfield_10011"] == "Existing Issue"

    @patch("quill.jira_client.JIRA")
    def test_set_parent_issue_promote_to_epic_includes_epic_name(self, mock_jira_class):
        import types
        from quill.jira_client import JiraClient
        mock_jira = MagicMock()
        mock_jira.fields.return_value = [{"name": "Epic Name", "id": "customfield_10011"}]
        mock_parent = MagicMock()
        mock_parent.fields = types.SimpleNamespace(issuetype=types.SimpleNamespace(name="Task"), summary="Parent Task")
        mock_child = MagicMock()
        mock_child.fields = types.SimpleNamespace(issuetype=types.SimpleNamespace(name="Task"), parent=None)
        mock_jira.issue.side_effect = lambda k: mock_parent if k == "PARENT-1" else mock_child
        mock_jira_class.return_value = mock_jira

        client = JiraClient("http://jira.test", "user", "pass")
        client.jira = mock_jira

        client.set_parent_issue("CHILD-1", "PARENT-1", promote_to_epic=True)
        mock_parent.update.assert_called_once()
        called_fields = mock_parent.update.call_args[1]["fields"]
        assert called_fields["issuetype"] == {"name": "Epic"}
        assert called_fields["customfield_10011"] == "Parent Task"

    @patch("quill.jira_client.JIRA")
    def test_update_issue_type_screen_retry_and_epic_name(self, mock_jira_class):
        import types
        from quill.jira_client import JiraClient
        mock_jira = MagicMock()
        mock_jira.fields.return_value = [{"name": "Epic Name", "id": "customfield_10004"}]
        mock_issue = MagicMock()
        mock_issue.fields = types.SimpleNamespace(issuetype=types.SimpleNamespace(name="Task"), summary="Existing Task")
        mock_jira.issue.return_value = mock_issue
        mock_jira.issue_types.return_value = [types.SimpleNamespace(name="Epic", id="10000")]
        mock_jira_class.return_value = mock_jira

        # First call with customfield_10004 fails due to screen restriction; second call without customfield_10004 succeeds
        def update_side_effect(fields=None):
            if fields and "customfield_10004" in fields and "issuetype" in fields:
                raise Exception("Field 'customfield_10004' cannot be set. It is not on the appropriate screen, or unknown.")
        mock_issue.update.side_effect = update_side_effect

        client = JiraClient("http://jira.test", "user", "pass")
        client.jira = mock_jira

        res = client.update_issue_type("PROJ-2", "Epic")
        assert res is True
        assert mock_issue.update.call_count == 3
        # Call 1: combined fields failed
        # Call 2: issuetype only succeeded
        # Call 3: set_epic_name succeeded

    @patch("quill.jira_client.JIRA")
    def test_update_issue_type_top_level_recreate_fallback(self, mock_jira_class):
        import types
        from quill.jira_client import JiraClient
        mock_jira = MagicMock()
        mock_jira.fields.return_value = [{"name": "Epic Name", "id": "customfield_10004"}]
        mock_issue = MagicMock()
        mock_issue.fields = types.SimpleNamespace(
            issuetype=types.SimpleNamespace(name="Task"),
            summary="Top level task",
            description="",
            project=types.SimpleNamespace(key="GITHUB"),
            labels=[],
            duedate=None,
        )
        mock_jira.issue.return_value = mock_issue
        mock_jira.issue_types.return_value = [types.SimpleNamespace(name="Epic", id="10000")]
        mock_issue.update.side_effect = Exception("Cannot change issue type in-place")
        mock_jira_class.return_value = mock_jira

        client = JiraClient("http://jira.test", "user", "pass")
        client.jira = mock_jira
        with patch.object(client, "recreate_as_standard_issue", return_value="GITHUB-999") as mock_recreate:
            res = client.update_issue_type("GITHUB-355", "Epic")
            assert res == "GITHUB-999"
            mock_recreate.assert_called_once_with(
                old_issue_key="GITHUB-355",
                new_type="Epic",
                parent_key=None,
                github_link_field=None,
            )


class TestSyncHierarchyGuard:
    def test_classify_hierarchy_levels_title_epic(self):
        import types
        from quill.sync import SyncEngine

        issues = [
            types.SimpleNamespace(number=1, title="[Epic] Viper roadmap", labels=[]),
            types.SimpleNamespace(number=2, title="Epic: XRadio integration", labels=[]),
            types.SimpleNamespace(number=3, title="Regular task", labels=[]),
        ]
        level1, level2, level3 = SyncEngine._classify_hierarchy_levels(issues, repo_parents=None)
        assert 1 in level1
        assert 2 in level1
        assert 3 in level2

    @patch("quill.jira_client.JIRA")
    def test_sync_issue_skips_demote_epic_to_task(self, mock_jira_class):
        import types
        from quill.jira_client import JiraClient
        from quill.sync import SyncEngine

        mock_jira = MagicMock()
        mock_jira_issue = MagicMock()
        mock_jira_issue.key = "GITHUB-355"
        mock_jira_issue.fields = types.SimpleNamespace(issuetype=types.SimpleNamespace(name="Epic"), summary="Viper refactor")
        mock_jira_issue._quill_cached_hash = "cached-hash"
        mock_jira.issue.return_value = mock_jira_issue
        mock_jira_class.return_value = mock_jira

        client = JiraClient("http://jira.test", "user", "pass")
        client.jira = mock_jira

        cfg = MagicMock()
        cfg.github_token = "fake-token"
        cfg.jira_default_issue_type = "Task"
        engine = SyncEngine(cfg)
        engine.jira_client = client

        rc = MagicMock()
        rc.project_key = "GITHUB"
        rc.issue_type = "Task"
        rc.epic_link_field = None

        mock_created = MagicMock()
        mock_created.strftime.return_value = "2026-07-14"
        gh_issue = types.SimpleNamespace(number=355, title="Viper refactor", body="", state="open", labels=[], html_url="https://github.com/test/repo/issues/355", comments=0, created_at=mock_created, user=types.SimpleNamespace(login="testuser"))
        with patch.object(client, "update_issue_type") as mock_update_type:
            engine._sync_issue(rc, gh_issue, lookup={"https://github.com/test/repo/issues/355": mock_jira_issue}, dry_run=False, force=False, stats={"created": 0, "updated": 0, "skipped": 0, "errors": 0}, level2_tasks={355})
            # Because GITHUB-355 is currently an Epic and target is Task with no parent, demotion MUST be skipped!
            mock_update_type.assert_not_called()
