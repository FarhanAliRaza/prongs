//! Terminal output. Writes through raw fds so it never touches the control
//! protocol, which lives on dedicated sockets.

const std = @import("std");
const report = @import("report.zig");
const sys = @import("sys.zig");

pub const Printer = struct {
    allocator: std.mem.Allocator,
    verbose: bool = false,
    tty: bool,

    pub fn init(allocator: std.mem.Allocator) Printer {
        return .{
            .allocator = allocator,
            .tty = sys.isatty(sys.stdout_fd),
        };
    }

    fn write(self: *const Printer, text: []const u8) void {
        _ = self;
        sys.writeAll(sys.stdout_fd, text) catch {};
    }

    pub fn print(self: *const Printer, comptime fmt: []const u8, args: anytype) void {
        const text = std.fmt.allocPrint(self.allocator, fmt, args) catch return;
        defer self.allocator.free(text);
        self.write(text);
    }

    pub fn progress(self: *const Printer, status: anytype, nodeid: []const u8) void {
        switch (status) {
            .passed => self.write("."),
            .failed => self.print("\nFAILED {s}\n", .{nodeid}),
            .err => self.print("\nERROR {s}\n", .{nodeid}),
            .skipped => self.write("s"),
            .xfailed => self.write("x"),
            .xpassed => self.write("X"),
            else => {},
        }
    }

    pub fn failureDetail(self: *const Printer, nodeid: []const u8, text: []const u8) void {
        self.print("\n{s}\n{s}\n{s}\n{s}\n", .{
            "________________________________________________________________",
            nodeid,
            text,
            "________________________________________________________________",
        });
    }

    pub fn summary(
        self: *const Printer,
        tally: *const report.Tally,
        total: usize,
        workers: u32,
        elapsed_ms: u64,
    ) void {
        self.print(
            "\n===== {d} tests: {d} passed, {d} failed, {d} errors, {d} skipped, " ++
                "{d} xfailed, {d} xpassed [{d} workers, {d}.{d:0>3}s] =====\n",
            .{
                total,          tally.passed,      tally.failed,
                tally.errors,   tally.skipped,     tally.xfailed,
                tally.xpassed,  workers,           elapsed_ms / 1000,
                elapsed_ms % 1000,
            },
        );
    }
};
