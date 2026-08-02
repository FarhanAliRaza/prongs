import pytest


def test_func_fixture(func_fixture):
    assert func_fixture == "func-value"


def test_module_fixture_first(module_resource):
    assert module_resource["module"].endswith("test_fixtures")


def test_module_fixture_second(module_resource):
    assert "handle" in module_resource


def test_session_fixture_increments(session_counter):
    session_counter["count"] += 1
    assert session_counter["count"] >= 1


def test_session_fixture_again(session_counter):
    session_counter["count"] += 1
    assert session_counter["count"] >= 1


def test_setup_failure(failing_setup_fixture):
    raise AssertionError("should never reach the call phase")


def test_teardown_failure(failing_teardown_fixture):
    assert failing_teardown_fixture == "ok"


class TestClassFixtures:
    @pytest.fixture(scope="class")
    def class_state(self):
        return {"created": True}

    def test_uses_class_state(self, class_state):
        assert class_state["created"]

    def test_reuses_class_state(self, class_state):
        assert class_state["created"]


@pytest.fixture
def yield_fixture_with_finalizer():
    resource = {"open": True}
    yield resource
    resource["open"] = False


def test_yield_finalization(yield_fixture_with_finalizer):
    assert yield_fixture_with_finalizer["open"]
