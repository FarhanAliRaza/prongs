"""Pytest plugin that swaps the serial runtest loop for the prefork engine.

Usage (prototype mode, Milestone 1):

    python -m ztest_py run -j 2 -- tests/ -q

or directly:

    pytest -p ztest_py.plugin --ztest-jobs=2 tests/

Real pytest still performs configuration, plugin loading, assertion
rewriting, collection, fixtures and execution; this plugin only replaces the
serial ``pytest_runtestloop`` with the collect-once prefork controller.
"""

from __future__ import annotations

import os

import pytest


def _resolve_jobs(value: str | int | None) -> int | None:
    if value in (None, "", "0"):
        return None
    if value == "auto":
        return os.cpu_count() or 2
    return int(value)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ztest")
    group.addoption(
        "--ztest-jobs",
        default=None,
        help="Run tests with N preforked workers ('auto' for CPU count).",
    )
    group.addoption(
        "--ztest-initial-batch",
        type=int,
        default=2,
        help="Tests initially queued per worker (minimum 2 for nextitem lookahead).",
    )


class PreforkPlugin:
    """Collects once, then hands the item list to the prefork controller."""

    def __init__(self, jobs: int, initial_batch: int = 2) -> None:
        self.jobs = jobs
        self.initial_batch = initial_batch
        self.items: list[pytest.Item] = []

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.items = list(session.items)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool | None:
        if session.config.option.collectonly:
            return None
        if not session.items:
            return None
        if session.testsfailed and not session.config.option.continue_on_collection_errors:
            return None
        from .prototype import run_prefork

        run_prefork(session, self.jobs, initial_batch=self.initial_batch)
        return True  # Prevent pytest's default serial loop.


def pytest_configure(config: pytest.Config) -> None:
    jobs = _resolve_jobs(config.getoption("--ztest-jobs", default=None))
    if jobs is not None and not config.pluginmanager.hasplugin("ztest-prefork"):
        config.pluginmanager.register(
            PreforkPlugin(jobs, config.getoption("--ztest-initial-batch")),
            "ztest-prefork",
        )
