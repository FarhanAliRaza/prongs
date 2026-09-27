"""Selection settings: [tool.fastest] in pyproject.toml, overridden by
FASTEST_<NAME> environment variables, overridden by CLI flags.

  history_window   a test whose outcome flipped in any of the last N rollups
                   is selected ("recent status change")
  max_map_age      a map more than N commits behind HEAD (counted from the
                   commit the last rollup recorded) runs everything
  flaky_window     a test is flaky while both outcomes on one tree were
                   observed within the last N rollups; older evidence
                   expires (0: nothing is ever flaky)
  flaky_retries    a flaky test that fails is re-run alone up to N times: a
                   pass makes the failure a flake, failing every time makes it
                   a failure (0: a flaky failure is a failure)
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "history_window": 3,
    "max_map_age": 50,
    "flaky_window": 50,
    "flaky_retries": 2,
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
