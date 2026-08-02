//! Milestone 5 (not yet implemented): SQLite test history.
//!
//! Plan: vendor the SQLite amalgamation, compile it through build.zig, and
//! make this module the only database writer — batched attempt inserts,
//! EMA/p90 duration stats feeding the duration-aware scheduler. Workers
//! never touch the database.
