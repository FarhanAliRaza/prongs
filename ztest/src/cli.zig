//! `ztest` command-line parsing. Everything after `--` is preserved verbatim
//! for pytest.

const std = @import("std");
const sys = @import("sys.zig");

pub const Command = enum { run };

pub const Options = struct {
    command: Command = .run,
    jobs: u32,
    python: []const u8 = "python3",
    pytest_args: []const []const u8 = &.{},
};

pub const UsageError = error{InvalidArgs};

pub fn usage(writer_fd: sys.fd_t) void {
    const text =
        \\usage: ztest run [-j N|auto] [--python EXE] -- <pytest args>
        \\
        \\  -j, --jobs      worker count, or "auto" for CPU count (default auto)
        \\  --python EXE    Python interpreter to launch (default python3)
        \\
        \\Everything after `--` is passed to pytest unchanged.
        \\
    ;
    sys.writeAll(writer_fd, text) catch {};
}

pub fn parse(allocator: std.mem.Allocator, argv: []const []const u8) UsageError!Options {
    if (argv.len < 2 or !std.mem.eql(u8, argv[1], "run")) return error.InvalidArgs;

    var options: Options = .{ .jobs = cpuCount() };
    var pytest_args: std.ArrayList([]const u8) = .empty;

    var i: usize = 2;
    while (i < argv.len) : (i += 1) {
        const arg = argv[i];
        if (std.mem.eql(u8, arg, "--")) {
            for (argv[i + 1 ..]) |rest| {
                pytest_args.append(allocator, rest) catch return error.InvalidArgs;
            }
            break;
        } else if (std.mem.eql(u8, arg, "-j") or std.mem.eql(u8, arg, "--jobs")) {
            i += 1;
            if (i >= argv.len) return error.InvalidArgs;
            options.jobs = parseJobs(argv[i]) orelse return error.InvalidArgs;
        } else if (std.mem.eql(u8, arg, "--python")) {
            i += 1;
            if (i >= argv.len) return error.InvalidArgs;
            options.python = argv[i];
        } else {
            return error.InvalidArgs;
        }
    }
    options.pytest_args = pytest_args.items;
    return options;
}

fn parseJobs(value: []const u8) ?u32 {
    if (std.mem.eql(u8, value, "auto")) return cpuCount();
    const n = std.fmt.parseInt(u32, value, 10) catch return null;
    if (n == 0) return null;
    return n;
}

fn cpuCount() u32 {
    const count = std.Thread.getCpuCount() catch return 2;
    return @intCast(@max(count, 1));
}
