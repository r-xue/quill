import hashlib
import re

# ── Content hash footer ──────────────────────────────────────────────────
# Embedded as an invisible comment at the end of the Jira description so that
# change detection can happen without diffing the full body text.
#
#   <!-- quill:sha256:a1b2c3d4e5f6 -->

_HASH_PATTERN = re.compile(r"<!-- quill:sha256:([a-f0-9]+) -->")


# Bump this when the Jira ticket format changes (e.g. new summary format,
# new description layout) to force re-sync of all existing tickets.
_QUILL_FORMAT_VERSION = "7"  # v7: panel with bgColor, no title bar


def compute_content_hash(title: str, body: str, state: str, labels: list[str]) -> str:
    """
    SHA256 hash of the key issue fields plus the quill format version.
    Bumping _QUILL_FORMAT_VERSION invalidates all stored hashes, causing
    quill to re-sync every ticket on the next run.
    """
    cleaned_body = body or ""
    sorted_labels = ",".join(sorted(labels or []))
    content = f"v{_QUILL_FORMAT_VERSION}\n{title}\n{cleaned_body}\n{state}\n{sorted_labels}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def embed_hash_footer(description: str, content_hash: str) -> str:
    """
    Legacy: previously appended the hash as an HTML comment to the description.
    Now a no-op — the hash is stored invisibly via Jira entity properties.
    Kept for backward compatibility (old code paths that call this still work).
    """
    # Strip any legacy footer that may exist from older versions of quill
    return _HASH_PATTERN.sub("", description).rstrip()


def extract_hash_footer(description: str) -> str | None:
    """
    Extract the content hash from a legacy Jira description footer.
    Used only to migrate existing tickets that were created before quill
    switched to storing the hash in issue entity properties.
    Returns None if no footer is found.
    """
    if not description:
        return None
    match = _HASH_PATTERN.search(description)
    return match.group(1) if match else None


# ── Markdown → Jira Wiki Markup ──────────────────────────────────────────


def markdown_to_jira(markdown_text: str) -> str:
    """
    Convert basic Markdown syntax to Jira Wiki Markup syntax.
    """
    if not markdown_text:
        return ""

    text = markdown_text

    # 1. Code blocks: ```lang\ncode\n``` → {code:lang}\ncode\n{code}
    def _replace_code_block(match):
        lang = match.group(1) or ""
        code = match.group(2)
        if lang:
            return f"{{code:{lang}}}\n{code.strip()}\n{{code}}"
        return f"{{code}}\n{code.strip()}\n{{code}}"

    text = re.sub(
        r"```([a-zA-Z0-9_\-+]*)\n(.*?)\n```",
        _replace_code_block,
        text,
        flags=re.DOTALL,
    )

    # 2. Inline code: `code` → {{code}}
    text = re.sub(r"`([^`\n]+)`", r"{{\1}}", text)

    # 3. Headers: # Header → h1. Header
    def _replace_header(match):
        level = len(match.group(1))
        return f"h{level}. {match.group(2)}"

    text = re.sub(r"^(#{1,6})\s+(.+)$", _replace_header, text, flags=re.MULTILINE)

    # 4. Bold: **bold** or __bold__ → placeholder
    text = re.sub(
        r"\*\*([^*]+)\*\*|__([^_]+)__",
        lambda m: f"\x00{m.group(1) or m.group(2)}\x00",
        text,
    )

    # 5. Italic: *italic* or _italic_ → _italic_
    text = re.sub(
        r"\*([^*]+)\*|_([^_]+)_",
        lambda m: f"_{m.group(1) or m.group(2)}_",
        text,
    )

    # Convert placeholder to * (Jira bold)
    text = text.replace("\x00", "*")

    # 6. Links: [text](url) → [text|url]
    text = re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", r"[\1|\2]", text)

    # 7. Unordered lists: - item or * item → * item
    text = re.sub(r"^\s*[-*]\s+(.+)$", r"* \1", text, flags=re.MULTILINE)

    # 8. Ordered lists: 1. item → # item
    text = re.sub(r"^\s*\d+\.\s+(.+)$", r"# \1", text, flags=re.MULTILINE)

    # 9. Blockquotes: > text → {quote}text{quote}
    text = re.sub(r"^>\s+(.+)$", r"{quote}\1{quote}", text, flags=re.MULTILINE)

    return text


# ── Comment formatting ───────────────────────────────────────────────────


def format_comment(
    github_username: str, github_url: str, comment_body_markdown: str
) -> str:
    """
    Format a comment body with attribution linking back to GitHub.
    """
    jira_body = markdown_to_jira(comment_body_markdown)
    header = f"*@{github_username}* commented on GitHub ([link|{github_url}]):\n\n"
    return f"{header}{jira_body}"
