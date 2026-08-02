import subprocess
import sys

import pytest

SESSION_EVENTS: list[str] = []


@pytest.fixture(scope="session")
def session_counter():
    """Session fixture with observable setup/teardown."""
    SESSION_EVENTS.append("session-setup")
    state = {"count": 0}
    yield state
    SESSION_EVENTS.append("session-teardown")


@pytest.fixture(scope="module")
def module_resource(request):
    return {"module": request.module.__name__, "handle": object()}


@pytest.fixture
def func_fixture():
    return "func-value"


@pytest.fixture
def failing_setup_fixture():
    raise RuntimeError("setup exploded on purpose")


@pytest.fixture
def failing_teardown_fixture():
    yield "ok"
    raise RuntimeError("teardown exploded on purpose")


@pytest.fixture
def spawn_subprocess():
    def _run(code: str) -> str:
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    return _run
