//! Outcome tallying from streamed TEST_REPORT phases, mirroring pytest's
//! summary semantics.

const std = @import("std");

pub const Phase = enum { setup, call, teardown };

pub const Tally = struct {
    passed: usize = 0,
    failed: usize = 0,
    skipped: usize = 0,
    xfailed: usize = 0,
    xpassed: usize = 0,
    errors: usize = 0,

    pub fn record(
        self: *Tally,
        phase: Phase,
        outcome: []const u8,
        wasxfail: bool,
    ) enum { none, passed, failed, skipped, xfailed, xpassed, err } {
        const failed = std.mem.eql(u8, outcome, "failed");
        const skipped = std.mem.eql(u8, outcome, "skipped");
        const passed = std.mem.eql(u8, outcome, "passed");
        switch (phase) {
            .setup => {
                if (failed) {
                    self.errors += 1;
                    return .err;
                }
                if (skipped) {
                    if (wasxfail) {
                        self.xfailed += 1;
                        return .xfailed;
                    }
                    self.skipped += 1;
                    return .skipped;
                }
            },
            .call => {
                if (failed) {
                    self.failed += 1;
                    return .failed;
                }
                if (skipped) {
                    if (wasxfail) {
                        self.xfailed += 1;
                        return .xfailed;
                    }
                    self.skipped += 1;
                    return .skipped;
                }
                if (passed) {
                    if (wasxfail) {
                        self.xpassed += 1;
                        return .xpassed;
                    }
                    self.passed += 1;
                    return .passed;
                }
            },
            .teardown => {
                if (failed) {
                    self.errors += 1;
                    return .err;
                }
            },
        }
        return .none;
    }

    pub fn exitCode(self: *const Tally, internal_error: bool) u8 {
        if (internal_error) return 3;
        if (self.failed > 0 or self.errors > 0) return 1;
        return 0;
    }
};

test "tally maps pytest outcomes" {
    var tally: Tally = .{};
    _ = tally.record(.setup, "passed", false);
    _ = tally.record(.call, "passed", false);
    _ = tally.record(.teardown, "passed", false);
    _ = tally.record(.call, "failed", false);
    _ = tally.record(.setup, "failed", false);
    _ = tally.record(.call, "skipped", true);
    _ = tally.record(.call, "passed", true);
    _ = tally.record(.setup, "skipped", false);
    try std.testing.expectEqual(@as(usize, 1), tally.passed);
    try std.testing.expectEqual(@as(usize, 1), tally.failed);
    try std.testing.expectEqual(@as(usize, 1), tally.errors);
    try std.testing.expectEqual(@as(usize, 1), tally.xfailed);
    try std.testing.expectEqual(@as(usize, 1), tally.xpassed);
    try std.testing.expectEqual(@as(usize, 1), tally.skipped);
    try std.testing.expectEqual(@as(u8, 1), tally.exitCode(false));
}
