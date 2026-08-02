//! ztest — parallel-first pytest-compatible runner (Milestone 3 slice).
//!
//!     ztest run -j auto -- tests/ -q
//!
//! Zig owns workers, scheduling and aggregation; a Python host owns pytest
//! configuration, collection and execution.

const std = @import("std");
const cli = @import("cli.zig");
const controller_mod = @import("controller.zig");
const output = @import("output.zig");
const process_linux = @import("process_linux.zig");
const sys = @import("sys.zig");

comptime {
    // Pull in unit-testable modules.
    _ = @import("protocol.zig");
    _ = @import("scheduler.zig");
    _ = @import("report.zig");
}

pub fn main(init: std.process.Init) u8 {
    const allocator = init.arena.allocator();
    const io = init.io;

    const raw_args = init.minimal.args.toSlice(allocator) catch return 3;
    const argv = allocator.alloc([]const u8, raw_args.len) catch return 3;
    for (raw_args, 0..) |arg, i| argv[i] = arg;

    const options = cli.parse(allocator, argv) catch {
        cli.usage(sys.stderr_fd);
        return 2;
    };

    const printer = output.Printer.init(allocator);

    const socket_path = process_linux.makeSocketPath(allocator) catch return 3;
    defer sys.unlink(socket_path);

    const listener = process_linux.listenUnix(socket_path) catch |err| {
        printer.print("ztest: cannot create control socket: {s}\n", .{@errorName(err)});
        return 3;
    };

    var host = process_linux.Host.spawn(
        allocator,
        io,
        options.python,
        socket_path,
        options.pytest_args,
    ) catch |err| {
        printer.print("ztest: cannot start Python host: {s}\n", .{@errorName(err)});
        return 3;
    };

    var act: std.posix.Sigaction = .{
        .handler = .{ .handler = controller_mod.onSigint },
        .mask = std.posix.sigemptyset(),
        .flags = 0,
    };
    std.posix.sigaction(std.posix.SIG.INT, &act, null);

    var controller = controller_mod.Controller.init(
        allocator,
        &printer,
        listener,
        &host,
        options.jobs,
    );
    return controller.run();
}
