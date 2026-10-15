#!/usr/bin/env python3
"""Reject internal documents in the Git index, including force-added files.

Run before committing with ``python tools/ci_private_files_gate.py``.
Only indexed paths are inspected; ignored local files and their contents are
never read. The same index check runs on CI's checked-out commit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PurePosixPath


PRIVATE_DOCUMENT_NAMES = frozenset({
    "agents.md",
    "agents.override.md",
    "claude.md",
    "claude.local.md",
    "memory.md",
    "codex_memory.md",
    "notes.md",
})


def main(repo_root: Path | None = None) -> int:
    """Check the Git index for internal document paths.

    Args:
        repo_root: Repository to check; defaults to this script's repository.

    Returns:
        Zero for a clean index, one for forbidden paths, or two if Git fails.
    """
    root = repo_root if repo_root is not None else Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--full-name", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        print("FAIL: cannot read the Git index; internal-document check did not run.")
        return 2

    forbidden = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        name = os.fsdecode(raw_path)
        path = PurePosixPath(name)
        if path.parts[0].casefold() == "docs" or path.name.casefold() in PRIVATE_DOCUMENT_NAMES:
            forbidden.append(name)

    if forbidden:
        print("FAIL: internal documents are tracked in Git:")
        for name in sorted(set(forbidden)):
            # Escape control characters so a filename cannot inject CI output.
            print(f"  {ascii(name)}")
        print("Use git rm --cached -- <path> to retain the local file without tracking it.")
        print("Publish project documentation under wiki/docs/ or the relevant source directory.")
        return 1

    print("OK: no internal documents are tracked in Git.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
