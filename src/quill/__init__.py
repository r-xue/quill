"""
quill: GitHub issues to Jira sync tool.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("quill-sync")
except PackageNotFoundError:
    try:
        from ._version import __version__  # type: ignore[import-not-found, import-untyped]
    except ImportError:
        __version__ = "0.0.0-dev"
