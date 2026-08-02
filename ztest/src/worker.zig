//! Per-connection state for the controller: framed reads with buffering,
//! sequence checking, and worker bookkeeping (outstanding assignments,
//! lookahead top-up).

const std = @import("std");
const protocol = @import("protocol.zig");
const sys = @import("sys.zig");

pub const Role = enum { unknown, host, worker };

pub const FrameEvent = struct {
    message_type: protocol.MessageType,
    sequence: u64,
    payload: []const u8,
};

pub const Connection = struct {
    allocator: std.mem.Allocator,
    fd: sys.fd_t,
    role: Role = .unknown,
    worker_id: i64 = -1,
    buffer: std.ArrayList(u8) = .empty,
    send_seq: u64 = 0,
    recv_seq: i128 = -1,
    /// Test ids assigned but not yet finished (bounded: lookahead keeps this
    /// at ~2). Needed for crash recovery.
    outstanding: std.ArrayList(usize) = .empty,
    sent_no_more: bool = false,
    /// Set by frame handlers (e.g. GOODBYE) to request teardown once the
    /// current frame batch finishes; the event loop owns actual destruction.
    pending_drop: bool = false,
    closed: bool = false,

    pub fn init(allocator: std.mem.Allocator, fd: sys.fd_t) Connection {
        return .{ .allocator = allocator, .fd = fd };
    }

    pub fn deinit(self: *Connection) void {
        self.buffer.deinit(self.allocator);
        self.outstanding.deinit(self.allocator);
        if (!self.closed) {
            sys.close(self.fd);
            self.closed = true;
        }
    }

    pub fn sendFrame(self: *Connection, message_type: protocol.MessageType, payload: []const u8) !void {
        const header = protocol.encodeHeader(message_type, @intCast(payload.len), self.send_seq);
        self.send_seq += 1;
        try self.writeAll(&header);
        try self.writeAll(payload);
    }

    fn writeAll(self: *Connection, bytes: []const u8) !void {
        try sys.writeAll(self.fd, bytes);
    }

    /// Read whatever is available; returns false on EOF.
    pub fn fill(self: *Connection) !bool {
        var chunk: [65536]u8 = undefined;
        const n = sys.read(self.fd, &chunk) catch |err| switch (err) {
            error.WouldBlock => return true,
            else => return err,
        };
        if (n == 0) return false;
        try self.buffer.appendSlice(self.allocator, chunk[0..n]);
        return true;
    }

    /// Parse one complete frame out of the buffer, if available. The
    /// returned payload slice is owned by the caller (duped) so the buffer
    /// can compact.
    pub fn nextFrame(self: *Connection) !?FrameEvent {
        const items = self.buffer.items;
        if (items.len < protocol.header_size) return null;
        const header = try protocol.decodeHeader(
            items[0..protocol.header_size],
            protocol.default_max_payload,
        );
        const total = protocol.header_size + header.payload_size;
        if (items.len < total) return null;
        if (header.sequence <= self.recv_seq) return error.NonMonotonicSequence;
        self.recv_seq = header.sequence;
        const payload = try self.allocator.dupe(u8, items[protocol.header_size..total]);
        const remaining = items.len - total;
        std.mem.copyForwards(u8, items[0..remaining], items[total..]);
        self.buffer.shrinkRetainingCapacity(remaining);
        return .{
            .message_type = header.message_type,
            .sequence = header.sequence,
            .payload = payload,
        };
    }

    pub fn removeOutstanding(self: *Connection, test_id: usize) void {
        for (self.outstanding.items, 0..) |id, i| {
            if (id == test_id) {
                _ = self.outstanding.swapRemove(i);
                return;
            }
        }
    }
};
