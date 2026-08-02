//! Milestone 3 scheduler: FIFO with crash requeue. Duration-aware and
//! fixture-affinity scheduling arrive in Milestones 5 and 6.

const std = @import("std");

pub const Scheduler = struct {
    allocator: std.mem.Allocator,
    /// Requeued (crash-recovered) tests take priority, then FIFO order.
    requeued: std.ArrayList(usize) = .empty,
    next_index: usize = 0,
    total: usize = 0,
    finished: std.ArrayList(bool) = .empty,
    finished_count: usize = 0,
    requeue_counts: std.ArrayList(u8) = .empty,
    max_requeues: u8 = 1,

    pub fn init(allocator: std.mem.Allocator, total: usize) !Scheduler {
        var scheduler: Scheduler = .{ .allocator = allocator, .total = total };
        try scheduler.finished.appendNTimes(allocator, false, total);
        try scheduler.requeue_counts.appendNTimes(allocator, 0, total);
        return scheduler;
    }

    pub fn pop(self: *Scheduler) ?usize {
        if (self.requeued.items.len > 0) {
            return self.requeued.pop();
        }
        while (self.next_index < self.total) {
            const id = self.next_index;
            self.next_index += 1;
            if (!self.finished.items[id]) return id;
        }
        return null;
    }

    pub fn hasPending(self: *const Scheduler) bool {
        return self.requeued.items.len > 0 or self.next_index < self.total;
    }

    /// Returns false when this completion is a duplicate.
    pub fn markFinished(self: *Scheduler, test_id: usize) bool {
        if (self.finished.items[test_id]) return false;
        self.finished.items[test_id] = true;
        self.finished_count += 1;
        return true;
    }

    pub fn allFinished(self: *const Scheduler) bool {
        return self.finished_count == self.total;
    }

    /// Returns true if requeued, false if the attempt limit was reached.
    pub fn requeue(self: *Scheduler, test_id: usize) !bool {
        if (self.finished.items[test_id]) return true;
        if (self.requeue_counts.items[test_id] >= self.max_requeues) return false;
        self.requeue_counts.items[test_id] += 1;
        try self.requeued.append(self.allocator, test_id);
        return true;
    }
};

test "fifo order with requeue priority" {
    var scheduler = try Scheduler.init(std.testing.allocator, 4);
    defer {
        scheduler.finished.deinit(std.testing.allocator);
        scheduler.requeue_counts.deinit(std.testing.allocator);
        scheduler.requeued.deinit(std.testing.allocator);
    }
    try std.testing.expectEqual(@as(?usize, 0), scheduler.pop());
    try std.testing.expectEqual(@as(?usize, 1), scheduler.pop());
    try std.testing.expect(try scheduler.requeue(0));
    try std.testing.expectEqual(@as(?usize, 0), scheduler.pop());
    try std.testing.expectEqual(@as(?usize, 2), scheduler.pop());
    try std.testing.expectEqual(@as(?usize, 3), scheduler.pop());
    try std.testing.expectEqual(@as(?usize, null), scheduler.pop());
    try std.testing.expect(!try scheduler.requeue(0)); // limit reached
    try std.testing.expect(scheduler.markFinished(0));
    try std.testing.expect(!scheduler.markFinished(0)); // duplicate
}
