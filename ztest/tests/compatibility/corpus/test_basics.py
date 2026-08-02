import pytest


def test_plain_pass():
    assert 1 + 1 == 2


def test_plain_fail():
    assert 1 + 1 == 3, "arithmetic is broken"


def test_assertion_rewriting_detail():
    left = {"a": 1, "b": 2}
    right = {"a": 1, "b": 3}
    assert left == right


class TestClassGrouping:
    def test_method_one(self):
        assert "abc".upper() == "ABC"

    def test_method_two(self):
        assert list(range(3)) == [0, 1, 2]


@pytest.mark.parametrize("value,expected", [(1, 1), (2, 4), (3, 9), (4, 16)])
def test_parametrized_square(value, expected):
    assert value * value == expected


@pytest.mark.parametrize("bad", [2, 5])
def test_parametrized_partial_failure(bad):
    assert bad % 2 == 0


def test_skip_unconditional():
    pytest.skip("always skipped")


@pytest.mark.skipif(True, reason="skipif marker")
def test_skipif_marker():
    raise AssertionError("never runs")


@pytest.mark.xfail(reason="known broken")
def test_xfail_failing():
    assert False


@pytest.mark.xfail(reason="unexpectedly passes")
def test_xfail_passing():
    assert True
