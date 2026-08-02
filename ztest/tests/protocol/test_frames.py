"""Milestone 2 exit criteria: framing, malformed frames, sequence, limits."""

import socket
import struct

import pytest

from ztest_py.protocol import (
    HEADER_SIZE,
    MAGIC,
    VERSION,
    Connection,
    MessageType,
    ProtocolError,
    ConnectionClosed,
    decode_header,
    encode_frame,
)


def make_pair():
    a, b = socket.socketpair()
    return Connection(a), Connection(b)


def test_roundtrip_simple():
    a, b = make_pair()
    a.send(MessageType.HELLO, {"role": "host"})
    frame = b.recv()
    assert frame.type is MessageType.HELLO
    assert frame.payload == {"role": "host"}
    assert frame.sequence == 0


def test_sequences_are_monotonic_per_connection():
    a, b = make_pair()
    for i in range(5):
        a.send(MessageType.HEARTBEAT, {"i": i})
    frames = [b.recv() for _ in range(5)]
    assert [f.sequence for f in frames] == [0, 1, 2, 3, 4]
    assert [f.payload["i"] for f in frames] == [0, 1, 2, 3, 4]


def test_empty_payload():
    a, b = make_pair()
    a.send(MessageType.SHUTDOWN)
    assert b.recv().payload == {}


def test_large_payload_roundtrip():
    a, b = make_pair()
    blob = {"data": "x" * 200_000}
    import threading

    t = threading.Thread(target=a.send, args=(MessageType.OUTPUT_CHUNK, blob))
    t.start()
    frame = b.recv()
    t.join()
    assert frame.payload == blob


def test_bad_magic_rejected():
    header = struct.pack("<4sHHIQ", b"NOPE", VERSION, 1, 0, 0)
    with pytest.raises(ProtocolError, match="bad magic"):
        decode_header(header)


def test_unknown_version_rejected():
    header = struct.pack("<4sHHIQ", MAGIC, VERSION + 1, 1, 0, 0)
    with pytest.raises(ProtocolError, match="version"):
        decode_header(header)


def test_unknown_message_type_rejected():
    header = struct.pack("<4sHHIQ", MAGIC, VERSION, 9999, 0, 0)
    with pytest.raises(ProtocolError, match="unknown message type"):
        decode_header(header)


def test_short_header_rejected():
    with pytest.raises(ProtocolError, match="short header"):
        decode_header(b"ZT")


def test_payload_size_limit_on_send():
    with pytest.raises(ProtocolError, match="exceeds limit"):
        encode_frame(MessageType.OUTPUT_CHUNK, 0, {"d": "x" * 100}, max_payload=10)


def test_payload_size_limit_on_receive():
    a_sock, b_sock = socket.socketpair()
    b = Connection(b_sock, max_payload=16)
    frame = encode_frame(MessageType.OUTPUT_CHUNK, 0, {"d": "x" * 100})
    a_sock.sendall(frame)
    with pytest.raises(ProtocolError, match="exceeds limit"):
        b.recv()


def test_non_monotonic_sequence_rejected():
    a_sock, b_sock = socket.socketpair()
    b = Connection(b_sock)
    a_sock.sendall(encode_frame(MessageType.HEARTBEAT, 5, {}))
    a_sock.sendall(encode_frame(MessageType.HEARTBEAT, 5, {}))
    assert b.recv().sequence == 5
    with pytest.raises(ProtocolError, match="non-monotonic"):
        b.recv()


def test_duplicate_completion_is_a_protocol_concern():
    # Sequence numbers make replayed frames detectable at the transport level.
    a_sock, b_sock = socket.socketpair()
    b = Connection(b_sock)
    frame = encode_frame(MessageType.TEST_FINISHED, 3, {"test_id": 7})
    a_sock.sendall(frame)
    assert b.recv().payload["test_id"] == 7
    a_sock.sendall(frame)  # byte-identical replay
    with pytest.raises(ProtocolError):
        b.recv()


def test_invalid_json_payload_rejected():
    a_sock, b_sock = socket.socketpair()
    b = Connection(b_sock)
    body = b"not json"
    header = struct.pack("<4sHHIQ", MAGIC, VERSION, 1, len(body), 0)
    a_sock.sendall(header + body)
    with pytest.raises(ProtocolError, match="invalid JSON"):
        b.recv()


def test_non_object_payload_rejected():
    a_sock, b_sock = socket.socketpair()
    b = Connection(b_sock)
    body = b"[1,2,3]"
    header = struct.pack("<4sHHIQ", MAGIC, VERSION, 1, len(body), 0)
    a_sock.sendall(header + body)
    with pytest.raises(ProtocolError, match="JSON object"):
        b.recv()


def test_clean_close_at_frame_boundary():
    a, b = make_pair()
    a.send(MessageType.GOODBYE)
    a.close()
    assert b.recv().type is MessageType.GOODBYE
    with pytest.raises(ConnectionClosed):
        b.recv()


def test_close_mid_frame_is_error():
    a_sock, b_sock = socket.socketpair()
    b = Connection(b_sock)
    frame = encode_frame(MessageType.HELLO, 0, {"role": "worker"})
    a_sock.sendall(frame[: HEADER_SIZE + 3])
    a_sock.close()
    with pytest.raises(ProtocolError, match="mid-frame"):
        b.recv()


def test_drain_parses_multiple_buffered_frames():
    a, b = make_pair()
    for i in range(10):
        a.send(MessageType.TEST_REPORT, {"n": i})
    import time

    time.sleep(0.05)
    frames, eof = b.drain()
    assert [f.payload["n"] for f in frames] == list(range(10))
    assert not eof


def test_drain_reports_eof():
    a, b = make_pair()
    a.send(MessageType.GOODBYE)
    a.close()
    import time

    time.sleep(0.05)
    frames, eof = b.drain()
    assert frames[-1].type is MessageType.GOODBYE
    assert eof
