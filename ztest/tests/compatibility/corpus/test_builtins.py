import os
import sys
import threading

import pytest


def test_capsys(capsys):
    print("hello-stdout")
    print("hello-stderr", file=sys.stderr)
    captured = capsys.readouterr()
    assert captured.out == "hello-stdout\n"
    assert captured.err == "hello-stderr\n"


def test_capsys_failure_shows_output(capsys):
    print("visible-in-failure")
    assert False


def test_monkeypatch(monkeypatch):
    monkeypatch.setenv("ZTEST_CORPUS_ENV", "yes")
    assert os.environ["ZTEST_CORPUS_ENV"] == "yes"


def test_monkeypatch_attr(monkeypatch):
    monkeypatch.setattr(sys, "ztest_sentinel", 42, raising=False)
    assert sys.ztest_sentinel == 42


def test_tmp_path(tmp_path):
    target = tmp_path / "data.txt"
    target.write_text("payload")
    assert target.read_text() == "payload"


def test_tmp_path_isolated(tmp_path):
    assert not list(tmp_path.iterdir())


def test_native_extension_import():
    import zlib

    assert zlib.crc32(b"ztest") != 0


def test_ctypes_native():
    import ctypes

    assert ctypes.c_int(7).value == 7


def test_starts_thread():
    results = []
    t = threading.Thread(target=lambda: results.append(threading.get_ident()))
    t.start()
    t.join()
    assert results and results[0] != threading.get_ident()


def test_subprocess(spawn_subprocess):
    assert spawn_subprocess("print('child-ok')") == "child-ok\n"


@pytest.mark.parametrize("i", range(5))
def test_prints_are_captured(i, capsys):
    print(f"noise-{i}")
    assert capsys.readouterr().out == f"noise-{i}\n"
