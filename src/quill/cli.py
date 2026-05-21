import argparse
import os
import sys

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from quill import __version__
from quill.config import Settings
from quill.sync import SyncEngine

console = Console()


# ── Subcommands ───────────────────────────────────────────────────────────


def cmd_sync(args):
    """Synchronize GitHub issues and comments to Jira."""
    overrides = {}
    if args.dry_run:
        overrides["dry_run"] = True
    cfg = Settings(**overrides)
    engine = SyncEngine(cfg)
    engine.sync_all(repo_override=args.repo, force=getattr(args, "force", False))


def cmd_check(args):
    """Validate credentials and connectivity to GitHub and Jira APIs."""
    cfg = Settings()

    console.print("\n[bold blue]Checking GitHub connection…[/bold blue]")
    from github import Github

    if cfg.github_token:
        gh = Github(cfg.github_token)
        user = gh.get_user().login
        console.print(f"[green]✓ GitHub — authenticated as @{user}[/green]")
    else:
        gh = Github()
        try:
            rate = gh.get_rate_limit()
            # PyGithub >= 2.x: .core is on the Rate object
            core = getattr(rate, "core", None) or rate.rate
            console.print(
                f"[yellow]! GitHub — anonymous "
                f"(rate limit: {core.remaining}/{core.limit})[/yellow]"
            )
        except Exception:
            console.print(
                "[yellow]! GitHub — anonymous (token recommended for rate limits)[/yellow]"
            )

    console.print(f"\n[bold blue]Checking Jira connection ({cfg.jira_server})…[/bold blue]")
    from quill.jira_client import JiraClient

    jira_client = JiraClient(
        server=cfg.jira_server,
        token=cfg.jira_token,
        verify_ssl=cfg.jira_verify_ssl,
    )
    display_name = jira_client.test_connection()
    console.print(f"[green]✓ Jira — authenticated as {display_name}[/green]")

    console.print("\n[bold green]✓ All checks passed.[/bold green]")


def cmd_status(args):
    """Show sync status (queries Jira for synced issue counts)."""
    cfg = Settings()
    engine = SyncEngine(cfg)
    counts = engine.get_status()

    if not counts:
        console.print(
            Panel("No repositories configured.", title="quill status")
        )
        return

    table = Table(title="quill — Synced Issues per Repository")
    table.add_column("GitHub Repository", style="cyan")
    table.add_column("Jira Project", style="magenta")
    table.add_column("Synced Issues", style="green", justify="right")

    for rc in cfg.repos:
        count = counts.get(rc.full_name, 0)
        table.add_row(rc.full_name, rc.jira_project, str(count))

    console.print(table)


def cmd_init(args):
    """Generate template configuration files."""
    toml_path = "quill.toml"
    env_path = ".env"

    created = []

    if not os.path.exists(toml_path) or _confirm_overwrite(toml_path):
        _write_template_toml(toml_path)
        created.append(toml_path)

    if not os.path.exists(env_path) or _confirm_overwrite(env_path):
        _write_template_env(env_path)
        created.append(env_path)

    if created:
        console.print(
            f"[bold green]✓ Created: {', '.join(created)}[/bold green]\n"
            f"Edit these files, then run [cyan]quill check[/cyan] to verify connectivity."
        )
    else:
        console.print("[yellow]No files created.[/yellow]")


def _confirm_overwrite(path: str) -> bool:
    answer = input(f"File '{path}' already exists. Overwrite? [y/N] ")
    return answer.lower() in ("y", "yes")


def _write_template_toml(path: str):
    template = '''\
# quill.toml — project-level configuration (safe to commit)
#
# Precedence (highest wins):
#   1. QUILL_* env vars
#   2. ./quill.toml  (this file)
#   3. ./.env          (secrets)
#   4. ~/.quill/config.toml  (user defaults)
#   5. Package defaults

jira_server = "https://jira.example.com"
jira_verify_ssl = true
jira_default_issue_type = "Task"
jira_github_link_field = "customfield_10200"

dry_run = false
batch_size = 50

[[repos]]
owner = "your-org"
repo = "your-repo"
jira_project = "CAS"
sync_labels = true
sync_comments = true

[repos.issue_filter]
state = "open"
labels = []
since = "2024-01-01"
'''
    with open(path, "w") as f:
        f.write(template)


def _write_template_env(path: str):
    template = '''\
# .env — project-level secrets (git-ignored)
# QUILL_GITHUB_TOKEN=ghp_...
# QUILL_JIRA_TOKEN=...
'''
    with open(path, "w") as f:
        f.write(template)


# ── Argument parser ───────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quill",
        description=(
            "Sync GitHub issues to an on-premise Jira instance.\n\n"
            "Configuration is loaded from (highest priority first):\n"
            "  1. QUILL_* environment variables\n"
            "  2. ./quill.toml\n"
            "  3. ./.env\n"
            "  4. ~/.quill/config.toml\n"
            "  5. Package defaults"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ── sync ──
    p_sync = sub.add_parser("sync", help="Run synchronization")
    p_sync.add_argument(
        "-r", "--repo", help="Sync only this repo (format: owner/repo)"
    )
    p_sync.add_argument(
        "-d", "--dry-run", action="store_true", help="Preview without writing"
    )
    p_sync.add_argument(
        "-f", "--force", action="store_true",
        help="Ignore stored hashes and update all tickets (use after format changes)"
    )
    p_sync.set_defaults(func=cmd_sync)

    # ── check ──
    p_check = sub.add_parser("check", help="Test API connectivity")
    p_check.set_defaults(func=cmd_check)

    # ── status ──
    p_status = sub.add_parser(
        "status", help="Show synced issue counts (queries Jira)"
    )
    p_status.set_defaults(func=cmd_status)

    # ── init ──
    p_init = sub.add_parser(
        "init", help="Generate template quill.toml and .env files"
    )
    p_init.set_defaults(func=cmd_init)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:
        console.print(f"[bold red]Error: {exc}[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
