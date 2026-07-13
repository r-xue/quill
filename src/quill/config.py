"""Configuration management for quill.

Settings are resolved from four layers, highest priority first:

1. **Environment variables** (``QUILL_*``)
   Ephemeral overrides, great for CI or one-off runs.
   Example: ``QUILL_JIRA_SERVER=https://jira.example.com quill sync``

2. **Working-directory config** (per-project)
   - ``./quill.toml``  — project-level settings (safe to commit)
   - ``./.env``          — project-level secrets  (git-ignored)

3. **User config** (``~/.quill/config.toml``)
   Personal defaults shared across all projects.

4. **Package defaults** (hardcoded in this file)
   Sensible fallbacks so quill works out of the box.

This follows the same convention as git, pip, npm, and ragdoll:
most-specific scope wins.
"""

from pathlib import Path
from typing import Optional, Tuple, Type

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# ── Config file locations ──────────────────────────────────────────────
_USER_CONFIG = Path.home() / ".quill" / "config.toml"  # layer 3
_PROJECT_CONFIG = "quill.toml"                          # layer 2 (CWD-relative)


# ── Nested models (not BaseSettings — these are embedded structures) ───


class GitHubIssueFilter(BaseModel):
    state: str = "all"
    labels: list[str] = Field(default_factory=list)
    since: Optional[str] = None  # ISO date string for initial backfill cutoff

    @field_validator("state")
    @classmethod
    def validate_state(cls, v: str) -> str:
        if v not in ("open", "closed", "all"):
            raise ValueError("state must be 'open', 'closed', or 'all'")
        return v


class GitHubRepoConfig(BaseModel):
    owner: str
    repo: str
    jira_project: str
    sync_labels: bool = True
    sync_comments: bool = True
    sync_project_fields: bool = False
    sync_milestones: bool = True
    sync_parent_links: bool = True
    issue_filter: GitHubIssueFilter = Field(default_factory=GitHubIssueFilter)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


# ── Main Settings class ───────────────────────────────────────────────


class Settings(BaseSettings):
    """Quill configuration.

    All fields map to environment variables prefixed with ``QUILL_``.
    For example ``QUILL_JIRA_SERVER`` → ``jira_server``.

    Precedence: env vars > ./quill.toml > ./.env > ~/.quill/config.toml > defaults.
    """

    model_config = SettingsConfigDict(
        env_prefix="QUILL_",
        env_file=".env",
        env_file_encoding="utf-8",
        toml_file=[_PROJECT_CONFIG, str(_USER_CONFIG)],
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        """Define the 4-layer config precedence.

        pydantic-settings resolves left-to-right: the first source to
        provide a value wins.
        """
        project_toml = TomlConfigSettingsSource(
            settings_cls,
            toml_file=Path(_PROJECT_CONFIG),
        )
        user_toml = TomlConfigSettingsSource(
            settings_cls,
            toml_file=_USER_CONFIG,
        )

        return (
            init_settings,      # 0. explicit kwargs  (programmatic)
            env_settings,       # 1. QUILL_* env vars
            project_toml,       # 2a. ./quill.toml  (project, non-secret)
            dotenv_settings,    # 2b. ./.env           (project, secrets)
            user_toml,          # 3. ~/.quill/config.toml  (user defaults)
            # 4. package defaults are the field defaults below
        )

    # ── GitHub ─────────────────────────────────────────────────────────
    github_token: Optional[str] = None

    # Repos list — populated from TOML [repos] array.
    # Cannot be set via a single env var (it's a nested structure).
    repos: list[GitHubRepoConfig] = Field(default_factory=list)

    # ── Jira ───────────────────────────────────────────────────────────
    jira_server: str = "https://jira.example.com"
    jira_token: str = ""
    jira_verify_ssl: bool = True
    jira_default_issue_type: str = "Task"
    jira_github_link_field: str = ""
    jira_epic_link_field: str = "customfield_10014"

    # ── Sync ───────────────────────────────────────────────────────────
    dry_run: bool = False
    batch_size: int = 50


# Module-level singleton — importable as ``from quill.config import settings``
settings = Settings()
