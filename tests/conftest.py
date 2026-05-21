import os
import pytest


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    """Isolate tests from user-level and project-level configs and env variables."""
    # Point configuration file paths to temp directory files so they don't load real settings
    monkeypatch.setattr("quill.config._USER_CONFIG", tmp_path / "user_config.toml")
    monkeypatch.setattr("quill.config._PROJECT_CONFIG", str(tmp_path / "project_config.toml"))
    
    # Temporarily remove any QUILL_ prefixed environment variables
    for key in list(os.environ):
        if key.startswith("QUILL_"):
            monkeypatch.delenv(key, raising=False)



@pytest.fixture
def mock_issue():
    """
    Mock GitHub issue object matching the PyGithub interface.
    """

    class MockLabel:
        def __init__(self, name):
            self.name = name

    class MockUser:
        def __init__(self, login):
            self.login = login

    class MockIssue:
        def __init__(self):
            self.number = 42
            self.title = "Test Bug Report"
            self.body = "This is a **markdown** bug report."
            self.state = "open"
            self.labels = [MockLabel("bug"), MockLabel("high-priority")]
            self.html_url = "https://github.com/your-org/your-repo/issues/42"
            from datetime import datetime, timezone

            self.updated_at = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
            self.pull_request = None

        def get_comments(self, since=None):
            return []

    return MockIssue()
