import asyncio

import pytest


def test_asyncio_via_run():
    async def compute():
        await asyncio.sleep(0)
        return 21 * 2

    assert asyncio.run(compute()) == 42


def test_error_not_assertion():
    raise ValueError("a non-assertion failure")


def test_nested_exception_chain():
    try:
        raise KeyError("inner")
    except KeyError as exc:
        raise RuntimeError("outer") from exc


@pytest.mark.parametrize("mode", ["ok", "raises"])
def test_expected_exception(mode):
    if mode == "raises":
        with pytest.raises(ZeroDivisionError):
            _ = 1 / 0
    else:
        assert True
