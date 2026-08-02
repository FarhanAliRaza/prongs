//! Test manifest received once from the Python host. Both sides speak in
//! dense integer test ids indexing this manifest.

const std = @import("std");

pub const Manifest = struct {
    allocator: std.mem.Allocator,
    nodeids: std.ArrayList([]const u8) = .empty,
    expected_count: usize = 0,
    complete: bool = false,

    pub fn init(allocator: std.mem.Allocator) Manifest {
        return .{ .allocator = allocator };
    }

    pub fn begin(self: *Manifest, expected: usize) void {
        self.expected_count = expected;
    }

    pub fn addItem(self: *Manifest, test_id: usize, id_text: []const u8) !void {
        if (test_id != self.nodeids.items.len) return error.OutOfOrderManifest;
        const copy = try self.allocator.dupe(u8, id_text);
        try self.nodeids.append(self.allocator, copy);
    }

    pub fn end(self: *Manifest) !void {
        if (self.nodeids.items.len != self.expected_count) return error.ManifestCountMismatch;
        self.complete = true;
    }

    pub fn count(self: *const Manifest) usize {
        return self.nodeids.items.len;
    }

    pub fn nodeid(self: *const Manifest, test_id: usize) []const u8 {
        return self.nodeids.items[test_id];
    }
};
