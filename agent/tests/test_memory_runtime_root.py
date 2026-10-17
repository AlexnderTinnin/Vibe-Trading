"""Memory state must follow VIBE_TRADING_HOME like every other runtime path."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]


def test_memory_defaults_honor_vibe_trading_home(tmp_path):
    home = tmp_path / "vt-home"
    code = (
        "from src.memory.persistent import MEMORY_BASE;"
        "from src.memory.search_index import _DEFAULT_DB_PATH;"
        "print(MEMORY_BASE);print(_DEFAULT_DB_PATH)"
    )
    env = {"PATH": "", "VIBE_TRADING_HOME": str(home), "HOME": str(tmp_path / "h")}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=AGENT_DIR, env=env,
        capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    assert Path(out[0]) == home / "memory"
    assert Path(out[1]) == home / "memory_index.db"
