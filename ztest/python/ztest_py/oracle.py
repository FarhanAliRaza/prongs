"""CLI entry for the correctness oracle: ``pytest -p ztest_py.oracle``.

Registers reporting.OraclePlugin when --ztest-oracle-out is given, so the same
oracle runs identically under vanilla pytest, pytest-xdist and the prefork
prototype.
"""

from __future__ import annotations

import pytest

from .reporting import OraclePlugin


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ztest")
    group.addoption(
        "--ztest-oracle-out",
        default=None,
        help="Write per-test phase outcomes to this JSON file.",
    )


def pytest_configure(config: pytest.Config) -> None:
    out = config.getoption("--ztest-oracle-out")
    if out:
        config.pluginmanager.register(OraclePlugin(out), "ztest-oracle")
