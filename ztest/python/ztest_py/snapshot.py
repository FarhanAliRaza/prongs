"""Milestone 7 (not yet implemented): the clean snapshot server.

Planned topology after collection:

    Python host
        ├── parent continues as report/plugin host
        └── child becomes clean snapshot server
                ├── forks worker 1..N
                └── forks replacements after crashes

The snapshot server never runs tests, stays single-threaded, and can create
replacement workers without inheriting reporting-plugin mutations. Until
this lands, replacement workers are forked directly from the host (which is
kept out of test execution, so its state stays close to post-collection).
"""
