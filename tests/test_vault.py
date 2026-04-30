"""Unit tests for codebase_wiki_builder.vault module."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codebase_wiki_builder.vault import (
    MD5_FOOTER_RE,
    compute_md5,
    extract_stored_md5,
    is_binary_file,
    md5_footer,
    mirror_path,
    slugify,
    source_path_from_vault,
    summary_filename,
    vault_path_for_source,
    wikilink,
)


# ---------------------------------------------------------------------------
# summary_filename
# ---------------------------------------------------------------------------

class TestSummaryFilename:
    def test_python_file(self):
        assert summary_filename(Path("user_service.py")) == "user_service.py.md"

    def test_file_no_extension(self):
        assert summary_filename(Path("Makefile")) == "Makefile.md"

    def test_dotfile(self):
        assert summary_filename(Path(".env")) == ".env.md"

    def test_nested_path_only_uses_name(self):
        # summary_filename only cares about the name attribute
        assert summary_filename(Path("/some/deep/path/login.py")) == "login.py.md"

    def test_typescript_file(self):
        assert summary_filename(Path("index.ts")) == "index.ts.md"


# ---------------------------------------------------------------------------
# mirror_path
# ---------------------------------------------------------------------------

class TestMirrorPath:
    def test_root_level_file(self, tmp_path):
        codebase = tmp_path / "app"
        vault = tmp_path / "vault"
        codebase.mkdir(); vault.mkdir()
        source = codebase / "main.py"
        result = mirror_path(source, codebase, vault)
        assert result == vault

    def test_nested_file(self, tmp_path):
        codebase = tmp_path / "app"
        vault = tmp_path / "vault"
        codebase.mkdir(parents=True); vault.mkdir()
        source = codebase / "src" / "auth" / "login.py"
        result = mirror_path(source, codebase, vault)
        assert result == vault / "src" / "auth"

    def test_raises_when_file_not_under_codebase(self, tmp_path):
        codebase = tmp_path / "app"
        vault = tmp_path / "vault"
        codebase.mkdir(); vault.mkdir()
        outside = tmp_path / "other" / "file.py"
        with pytest.raises(ValueError):
            mirror_path(outside, codebase, vault)


# ---------------------------------------------------------------------------
# vault_path_for_source
# ---------------------------------------------------------------------------

class TestVaultPathForSource:
    def test_nested_source_file(self, tmp_path):
        codebase = tmp_path / "myapp"
        vault = tmp_path / "vault"
        source = codebase / "src" / "auth" / "login.py"
        result = vault_path_for_source(source, codebase, vault)
        assert result == vault / "src" / "auth" / "login.py.md"

    def test_root_level_file(self, tmp_path):
        codebase = tmp_path / "myapp"
        vault = tmp_path / "vault"
        source = codebase / "setup.py"
        result = vault_path_for_source(source, codebase, vault)
        assert result == vault / "setup.py.md"

    def test_dotfile(self, tmp_path):
        codebase = tmp_path / "myapp"
        vault = tmp_path / "vault"
        source = codebase / ".env"
        result = vault_path_for_source(source, codebase, vault)
        assert result == vault / ".env.md"


# ---------------------------------------------------------------------------
# source_path_from_vault
# ---------------------------------------------------------------------------

class TestSourcePathFromVault:
    def test_roundtrip(self, tmp_path):
        codebase = tmp_path / "myapp"
        vault = tmp_path / "vault"
        source = codebase / "src" / "auth" / "login.py"
        summary = vault_path_for_source(source, codebase, vault)
        recovered = source_path_from_vault(summary, vault, codebase)
        assert recovered == source

    def test_dotfile_roundtrip(self, tmp_path):
        codebase = tmp_path / "myapp"
        vault = tmp_path / "vault"
        source = codebase / ".env"
        summary = vault_path_for_source(source, codebase, vault)
        recovered = source_path_from_vault(summary, vault, codebase)
        assert recovered == source

    def test_strips_md_suffix(self, tmp_path):
        vault = tmp_path / "vault"
        codebase = tmp_path / "code"
        summary = vault / "utils.py.md"
        result = source_path_from_vault(summary, vault, codebase)
        assert result.name == "utils.py"
        assert result.suffix == ".py"


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_basic_sentence(self):
        assert slugify("How does auth work?") == "how-does-auth-work"

    def test_already_lowercase(self):
        assert slugify("hello world") == "hello-world"

    def test_multiple_spaces_collapse(self):
        assert slugify("hello   world") == "hello-world"

    def test_special_characters_stripped(self):
        assert slugify("foo!@#$bar") == "foobar"

    def test_multiple_hyphens_collapsed(self):
        assert slugify("foo--bar") == "foo-bar"

    def test_leading_trailing_hyphens_stripped(self):
        assert slugify("--hello--") == "hello"

    def test_empty_string(self):
        assert slugify("") == ""

    def test_all_special_chars(self):
        # All non-alphanumeric input → empty string
        result = slugify("!@#$%^&*()")
        assert result == ""

    def test_numbers_preserved(self):
        assert slugify("version 2 release") == "version-2-release"

    def test_unicode_stripped(self):
        # Non-ASCII chars like é are stripped since [^a-z0-9-] removes them
        result = slugify("café")
        assert "é" not in result


# ---------------------------------------------------------------------------
# compute_md5
# ---------------------------------------------------------------------------

class TestComputeMd5:
    def test_known_content(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")
        expected = hashlib.md5(b"hello world").hexdigest()
        assert compute_md5(f) == expected

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        expected = hashlib.md5(b"").hexdigest()
        assert compute_md5(f) == expected

    def test_returns_32_char_hexdigest(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("some content", encoding="utf-8")
        result = compute_md5(f)
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# extract_stored_md5
# ---------------------------------------------------------------------------

class TestExtractStoredMd5:
    def test_returns_none_for_nonexistent(self, tmp_path):
        assert extract_stored_md5(tmp_path / "nonexistent.md") is None

    def test_returns_none_for_missing_footer(self, tmp_path):
        f = tmp_path / "summary.md"
        f.write_text("# Summary\n\nSome content\n", encoding="utf-8")
        assert extract_stored_md5(f) is None

    def test_extracts_md5_from_footer(self, tmp_path):
        hexdigest = "a" * 32
        f = tmp_path / "summary.md"
        f.write_text(f"# Summary\n\nContent\n<!-- md5: {hexdigest} -->", encoding="utf-8")
        assert extract_stored_md5(f) == hexdigest

    def test_extracts_with_spaces_in_footer(self, tmp_path):
        hexdigest = "1234567890abcdef" * 2
        f = tmp_path / "summary.md"
        f.write_text(f"Content\n<!--  md5:  {hexdigest}  -->", encoding="utf-8")
        assert extract_stored_md5(f) == hexdigest

    def test_footer_on_last_line(self, tmp_path):
        hexdigest = "deadbeef" + "0" * 24
        f = tmp_path / "summary.md"
        content = f"Line 1\nLine 2\n<!-- md5: {hexdigest} -->"
        f.write_text(content, encoding="utf-8")
        assert extract_stored_md5(f) == hexdigest


# ---------------------------------------------------------------------------
# md5_footer
# ---------------------------------------------------------------------------

class TestMd5Footer:
    def test_basic_format(self):
        hexdigest = "a" * 32
        result = md5_footer(hexdigest)
        assert result == f"<!-- md5: {hexdigest} -->"

    def test_footer_is_parseable_by_regex(self):
        hexdigest = "1234567890abcdef1234567890abcdef"
        footer = md5_footer(hexdigest)
        m = MD5_FOOTER_RE.search(footer)
        assert m is not None
        assert m.group(1) == hexdigest


# ---------------------------------------------------------------------------
# is_binary_file
# ---------------------------------------------------------------------------

class TestIsBinaryFile:
    def test_text_file_is_not_binary(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("print('hello')\n", encoding="utf-8")
        assert is_binary_file(f) is False

    def test_png_extension_is_binary(self, tmp_path):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert is_binary_file(f) is True

    def test_null_byte_makes_binary(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"some text\x00more text")
        assert is_binary_file(f) is True

    def test_invalid_utf8_makes_binary(self, tmp_path):
        f = tmp_path / "latin1.txt"
        f.write_bytes(b"\xff\xfe binary garbage")
        assert is_binary_file(f) is True

    def test_pdf_extension_is_binary(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")
        assert is_binary_file(f) is True

    def test_exe_extension_is_binary(self, tmp_path):
        f = tmp_path / "app.exe"
        f.write_bytes(b"MZ header")
        assert is_binary_file(f) is True

    def test_empty_text_file_is_not_binary(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        assert is_binary_file(f) is False

    def test_case_insensitive_extension(self, tmp_path):
        f = tmp_path / "image.PNG"
        f.write_bytes(b"\x89PNG")
        assert is_binary_file(f) is True


# ---------------------------------------------------------------------------
# wikilink
# ---------------------------------------------------------------------------

class TestWikilink:
    def test_basic_wikilink(self, tmp_path):
        vault = tmp_path / "vault"
        summary = vault / "src" / "auth" / "login.py.md"
        result = wikilink(summary, vault)
        assert result == "[[src/auth/login.py]]"

    def test_root_level_wikilink(self, tmp_path):
        vault = tmp_path / "vault"
        summary = vault / "setup.py.md"
        result = wikilink(summary, vault)
        assert result == "[[setup.py]]"
