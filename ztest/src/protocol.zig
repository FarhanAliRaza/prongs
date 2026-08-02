//! Frame protocol, Zig side (Milestone 2). Mirrors python/ztest_py/protocol.py.
//!
//! Frame layout (little-endian):
//!   magic         u32   "ZTST"
//!   version       u16
//!   message_type  u16
//!   payload_size  u32
//!   sequence      u64
//!   payload       bytes (UTF-8 JSON object)

const std = @import("std");

pub const magic = [4]u8{ 'Z', 'T', 'S', 'T' };
pub const version: u16 = 1;
pub const header_size: usize = 20;
pub const default_max_payload: u32 = 64 * 1024 * 1024;

pub const MessageType = enum(u16) {
    hello = 1,
    host_ready = 2,

    manifest_begin = 3,
    manifest_item = 4,
    manifest_end = 5,

    spawn_worker = 6,
    worker_ready = 7,
    worker_exited = 8,

    assign_tests = 9,
    no_more_tests = 10,
    cancel_test = 11,
    shutdown = 12,

    test_started = 13,
    test_report = 14,
    test_finished = 15,
    output_chunk = 16,

    heartbeat = 17,
    protocol_error = 18,
    goodbye = 19,
};

pub const ProtocolError = error{
    BadMagic,
    UnsupportedVersion,
    UnknownMessageType,
    PayloadTooLarge,
    NonMonotonicSequence,
};

pub const Header = struct {
    message_type: MessageType,
    payload_size: u32,
    sequence: u64,
};

pub fn decodeHeader(bytes: *const [header_size]u8, max_payload: u32) ProtocolError!Header {
    if (!std.mem.eql(u8, bytes[0..4], &magic)) return error.BadMagic;
    const ver = std.mem.readInt(u16, bytes[4..6], .little);
    if (ver != version) return error.UnsupportedVersion;
    const raw_type = std.mem.readInt(u16, bytes[6..8], .little);
    const size = std.mem.readInt(u32, bytes[8..12], .little);
    const sequence = std.mem.readInt(u64, bytes[12..20], .little);
    if (size > max_payload) return error.PayloadTooLarge;
    const message_type = std.enums.fromInt(MessageType, raw_type) orelse
        return error.UnknownMessageType;
    return .{ .message_type = message_type, .payload_size = size, .sequence = sequence };
}

pub fn encodeHeader(message_type: MessageType, payload_size: u32, sequence: u64) [header_size]u8 {
    var out: [header_size]u8 = undefined;
    @memcpy(out[0..4], &magic);
    std.mem.writeInt(u16, out[4..6], version, .little);
    std.mem.writeInt(u16, out[6..8], @intFromEnum(message_type), .little);
    std.mem.writeInt(u32, out[8..12], payload_size, .little);
    std.mem.writeInt(u64, out[12..20], sequence, .little);
    return out;
}

test "header roundtrip" {
    const header = encodeHeader(.assign_tests, 42, 7);
    const decoded = try decodeHeader(&header, default_max_payload);
    try std.testing.expectEqual(MessageType.assign_tests, decoded.message_type);
    try std.testing.expectEqual(@as(u32, 42), decoded.payload_size);
    try std.testing.expectEqual(@as(u64, 7), decoded.sequence);
}

test "bad magic rejected" {
    var header = encodeHeader(.hello, 0, 0);
    header[0] = 'X';
    try std.testing.expectError(error.BadMagic, decodeHeader(&header, default_max_payload));
}

test "unknown version rejected" {
    var header = encodeHeader(.hello, 0, 0);
    std.mem.writeInt(u16, header[4..6], version + 1, .little);
    try std.testing.expectError(error.UnsupportedVersion, decodeHeader(&header, default_max_payload));
}

test "unknown message type rejected" {
    var header = encodeHeader(.hello, 0, 0);
    std.mem.writeInt(u16, header[6..8], 9999, .little);
    try std.testing.expectError(error.UnknownMessageType, decodeHeader(&header, default_max_payload));
}

test "payload limit enforced" {
    const header = encodeHeader(.output_chunk, 1000, 0);
    try std.testing.expectError(error.PayloadTooLarge, decodeHeader(&header, 100));
}
