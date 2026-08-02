"""Versioned frame protocol shared by the prototype, the Python host and the
Zig controller (Milestone 2).

Frame layout (little-endian):

    magic         u32   "ZTST" (bytes 0x5A 0x54 0x53 0x54 on the wire)
    version       u16
    message_type  u16
    payload_size  u32
    sequence      u64
    payload       bytes  (UTF-8 JSON object)

Payloads are length-prefixed JSON for the first implementation; high-frequency
messages move to fixed binary payloads only after profiling shows the need.

Invariants enforced here:

* every frame carries a monotonically increasing per-connection sequence number,
* unknown protocol versions fail clearly,
* payload size is bounded by a configurable limit,
* a truncated or corrupt header is a hard protocol error.
"""

from __future__ import annotations

import enum
import json
import socket
import struct
from dataclasses import dataclass
from typing import Any

MAGIC = b"ZTST"
VERSION = 1

# magic u32 + version u16 + type u16 + payload_size u32 + sequence u64
_HEADER = struct.Struct("<4sHHIQ")
HEADER_SIZE = _HEADER.size  # 20 bytes

DEFAULT_MAX_PAYLOAD = 64 * 1024 * 1024


class MessageType(enum.IntEnum):
    HELLO = 1
    HOST_READY = 2

    MANIFEST_BEGIN = 3
    MANIFEST_ITEM = 4
    MANIFEST_END = 5

    SPAWN_WORKER = 6
    WORKER_READY = 7
    WORKER_EXITED = 8

    ASSIGN_TESTS = 9
    NO_MORE_TESTS = 10
    CANCEL_TEST = 11
    SHUTDOWN = 12

    TEST_STARTED = 13
    TEST_REPORT = 14
    TEST_FINISHED = 15
    OUTPUT_CHUNK = 16

    HEARTBEAT = 17
    PROTOCOL_ERROR = 18
    GOODBYE = 19


class ProtocolError(Exception):
    """Raised on any framing violation. Not recoverable on this connection."""


class ConnectionClosed(ProtocolError):
    """The peer closed the connection cleanly at a frame boundary."""


@dataclass(frozen=True)
class Frame:
    type: MessageType
    sequence: int
    payload: dict[str, Any]


def encode_frame(
    message_type: MessageType,
    sequence: int,
    payload: dict[str, Any],
    *,
    max_payload: int = DEFAULT_MAX_PAYLOAD,
) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > max_payload:
        raise ProtocolError(
            f"payload of {len(body)} bytes exceeds limit {max_payload}"
        )
    return _HEADER.pack(MAGIC, VERSION, int(message_type), len(body), sequence) + body


def decode_header(header: bytes, *, max_payload: int = DEFAULT_MAX_PAYLOAD) -> tuple[MessageType, int, int]:
    """Return (message_type, payload_size, sequence) or raise ProtocolError."""
    if len(header) != HEADER_SIZE:
        raise ProtocolError(f"short header: {len(header)} bytes")
    magic, version, mtype, size, sequence = _HEADER.unpack(header)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ProtocolError(f"unsupported protocol version {version}")
    if size > max_payload:
        raise ProtocolError(f"payload of {size} bytes exceeds limit {max_payload}")
    try:
        mtype = MessageType(mtype)
    except ValueError:
        raise ProtocolError(f"unknown message type {mtype}") from None
    return mtype, size, sequence


class Connection:
    """A framed, sequence-checked connection over a stream socket.

    Reads and writes are blocking; the controller side multiplexes with
    selectors/poll, the worker side uses one blocking connection.
    """

    def __init__(self, sock: socket.socket, *, max_payload: int = DEFAULT_MAX_PAYLOAD) -> None:
        self._sock = sock
        self._max_payload = max_payload
        self._send_seq = 0
        self._recv_seq = -1
        self._buffer = b""

    def fileno(self) -> int:
        return self._sock.fileno()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def send(self, message_type: MessageType, payload: dict[str, Any] | None = None) -> None:
        frame = encode_frame(
            message_type,
            self._send_seq,
            payload or {},
            max_payload=self._max_payload,
        )
        self._send_seq += 1
        self._sock.sendall(frame)

    def recv(self) -> Frame:
        while True:
            frame = self._parse_one()
            if frame is not None:
                return frame
            chunk = self._sock.recv(65536)
            if not chunk:
                if not self._buffer:
                    raise ConnectionClosed("peer closed connection")
                raise ProtocolError("connection closed mid-frame")
            self._buffer += chunk

    def drain(self) -> tuple[list[Frame], bool]:
        """Opportunistic read for multiplexed controllers.

        Reads all currently available bytes without blocking and returns
        (complete frames, eof). The socket itself stays blocking so writes
        remain simple; reads use MSG_DONTWAIT.
        """
        eof = False
        while True:
            try:
                chunk = self._sock.recv(65536, socket.MSG_DONTWAIT)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError:
                eof = True
                break
            if not chunk:
                eof = True
                break
            self._buffer += chunk
        frames: list[Frame] = []
        while True:
            frame = self._parse_one()
            if frame is None:
                break
            frames.append(frame)
        if eof and self._buffer:
            raise ProtocolError("connection closed mid-frame")
        return frames, eof

    def _parse_one(self) -> Frame | None:
        if len(self._buffer) < HEADER_SIZE:
            return None
        mtype, size, sequence = decode_header(
            self._buffer[:HEADER_SIZE], max_payload=self._max_payload
        )
        if len(self._buffer) < HEADER_SIZE + size:
            return None
        if sequence <= self._recv_seq:
            raise ProtocolError(
                f"non-monotonic sequence {sequence} after {self._recv_seq}"
            )
        self._recv_seq = sequence
        body = self._buffer[HEADER_SIZE : HEADER_SIZE + size]
        self._buffer = self._buffer[HEADER_SIZE + size :]
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"invalid JSON payload: {exc}") from None
        if not isinstance(payload, dict):
            raise ProtocolError("payload must be a JSON object")
        return Frame(mtype, sequence, payload)
