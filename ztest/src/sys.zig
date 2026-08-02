//! Thin Linux syscall shim. Zig 0.16 moved most I/O behind `std.Io`; ztest
//! is Linux-first (the plan's fast path is Linux prefork), so the controller
//! talks to the kernel directly for its socket event loop.

const std = @import("std");
const linux = std.os.linux;

pub const fd_t = i32;

pub const Error = error{ SysCallFailed, WouldBlock, ConnectionClosed };

fn check(rc: usize) Error!usize {
    const err = linux.errno(rc);
    if (err == .SUCCESS) return rc;
    if (err == .AGAIN) return error.WouldBlock;
    return error.SysCallFailed;
}

pub fn socketUnixStream() Error!fd_t {
    const rc = try check(linux.socket(linux.AF.UNIX, linux.SOCK.STREAM, 0));
    return @intCast(rc);
}

pub fn bindUnix(fd: fd_t, path: []const u8) Error!void {
    var addr: linux.sockaddr.un = .{ .family = linux.AF.UNIX, .path = undefined };
    if (path.len >= addr.path.len) return error.SysCallFailed;
    @memset(&addr.path, 0);
    @memcpy(addr.path[0..path.len], path);
    _ = try check(linux.bind(fd, @ptrCast(&addr), @sizeOf(linux.sockaddr.un)));
}

pub fn listen(fd: fd_t, backlog: u31) Error!void {
    _ = try check(linux.listen(fd, backlog));
}

pub fn accept(fd: fd_t) Error!fd_t {
    const rc = try check(linux.accept4(fd, null, null, 0));
    return @intCast(rc);
}

pub fn read(fd: fd_t, buffer: []u8) Error!usize {
    return try check(linux.read(fd, buffer.ptr, buffer.len));
}

pub fn write(fd: fd_t, bytes: []const u8) Error!usize {
    return try check(linux.write(fd, bytes.ptr, bytes.len));
}

pub fn writeAll(fd: fd_t, bytes: []const u8) Error!void {
    var remaining = bytes;
    while (remaining.len > 0) {
        const n = write(fd, remaining) catch |err| switch (err) {
            error.WouldBlock => continue,
            else => return err,
        };
        remaining = remaining[n..];
    }
}

pub fn close(fd: fd_t) void {
    _ = linux.close(fd);
}

pub fn unlink(path: [:0]const u8) void {
    _ = linux.unlink(path.ptr);
}

pub fn isatty(fd: fd_t) bool {
    var wsz: std.posix.winsize = undefined;
    return linux.ioctl(fd, linux.T.IOCGWINSZ, @intFromPtr(&wsz)) == 0;
}

pub const stdout_fd: fd_t = 1;
pub const stderr_fd: fd_t = 2;
