"""Where the evidence came from: git state at observation time.

A dependency map is only as trustworthy as our knowledge of the tree it was
observed on. Every recorded run stores the commit HEAD pointed at, plus a
content hash for every file that did not match HEAD (modified, added,
untracked) at the start and at the end of the run. The selector uses that to
diff against the tree the evidence actually saw, not the tree the caller
assumes.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return p.stdout


def git_head(repo: Path) -> str | None:
    out = _git(repo, "rev-parse", "HEAD")
    return out.strip() if out else None


def rev_parse(repo: Path, rev: str) -> str | None:
    out = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
    return out.strip() if out else None


def commits_behind(repo: Path, commit: str) -> int | None:
    """How many commits HEAD is ahead of `commit` (None if not an ancestor)."""
    if _git(repo, "merge-base", "--is-ancestor", commit, "HEAD") is None:
        return None
    out = _git(repo, "rev-list", "--count", f"{commit}..HEAD")
    return int(out.strip()) if out else None


def count_commits(repo: Path, since: str, rev: str = "HEAD") -> int | None:
    """Commits reachable from `rev` but not from `since` (None if either is
    unknown): how far `rev` has moved on since `since`, merge-base or not."""
    out = _git(repo, "rev-list", "--count", f"{since}..{rev}")
    return int(out.strip()) if out else None


def is_shallow(repo: Path) -> bool:
    out = _git(repo, "rev-parse", "--is-shallow-repository")
    return bool(out) and out.strip() == "true"


def shallow_hint(repo: Path) -> str:
    """Why an evidence commit may be missing, when this clone is shallow."""
    if not is_shallow(repo):
        return ""
    return " (shallow clone: run `fastest ci restore`, or fetch the full history)"


def exists_at(repo: Path, rev: str, path: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{rev}:{path}") is not None


MAX_BLOB = 4 << 20  # content above this is hashed but not stored


def bytes_hash(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def read_and_hash(path: Path) -> tuple[str | None, bytes | None]:
    """(blake2b of the file's bytes, the bytes); (None, None) if absent. A file
    above MAX_BLOB is not read: it gets a size+mtime pseudo-hash and no bytes,
    which still detects change (conservatively) without touching big binaries."""
    try:
        st = path.stat()
        if st.st_size > MAX_BLOB:
            return f"stat:{st.st_size}:{st.st_mtime_ns}", None
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None, None
    return bytes_hash(data), data


def file_hash(path: Path) -> str | None:
    """blake2b of the file's bytes; None if it does not exist."""
    return read_and_hash(path)[0]


def blob_hash_at(repo: Path, rev: str, path: str) -> str | None:
    """Hash of `path` as it exists in commit `rev` (same digest as file_hash)."""
    try:
        p = subprocess.run(
            ["git", "show", f"{rev}:{path}"], cwd=repo, capture_output=True, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return bytes_hash(p.stdout)


def _own_state(repo: Path) -> tuple[str, ...]:
    """Path prefixes under the repository that hold fastest's own state (the
    map, the journal). They are never part of the tree a run observed: a
    project that does not ignore .fastest/ would otherwise see every run's
    journal file as a change to its tree."""
    prefixes = {".fastest/"}
    for var in ("FASTEST_DIR", "FASTEST_JOURNAL"):
        value = os.environ.get(var)
        if value:
            try:
                rel = (repo / value).resolve().relative_to(repo.resolve())
            except ValueError:
                continue  # outside the repository: git never reports it
            prefixes.add(rel.as_posix().rstrip("/") + "/")
    return tuple(prefixes)


def dirty_files(repo: Path, contents: dict[str, bytes] | None = None) -> dict[str, str | None]:
    """path -> content hash for every file that differs from HEAD.

    Covers staged, unstaged and untracked (non-ignored) files, except
    fastest's own state. Deleted files hash to None. Renames report the new
    path. When `contents` is given, the bytes of each Python file (the only
    kind the selector line-diffs) are stored in it by hash.
    """
    out = _git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
    if not out:
        return {}
    own = _own_state(repo)
    result: dict[str, str | None] = {}
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":  # rename/copy: the original path follows
            i += 1
        if path.startswith(own):
            continue
        h, data = read_and_hash(repo / path)
        result[path] = h
        if contents is not None and data is not None and path.endswith(".py"):
            contents[h] = data
    return result


def snapshot(repo: Path) -> dict:
    """{'commit': sha|None, 'dirty': {path: hash|None}, 'contents': {hash: bytes}}
    for the tree right now."""
    contents: dict[str, bytes] = {}
    dirty = dirty_files(repo, contents)
    return {"commit": git_head(repo), "dirty": dirty, "contents": contents}


def between(start: dict, end: dict) -> dict:
    """What a run observed, from snapshots taken at its start and end: the
    commit, every dirty path with its hash at start and end, the content of
    the dirty .py files, and whether the tree changed under the run."""
    return {
        "commit": start["commit"],
        "dirty_files": {
            p: [start["dirty"].get(p), end["dirty"].get(p)]
            for p in start["dirty"].keys() | end["dirty"].keys()
        },
        "blobs": end["contents"],  # what the run actually saw of dirty files
        "tree_changed": start["commit"] != end["commit"] or start["dirty"] != end["dirty"],
    }


def recorder_fingerprint() -> str:
    from fastest import __version__

    try:
        import pytest

        pv = pytest.__version__
    except ImportError:  # pragma: no cover - the recorder runs inside pytest
        pv = "?"
    py = ".".join(str(v) for v in sys.version_info[:3])
    return f"fastest={__version__};python={py};pytest={pv};{platform.system().lower()}"
