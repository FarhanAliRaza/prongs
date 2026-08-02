"""ztest — parallel-first pytest-compatible runner.

Zig owns workers, scheduling, timeouts and aggregation; real pytest owns
configuration, plugins, assertion rewriting, collection, fixtures and
execution. This package is the Python side: the pytest host, the prefork
worker, and the wire protocol.
"""

__version__ = "0.1.0"
