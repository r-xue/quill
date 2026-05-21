import os
import pytest
from quill.config import Settings, GitHubRepoConfig


def test_defaults():
    """Verify package defaults work with no config files or env vars."""
    # Clear any QUILL_ env vars that might interfere
    for key in list(os.environ):
        if key.startswith("QUILL_"):
            del os.environ[key]

    cfg = Settings(
        _env_file=None,  # disable .env loading for test isolation
    )
    assert cfg.dry_run is False
    assert cfg.batch_size == 50
    assert cfg.jira_github_link_field == ""
    assert cfg.github_token is None
    assert cfg.jira_default_issue_type == "Task"
    assert cfg.repos == []


def test_env_var_override():
    """Verify QUILL_* env vars take highest priority."""
    os.environ["QUILL_JIRA_SERVER"] = "https://jira.test.com"
    os.environ["QUILL_DRY_RUN"] = "true"
    try:
        cfg = Settings(_env_file=None)
        assert cfg.jira_server == "https://jira.test.com"
        assert cfg.dry_run is True
    finally:
        del os.environ["QUILL_JIRA_SERVER"]
        del os.environ["QUILL_DRY_RUN"]


def test_programmatic_override():
    """Verify explicit kwargs (init_settings) win over everything."""
    os.environ["QUILL_BATCH_SIZE"] = "999"
    try:
        cfg = Settings(batch_size=42, _env_file=None)
        assert cfg.batch_size == 42  # init wins over env var
    finally:
        del os.environ["QUILL_BATCH_SIZE"]


def test_github_repo_config():
    rc = GitHubRepoConfig(
        owner="your-org", repo="your-repo", jira_project="CAS"
    )
    assert rc.full_name == "your-org/your-repo"
    assert rc.sync_labels is True
    assert rc.sync_comments is True
    assert rc.issue_filter.state == "all"
