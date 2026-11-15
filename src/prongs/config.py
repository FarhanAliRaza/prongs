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
  isolate_failures 1: a test that fails in a selected run is run again in a
                   fresh process with only the other failures. Passing there,
                   it is order-dependent (another selected test left state
                   behind, and the tests that tidy up in a full run were not
                   selected): reported, not a failure (0: a failure)

[tool.prongs] only:

  inert            glob patterns for tracked non-Python files that cannot
                   affect test outcomes, on top of the built-in list (docs,
                   .github/, *.md ...). A pattern is matched against the
                   path and against the file name: "*.svg", "assets/*"
  native_commands  programs (by file name: "git", "node") that are not Python
                   and run no project Python: a test that starts one is
                   selected by a changed non-Python file, not by changed code
  import_state     true: a recorded run also stores a fingerprint of what
                   import produced, and a change that would run everything
                   only because import-time code changed selects the tests
                   that can reach what import now produces differently
                   (state.py). "shadow": keep running everything, and report
                   what selecting would have skipped. PRONGS_IMPORT_STATE too
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
    "isolate_failures": 1,
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


def import_state(repo: Path) -> str | None:
    """'select' (true), 'shadow', or None: what the import-state rule does."""
    value = os.environ.get("PRONGS_IMPORT_STATE") or _table(repo).get("import_state")
    if value is True or (isinstance(value, str) and value.lower() in ("1", "true", "select")):
        return "select"
    return "shadow" if isinstance(value, str) and value.lower() == "shadow" else None


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
