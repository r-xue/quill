from quill.mapper import (
    markdown_to_jira,
    format_comment,
    compute_content_hash,
    embed_hash_footer,
    extract_hash_footer,
)


class TestMarkdownToJira:
    def test_headers(self):
        assert markdown_to_jira("# Header 1") == "h1. Header 1"
        assert markdown_to_jira("## Header 2") == "h2. Header 2"

    def test_bold_and_italic(self):
        assert (
            markdown_to_jira("This is **bold** and *italic* text.")
            == "This is *bold* and _italic_ text."
        )

    def test_inline_code(self):
        assert markdown_to_jira("Use `print()` function.") == "Use {{print()}} function."

    def test_code_block(self):
        md = "```python\ndef hello():\n    print('world')\n```"
        expected = "{code:python}\ndef hello():\n    print('world')\n{code}"
        assert markdown_to_jira(md) == expected

    def test_links(self):
        assert (
            markdown_to_jira("Check [Google](https://google.com).")
            == "Check [Google|https://google.com]."
        )

    def test_unordered_list(self):
        assert markdown_to_jira("- item 1\n- item 2") == "* item 1\n* item 2"

    def test_asterisk_bullet_list_not_converted_to_italic(self):
        md = "* Expertise with radio astronomy\n* Experience with building workflows"
        expected = "* Expertise with radio astronomy\n* Experience with building workflows"
        assert markdown_to_jira(md) == expected

    def test_ordered_list(self):
        assert markdown_to_jira("1. first\n2. second") == "# first\n# second"

    def test_blockquote(self):
        assert markdown_to_jira("> important note") == "{quote}important note{quote}"


class TestFormatComment:
    def test_attribution_header(self):
        formatted = format_comment("johndoe", "https://github.com/comment/1", "body")
        assert "@johndoe" in formatted
        assert "https://github.com/comment/1" in formatted


class TestContentHash:
    def test_same_input_same_hash(self):
        h1 = compute_content_hash("T", "B", "open", ["bug"])
        h2 = compute_content_hash("T", "B", "open", ["bug"])
        assert h1 == h2

    def test_different_input_different_hash(self):
        h1 = compute_content_hash("T", "B", "open", ["bug"])
        h2 = compute_content_hash("T2", "B", "open", ["bug"])
        h3 = compute_content_hash("T", "B", "closed", ["bug"])
        assert h1 != h2
        assert h1 != h3


class TestHashFooter:
    def test_legacy_extract(self):
        desc = "Some Jira description text.\n\n<!-- quill:sha256:abc123deadbeef -->"
        assert extract_hash_footer(desc) == "abc123deadbeef"

    def test_extract_returns_none_when_absent(self):
        assert extract_hash_footer("no footer here") is None
        assert extract_hash_footer("") is None
        assert extract_hash_footer(None) is None

    def test_embed_replaces_old_footer(self):
        desc = "text\n\n<!-- quill:sha256:1234567890abcdef -->"
        embedded = embed_hash_footer(desc, "newnew")
        assert "1234567890abcdef" not in embedded
        assert "<!-- quill:sha256:newnew -->" in embedded
        assert embedded == "text\n\n<!-- quill:sha256:newnew -->"
