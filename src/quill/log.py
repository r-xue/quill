import logging
from rich.logging import RichHandler

# Set up logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)],
)

logger = logging.getLogger("quill")


def redact_token(token: str) -> str:
    """
    Redact a token, showing only the first and last few characters.
    """
    if not token:
        return "None"
    if len(token) <= 8:
        return "********"
    return f"{token[:4]}...{token[-4:]}"
