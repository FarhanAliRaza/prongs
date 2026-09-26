"""Selection settings: [tool.fastest] in pyproject.toml, overridden by
FASTEST_<NAME> environment variables, overridden by CLI flags.

  history_window   a test whose outcome flipped in any of the last N rollups
                   is selected ("recent status change")
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "history_window": 3,
}


def settings(repo: Path, **overrides) -> dict:
    out = dict(DEFAULTS)
    try:
        table = tomllib.loads((Path(repo) / "pyproject.toml").read_text())
        table = table.get("tool", {}).get("fastest", {})
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        table = {}
    for name in DEFAULTS:
        if name in table:
            out[name] = int(table[name])
        env = os.environ.get(f"FASTEST_{name.upper()}")
        if env:
            out[name] = int(env)
        if overrides.get(name) is not None:
            out[name] = int(overrides[name])
    return out
