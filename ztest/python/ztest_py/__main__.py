"""ztest Python CLI.

    python -m ztest_py run -j auto -- tests/ -q

This is the Milestone 1 entry point (pure-Python prefork). The Zig `ztest`
binary supersedes it as the user-facing CLI from Milestone 3 on, launching
``python -m ztest_py host`` internally.
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="ztest-py")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a suite with the prefork prototype")
    run.add_argument("-j", "--jobs", default="auto")
    run.add_argument("--initial-batch", type=int, default=2)

    host = sub.add_parser("host", help="pytest host driven by the Zig controller")
    host.add_argument("--socket", required=True)

    if "--" in argv:
        split = argv.index("--")
        own, pytest_args = argv[:split], argv[split + 1 :]
    else:
        own, pytest_args = argv, []
    args = parser.parse_args(own)

    if args.command == "run":
        import pytest

        from .plugin import PreforkPlugin, _resolve_jobs

        jobs = _resolve_jobs(args.jobs) or (os.cpu_count() or 2)
        return int(
            pytest.main(
                pytest_args,
                plugins=[PreforkPlugin(jobs, args.initial_batch)],
            )
        )

    if args.command == "host":
        from .host import HostPlugin

        import pytest

        return int(pytest.main(pytest_args, plugins=[HostPlugin(args.socket)]))

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
