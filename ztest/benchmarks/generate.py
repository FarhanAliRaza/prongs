"""Milestone 0: synthetic benchmark suite generators.

Each generator writes a self-contained pytest suite into a target directory.
Suites are deterministic (seeded) so runs are reproducible.

    python benchmarks/generate.py tiny --tests 20000 --out testbeds/tiny
    python benchmarks/generate.py all --out testbeds
"""

from __future__ import annotations

import argparse
import random
import textwrap
from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def gen_tiny(out: Path, tests: int = 20_000, per_module: int = 200) -> None:
    """Tests taking roughly 50-500 us each: framework overhead dominates."""
    modules = max(1, tests // per_module)
    rng = random.Random(1)
    for m in range(modules):
        lines = ["def _spin(n):", "    s = 0", "    for i in range(n):", "        s += i*i", "    return s", ""]
        for t in range(per_module):
            n = rng.randint(50, 500) * 4  # ~50-500us of pure-python arithmetic
            lines += [
                f"def test_tiny_{m}_{t}():",
                f"    assert _spin({n}) >= 0",
                "",
            ]
        _write(out / f"test_tiny_{m:04d}.py", "\n".join(lines))


def gen_collection_heavy(out: Path, modules: int = 150, per_module: int = 20) -> None:
    """Expensive import/collection: each module burns CPU at import time,
    the way large real-world test modules pay for imports, decorators and
    class bodies. Repeating collection per worker is what this punishes."""
    for m in range(modules):
        lines = [
            "import hashlib",
            "",
            "# simulate heavy import-time work (framework imports, ORM model",
            "# registration, decorator evaluation)",
            "_blob = b'x' * 4096",
            "for _ in range(300):",
            "    _blob = hashlib.sha256(_blob).digest() * 128",
            "",
            "import pytest",
            "",
            "@pytest.mark.parametrize('i', range(%d))" % per_module,
            "def test_param_%04d(i):" % m,
            "    assert i >= 0",
            "",
        ]
        _write(out / f"test_colheavy_{m:04d}.py", "\n".join(lines))


def gen_fixture_heavy(out: Path, modules: int = 30, per_module: int = 40) -> None:
    conftest = textwrap.dedent(
        """
        import time
        import pytest

        @pytest.fixture(scope="session")
        def expensive_session():
            time.sleep(0.3)
            return {"db": "session-handle"}

        @pytest.fixture(scope="module")
        def expensive_module():
            time.sleep(0.05)
            return {"conn": object()}

        @pytest.fixture
        def cheap_func():
            return 42
        """
    )
    _write(out / "conftest.py", conftest)
    for m in range(modules):
        lines = ["import pytest", ""]
        lines += [
            "class TestWithClassFixture:",
            "    @pytest.fixture(scope='class')",
            "    def class_state(self):",
            "        import time; time.sleep(0.02)",
            "        return {}",
            "",
        ]
        for t in range(per_module // 2):
            lines += [
                f"    def test_cls_{t}(self, class_state, cheap_func):",
                "        assert cheap_func == 42",
                "",
            ]
        for t in range(per_module - per_module // 2):
            lines += [
                f"def test_mod_{m}_{t}(expensive_session, expensive_module):",
                "    assert expensive_session['db'] == 'session-handle'",
                "",
            ]
        _write(out / f"test_fixheavy_{m:04d}.py", "\n".join(lines))


def gen_uneven(out: Path, tests: int = 400) -> None:
    """Durations from milliseconds to seconds; punishes static sharding."""
    rng = random.Random(2)
    per_module = 40
    for m in range(max(1, tests // per_module)):
        lines = ["import time", ""]
        for t in range(per_module):
            r = rng.random()
            if r < 0.75:
                dur = rng.uniform(0.001, 0.01)
            elif r < 0.95:
                dur = rng.uniform(0.02, 0.2)
            else:
                dur = rng.uniform(0.5, 2.0)
            lines += [
                f"def test_uneven_{m}_{t}():",
                f"    time.sleep({dur:.4f})",
                "    assert True",
                "",
            ]
        _write(out / f"test_uneven_{m:03d}.py", "\n".join(lines))


def gen_cpu_heavy(out: Path, tests: int = 64) -> None:
    per_module = 8
    for m in range(max(1, tests // per_module)):
        lines = [
            "def _work():",
            "    s = 0",
            "    for i in range(600_000):",
            "        s += i * i % 7",
            "    return s",
            "",
        ]
        for t in range(per_module):
            lines += [
                f"def test_cpu_{m}_{t}():",
                "    assert _work() >= 0",
                "",
            ]
        _write(out / f"test_cpu_{m:03d}.py", "\n".join(lines))


def gen_io_heavy(out: Path, tests: int = 200) -> None:
    per_module = 20
    for m in range(max(1, tests // per_module)):
        lines = [
            "import socket",
            "import subprocess",
            "import sys",
            "import tempfile",
            "import time",
            "",
        ]
        for t in range(per_module):
            kind = t % 4
            if kind == 0:
                body = ["    time.sleep(0.02)"]
            elif kind == 1:
                body = [
                    "    a, b = socket.socketpair()",
                    "    a.sendall(b'ping')",
                    "    assert b.recv(4) == b'ping'",
                    "    a.close(); b.close()",
                ]
            elif kind == 2:
                body = [
                    "    with tempfile.NamedTemporaryFile() as f:",
                    "        f.write(b'x' * 65536)",
                    "        f.flush()",
                ]
            else:
                body = [
                    "    out = subprocess.run([sys.executable, '-c', 'print(1)'],"
                    " capture_output=True)",
                    "    assert out.stdout.strip() == b'1'",
                ]
            lines += [f"def test_io_{m}_{t}():"] + body + ["    assert True", ""]
        _write(out / f"test_io_{m:03d}.py", "\n".join(lines))


def gen_failure_heavy(out: Path, tests: int = 200) -> None:
    rng = random.Random(3)
    conftest = textwrap.dedent(
        """
        import pytest

        @pytest.fixture
        def broken_setup():
            raise RuntimeError("setup failure")

        @pytest.fixture
        def broken_teardown():
            yield 1
            raise RuntimeError("teardown failure")
        """
    )
    _write(out / "conftest.py", conftest)
    per_module = 25
    for m in range(max(1, tests // per_module)):
        lines = ["import pytest", ""]
        for t in range(per_module):
            kind = rng.randrange(7)
            name = f"test_fh_{m}_{t}"
            if kind == 0:
                lines += [f"def {name}():", "    assert 1 == 2", ""]
            elif kind == 1:
                lines += [f"def {name}(broken_setup):", "    pass", ""]
            elif kind == 2:
                lines += [f"def {name}(broken_teardown):", "    assert broken_teardown == 1", ""]
            elif kind == 3:
                lines += [f"def {name}():", "    pytest.skip('skipped')", ""]
            elif kind == 4:
                lines += [
                    "@pytest.mark.xfail(reason='known')",
                    f"def {name}():",
                    "    assert False",
                    "",
                ]
            elif kind == 5:
                lines += [f"def {name}():", "    raise ValueError('boom')", ""]
            else:
                lines += [f"def {name}():", "    assert True", ""]
        _write(out / f"test_fh_{m:03d}.py", "\n".join(lines))


GENERATORS = {
    "tiny": gen_tiny,
    "collection_heavy": gen_collection_heavy,
    "fixture_heavy": gen_fixture_heavy,
    "uneven": gen_uneven,
    "cpu_heavy": gen_cpu_heavy,
    "io_heavy": gen_io_heavy,
    "failure_heavy": gen_failure_heavy,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", choices=[*GENERATORS, "all"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--tests", type=int, default=None)
    args = parser.parse_args()

    if args.suite == "all":
        for name, gen in GENERATORS.items():
            target = Path(args.out) / name
            gen(target)
            print(f"generated {name} -> {target}")
    else:
        gen = GENERATORS[args.suite]
        kwargs = {}
        if args.tests is not None:
            kwargs["tests"] = args.tests
        gen(Path(args.out), **kwargs)
        print(f"generated {args.suite} -> {args.out}")


if __name__ == "__main__":
    main()
