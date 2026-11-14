"""Selection settings: [tool.prongs] in pyproject.toml, overridden by
PRONGS_<NAME> environment variables, overridden by CLI flags.

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

[tool.prongs] only:

  inert            glob patterns for tracked non-Python files that cannot
                   affect test outcomes, on top of the built-in list (docs,
                   .github/, *.md ...). A pattern is matched against the
                   path and against the file name: "*.svg", "assets/*"
  native_commands  programs (by file name: "git", "node") that are not Python
                   and run no project Python: a test that starts one is
                   selected by a changed non-Python file, not by changed code
  ignore_docstrings  true: a docstring edit is not a change. Off by default:
                   a docstring is data a program can read (`__doc__`, a CLI's
                   help text). Also PRONGS_IGNORE_DOCSTRINGS=1
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


def _table(repo: Path) -> dict:
    try:
        table = tomllib.loads((Path(repo) / "pyproject.toml").read_text())
        return table.get("tool", {}).get("prongs", {})
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def inert(repo: Path) -> tuple[str, ...]:
    return names(repo, "inert")  # a bare string would match per character


def names(repo: Path, name: str) -> tuple[str, ...]:
    """A list-of-strings setting; anything else is no list at all."""
    value = _table(repo).get(name, [])
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def flag(repo: Path, name: str) -> bool:
    env = os.environ.get(f"PRONGS_{name.upper()}")
    if env:
        return env.lower() in ("1", "true", "yes")
    return _table(repo).get(name) is True


def settings(repo: Path, **overrides) -> dict:
    out = dict(DEFAULTS)
    table = _table(repo)
    for name in DEFAULTS:
        if name in table:
            out[name] = int(table[name])
        env = os.environ.get(f"PRONGS_{name.upper()}")
        if env:
            out[name] = int(env)
        if overrides.get(name) is not None:
            out[name] = int(overrides[name])
    return out
