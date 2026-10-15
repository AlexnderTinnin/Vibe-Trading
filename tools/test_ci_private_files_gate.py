"""Exercise the privacy gate against real temporary Git indexes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ci_private_files_gate import main


def _git(root: Path, *args: str) -> None:
    """Run Git in an isolated test repository."""
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a real repository with an ignored internal-documents directory."""
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("/docs/\nAGENTS.md\nCLAUDE.md\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".gitignore")
    return tmp_path


@pytest.mark.parametrize("name", [
    "docs/sessions/private.md",
    "docs/plans/design.json",
    "docs",
    "DOCS/private.md",
    "AGENTS.md",
    "agent/AGENTS.override.md",
    "CLAUDE.md",
    "frontend/CLAUDE.local.md",
    "MEMORY.md",
    "nested/CODEX_MEMORY.md",
    "NOTES.md",
    "docs/notes with spaces\nand newline.md",
])
def test_force_added_internal_documents_fail(repo: Path, name: str, capsys) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("private content must never appear in output", encoding="utf-8")
    _git(repo, "add", "-f", "--", name)

    assert main(repo) == 1
    output = capsys.readouterr().out
    assert ascii(name) in output
    assert "private content" not in output


def test_public_docs_and_ignored_local_notes_pass(repo: Path) -> None:
    for name in ["wiki/docs/guide.md", "agent/src/skills/example/SKILL.md", "README.md"]:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public documentation", encoding="utf-8")
        _git(repo, "add", "--", name)
    (repo / "docs").mkdir()
    (repo / "docs/private.md").write_text("local notes", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("local instructions", encoding="utf-8")

    assert main(repo) == 0


def test_unstaged_deletion_does_not_hide_an_indexed_file(repo: Path) -> None:
    path = repo / "docs/private.md"
    path.parent.mkdir()
    path.write_text("private", encoding="utf-8")
    _git(repo, "add", "-f", "--", "docs/private.md")
    path.unlink()

    assert main(repo) == 1


def test_untracking_keeps_local_content_and_clears_failure(repo: Path) -> None:
    path = repo / "docs/private.md"
    path.parent.mkdir()
    original = b"local notes\r\n\x00unchanged\n"
    path.write_bytes(original)
    _git(repo, "add", "-f", "--", "docs/private.md")
    assert main(repo) == 1

    _git(repo, "rm", "--cached", "--", "docs/private.md")

    assert path.read_bytes() == original
    assert main(repo) == 0


def test_missing_git_repository_fails_closed(tmp_path: Path, capsys) -> None:
    assert main(tmp_path) == 2
    assert "check did not run" in capsys.readouterr().out
