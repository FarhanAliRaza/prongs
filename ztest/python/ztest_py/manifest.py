"""Manifest construction: compact integer-indexed descriptions of collected
tests, sent once to the controller (Milestone 2).

No Python objects cross the process boundary — both sides speak in integer
test ids that index this manifest.
"""

from __future__ import annotations

from typing import Any

import pytest


def manifest_item(item: pytest.Item, test_id: int) -> dict[str, Any]:
    path, line, _domain = item.location
    fixture_names: list[str] = list(getattr(item, "fixturenames", []) or [])
    scopes: list[str] = []
    try:
        info = item._fixtureinfo  # type: ignore[attr-defined]
        for name in fixture_names:
            defs = info.name2fixturedefs.get(name)
            scopes.append(defs[-1].scope if defs else "function")
    except Exception:
        scopes = ["function"] * len(fixture_names)
    return {
        "test_id": test_id,
        "nodeid": item.nodeid,
        "path": str(path),
        "line": line if line is not None else -1,
        "markers": sorted({mark.name for mark in item.iter_markers()}),
        "fixture_names": fixture_names,
        "fixture_scopes": scopes,
        "collection_order": test_id,
        "group_hint": str(path),
    }


def build_manifest(items: list[pytest.Item]) -> list[dict[str, Any]]:
    return [manifest_item(item, i) for i, item in enumerate(items)]
