//! Linux process management: the Unix control socket and the Python host
//! child process.

const std = @import("std");
const sys = @import("sys.zig");

pub fn monotonicMs() u64 {
    var ts: std.os.linux.timespec = undefined;
    _ = std.os.linux.clock_gettime(.MONOTONIC, &ts);
    return @as(u64, @intCast(ts.sec)) * 1000 + @as(u64, @intCast(ts.nsec)) / 1_000_000;
}

pub fn makeSocketPath(allocator: std.mem.Allocator) ![:0]const u8 {
    const pid = std.os.linux.getpid();
    return std.fmt.allocPrintSentinel(allocator, "/tmp/ztest-{d}-{d}.sock", .{ pid, monotonicMs() }, 0);
}

pub fn listenUnix(path: []const u8) !sys.fd_t {
    const fd = try sys.socketUnixStream();
    errdefer sys.close(fd);
    try sys.bindUnix(fd, path);
    try sys.listen(fd, 64);
    return fd;
}

pub const Host = struct {
    child: std.process.Child,
    io: std.Io,

    pub fn spawn(
        allocator: std.mem.Allocator,
        io: std.Io,
        python: []const u8,
        socket_path: []const u8,
        pytest_args: []const []const u8,
    ) !Host {
        var argv: std.ArrayList([]const u8) = .empty;
        try argv.appendSlice(allocator, &.{
            python, "-m", "ztest_py", "host", "--socket", socket_path, "--",
        });
        try argv.appendSlice(allocator, pytest_args);

        const child = try std.process.spawn(io, .{
            .argv = argv.items,
            .stdin = .ignore,
            .stdout = .inherit,
            .stderr = .inherit,
        });
        return .{ .child = child, .io = io };
    }

    pub fn wait(self: *Host) !u32 {
        const term = try self.child.wait(self.io);
        return switch (term) {
            .exited => |code| code,
            else => 128,
        };
    }

    pub fn kill(self: *Host) void {
        self.child.kill(self.io);
    }
};
