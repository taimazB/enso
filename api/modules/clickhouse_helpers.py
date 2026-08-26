"""Per-thread ClickHouse clients for the API.

A `clickhouse_connect` client carries one server-side session, and ClickHouse
rejects a second query on a session that is already busy:

    ProgrammingError: Attempt to execute concurrent queries within the same
    session. Please use a separate client instance per thread/process.

Every endpoint here is a sync `def`, so FastAPI runs it in the thread pool and
two overlapping requests — a slow monthly image render and the chart's
`/timeseries` POST, say — really do hit the client at the same time. Hence one
client per thread rather than one per process: the pool is bounded (~40 threads)
and each client is little more than a session id over a shared urllib3 pool.
"""

from __future__ import annotations

import threading

from shared.ch import DATABASE, get_client

__all__ = ["DATABASE", "client", "reset"]

_local = threading.local()


def client():
    """This thread's client, opened on first use."""
    existing = getattr(_local, "client", None)
    if existing is not None:
        return existing
    _local.client = get_client()
    return _local.client


def reset() -> None:
    """Drop this thread's client so its next call reconnects.

    Deliberately thread-local: closing another thread's client could pull it out
    from under an in-flight query. Other threads recover the same way, on their
    own next failure.
    """
    existing = getattr(_local, "client", None)
    _local.client = None
    if existing is not None:
        try:
            existing.close()
        except Exception:  # noqa: BLE001 — reset must not raise
            pass
