//! Milestone 3 controller: single-threaded poll loop that owns the control
//! socket, the Python host, worker scheduling with one-test lookahead, and
//! result aggregation.

const std = @import("std");
const protocol = @import("protocol.zig");
const manifest_mod = @import("manifest.zig");
const scheduler_mod = @import("scheduler.zig");
const report_mod = @import("report.zig");
const output = @import("output.zig");
const worker_mod = @import("worker.zig");
const process_linux = @import("process_linux.zig");
const sys = @import("sys.zig");

const Connection = worker_mod.Connection;

pub var interrupted: bool = false;

pub fn onSigint(_: std.posix.SIG) callconv(.c) void {
    interrupted = true;
}

pub const Controller = struct {
    allocator: std.mem.Allocator,
    printer: *const output.Printer,
    listener: sys.fd_t,
    host: *process_linux.Host,
    jobs: u32,

    manifest: manifest_mod.Manifest,
    scheduler: ?scheduler_mod.Scheduler = null,
    tally: report_mod.Tally = .{},
    connections: std.ArrayList(*Connection) = .empty,
    host_conn: ?*Connection = null,
    next_worker_id: i64 = 0,
    workers_alive: u32 = 0,
    internal_error: bool = false,
    started_ms: u64 = 0,
    /// The initial per-worker queue depth. Two is the minimum that gives
    /// every worker a live `nextitem` for fixture-teardown correctness.
    initial_batch: usize = 2,

    pub fn init(
        allocator: std.mem.Allocator,
        printer: *const output.Printer,
        listener: sys.fd_t,
        host: *process_linux.Host,
        jobs: u32,
    ) Controller {
        return .{
            .allocator = allocator,
            .printer = printer,
            .listener = listener,
            .host = host,
            .jobs = jobs,
            .manifest = manifest_mod.Manifest.init(allocator),
            .started_ms = process_linux.monotonicMs(),
        };
    }

    pub fn run(self: *Controller) u8 {
        self.loop() catch |err| {
            self.printer.print("ztest: internal error: {s}\n", .{@errorName(err)});
            self.internal_error = true;
        };
        self.shutdown();
        if (interrupted) return 130;
        if (self.manifest.complete and self.manifest.count() == 0) return 5;
        if (self.scheduler == null) return 3;
        const elapsed: u64 = process_linux.monotonicMs() - self.started_ms;
        self.printer.summary(&self.tally, self.manifest.count(), self.jobs, elapsed);
        return self.tally.exitCode(self.internal_error);
    }

    fn loop(self: *Controller) !void {
        while (!interrupted) {
            if (self.scheduler) |*scheduler| {
                if (scheduler.allFinished()) return;
                if (self.manifest.count() == 0) return;
            }

            var pollfds: std.ArrayList(std.posix.pollfd) = .empty;
            defer pollfds.deinit(self.allocator);
            try pollfds.append(self.allocator, .{
                .fd = self.listener,
                .events = std.posix.POLL.IN,
                .revents = 0,
            });
            for (self.connections.items) |conn| {
                try pollfds.append(self.allocator, .{
                    .fd = conn.fd,
                    .events = std.posix.POLL.IN,
                    .revents = 0,
                });
            }

            const ready = std.posix.poll(pollfds.items, 1000) catch continue;
            if (ready == 0) continue;

            if (pollfds.items[0].revents & std.posix.POLL.IN != 0) {
                try self.acceptConnection();
            }

            // Snapshot: handlers may close/remove connections.
            var index: usize = 1;
            while (index < pollfds.items.len) : (index += 1) {
                const pfd = pollfds.items[index];
                if (pfd.revents == 0) continue;
                const conn = self.findConnection(pfd.fd) orelse continue;
                self.serviceConnection(conn) catch |err| {
                    self.dropConnection(conn, @errorName(err));
                };
            }
        }
    }

    fn acceptConnection(self: *Controller) !void {
        const fd = sys.accept(self.listener) catch return;
        const conn = try self.allocator.create(Connection);
        conn.* = Connection.init(self.allocator, fd);
        try self.connections.append(self.allocator, conn);
    }

    fn findConnection(self: *Controller, fd: sys.fd_t) ?*Connection {
        for (self.connections.items) |conn| {
            if (conn.fd == fd) return conn;
        }
        return null;
    }

    fn serviceConnection(self: *Controller, conn: *Connection) !void {
        const open = try conn.fill();
        while (!conn.pending_drop) {
            const frame = (try conn.nextFrame()) orelse break;
            defer self.allocator.free(frame.payload);
            try self.handleFrame(conn, frame);
        }
        if (!open or conn.pending_drop) self.dropConnection(conn, "EOF");
    }

    /// Idempotent: safe to call with a pointer that has already been
    /// dropped (it is matched against the live list by identity, never
    /// dereferenced first). Destroys the connection.
    fn dropConnection(self: *Controller, conn: *Connection, reason: []const u8) void {
        var live = false;
        for (self.connections.items) |candidate| {
            if (candidate == conn) {
                live = true;
                break;
            }
        }
        if (!live) return;

        if (conn.role == .worker and conn.outstanding.items.len > 0) {
            // Recover tests that died with the worker.
            if (self.scheduler) |*scheduler| {
                for (conn.outstanding.items) |test_id| {
                    const requeued = scheduler.requeue(test_id) catch false;
                    if (requeued) continue;
                    // Attempt limit reached: record as a crash failure.
                    if (scheduler.markFinished(test_id)) {
                        self.tally.failed += 1;
                        self.printer.print(
                            "\nCRASHED {s} (worker {d}: {s})\n",
                            .{ self.manifest.nodeid(test_id), conn.worker_id, reason },
                        );
                    }
                }
                // Replace the dead worker while work remains.
                if (scheduler.hasPending()) {
                    self.spawnWorker() catch {
                        self.internal_error = true;
                    };
                }
            }
        }
        if (conn.role == .worker and self.workers_alive > 0) self.workers_alive -= 1;
        if (conn.role == .host) {
            self.host_conn = null;
            if (self.scheduler == null or !self.scheduler.?.allFinished()) {
                // Host died before the run completed.
                self.internal_error = true;
            }
        }
        for (self.connections.items, 0..) |candidate, i| {
            if (candidate == conn) {
                _ = self.connections.swapRemove(i);
                break;
            }
        }
        conn.deinit();
        self.allocator.destroy(conn);
    }

    fn handleFrame(self: *Controller, conn: *Connection, frame: worker_mod.FrameEvent) !void {
        var parsed = std.json.parseFromSlice(
            std.json.Value,
            self.allocator,
            if (frame.payload.len == 0) "{}" else frame.payload,
            .{},
        ) catch return error.InvalidPayload;
        defer parsed.deinit();
        const payload = parsed.value;

        switch (frame.message_type) {
            .hello => {
                const role = getString(payload, "role") orelse return error.InvalidPayload;
                if (std.mem.eql(u8, role, "host")) {
                    conn.role = .host;
                    self.host_conn = conn;
                }
            },
            .manifest_begin => {
                const count = getInt(payload, "count") orelse return error.InvalidPayload;
                self.manifest.begin(@intCast(count));
            },
            .manifest_item => {
                const test_id = getInt(payload, "test_id") orelse return error.InvalidPayload;
                const nodeid = getString(payload, "nodeid") orelse return error.InvalidPayload;
                try self.manifest.addItem(@intCast(test_id), nodeid);
            },
            .manifest_end => {
                try self.manifest.end();
            },
            .host_ready => {
                try self.startWorkers();
            },
            .worker_ready => {
                conn.role = .worker;
                conn.worker_id = getInt(payload, "worker_id") orelse -1;
                self.workers_alive += 1;
                try self.assign(conn, self.initial_batch);
            },
            .test_started => {},
            .test_report => try self.handleReport(payload),
            .test_finished => {
                const test_id_signed = getInt(payload, "test_id") orelse return error.InvalidPayload;
                const test_id: usize = @intCast(test_id_signed);
                var scheduler = &(self.scheduler orelse return error.ManifestNotReady);
                if (!scheduler.markFinished(test_id)) {
                    // Duplicate completion events are rejected.
                    return error.DuplicateCompletion;
                }
                conn.removeOutstanding(test_id);
                try self.assign(conn, 1);
            },
            .goodbye => {
                conn.outstanding.clearRetainingCapacity();
                conn.pending_drop = true;
            },
            .heartbeat => {},
            else => return error.UnexpectedMessage,
        }
    }

    fn handleReport(self: *Controller, payload: std.json.Value) !void {
        const phase_name = getString(payload, "phase") orelse return error.InvalidPayload;
        const outcome = getString(payload, "outcome") orelse return error.InvalidPayload;
        const nodeid = getString(payload, "nodeid") orelse "<unknown>";
        const wasxfail = getBool(payload, "wasxfail") orelse false;
        const phase: report_mod.Phase = if (std.mem.eql(u8, phase_name, "setup"))
            .setup
        else if (std.mem.eql(u8, phase_name, "call"))
            .call
        else
            .teardown;
        const status = self.tally.record(phase, outcome, wasxfail);
        self.printer.progress(status, nodeid);
        if (status == .failed or status == .err) {
            if (getString(payload, "longrepr_text")) |text| {
                self.printer.failureDetail(nodeid, text);
            }
        }
    }

    fn startWorkers(self: *Controller) !void {
        if (!self.manifest.complete) return error.ManifestNotReady;
        const total = self.manifest.count();
        self.scheduler = try scheduler_mod.Scheduler.init(self.allocator, total);
        if (total == 0) return;
        const worker_count: u32 = @intCast(@min(@as(usize, self.jobs), total));
        var i: u32 = 0;
        while (i < worker_count) : (i += 1) {
            try self.spawnWorker();
        }
    }

    fn spawnWorker(self: *Controller) !void {
        const host_conn = self.host_conn orelse return error.HostGone;
        const payload = try std.fmt.allocPrint(
            self.allocator,
            "{{\"worker_id\":{d}}}",
            .{self.next_worker_id},
        );
        defer self.allocator.free(payload);
        self.next_worker_id += 1;
        try host_conn.sendFrame(.spawn_worker, payload);
    }

    fn assign(self: *Controller, conn: *Connection, count: usize) !void {
        var scheduler = &(self.scheduler orelse return error.ManifestNotReady);
        var ids: std.ArrayList(usize) = .empty;
        defer ids.deinit(self.allocator);
        while (ids.items.len < count) {
            const id = scheduler.pop() orelse break;
            try ids.append(self.allocator, id);
        }
        if (ids.items.len > 0) {
            var json: std.ArrayList(u8) = .empty;
            defer json.deinit(self.allocator);
            try json.appendSlice(self.allocator, "{\"test_ids\":[");
            for (ids.items, 0..) |id, i| {
                if (i > 0) try json.append(self.allocator, ',');
                const num = try std.fmt.allocPrint(self.allocator, "{d}", .{id});
                defer self.allocator.free(num);
                try json.appendSlice(self.allocator, num);
                try conn.outstanding.append(self.allocator, id);
            }
            try json.appendSlice(self.allocator, "]}");
            try conn.sendFrame(.assign_tests, json.items);
        }
        if (!scheduler.hasPending() and !conn.sent_no_more) {
            conn.sent_no_more = true;
            try conn.sendFrame(.no_more_tests, "{}");
        }
    }

    fn shutdown(self: *Controller) void {
        for (self.connections.items) |conn| {
            conn.sendFrame(.shutdown, "{}") catch {};
        }
        sys.close(self.listener);

        if (interrupted) {
            self.host.kill();
        }
        _ = self.host.wait() catch {};

        for (self.connections.items) |conn| {
            conn.deinit();
            self.allocator.destroy(conn);
        }
        self.connections.deinit(self.allocator);
    }
};

fn getString(value: std.json.Value, key: []const u8) ?[]const u8 {
    const entry = value.object.get(key) orelse return null;
    return switch (entry) {
        .string => |s| s,
        else => null,
    };
}

fn getInt(value: std.json.Value, key: []const u8) ?i64 {
    const entry = value.object.get(key) orelse return null;
    return switch (entry) {
        .integer => |n| n,
        else => null,
    };
}

fn getBool(value: std.json.Value, key: []const u8) ?bool {
    const entry = value.object.get(key) orelse return null;
    return switch (entry) {
        .bool => |b| b,
        else => null,
    };
}
