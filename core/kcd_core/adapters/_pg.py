"""Shared Postgres plumbing: pool construction and JSON hygiene."""

from __future__ import annotations

from typing import Any

from psycopg_pool import ConnectionPool


def make_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    options: str = "",
    name: str = "kcd-core",
    timeout_s: float = 5.0,
    statement_timeout_ms: int = 5000,
) -> ConnectionPool:
    """Pool with the settings every Core process must have.

    - session time zone is UTC (the audit hash chain serialises timestamps; a drifting zone would break it)
    - statement and connect timeouts, so a stuck database fails calls instead of freezing the event loop
    - dead connections are detected before they are handed out
    """
    pg_options = f"-c timezone=UTC -c statement_timeout={statement_timeout_ms} {options}".strip()
    pool = ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout_s,
        name=name,
        open=False,
        check=ConnectionPool.check_connection,
        kwargs={"connect_timeout": int(timeout_s), "application_name": name, "options": pg_options},
    )
    pool.open(wait=True, timeout=timeout_s * 2)
    return pool


def clean_json(value: Any) -> Any:
    """Replace NUL characters, which Postgres JSONB (and TEXT) cannot store, so a hostile patient message
    cannot turn into a poison pill that fails every retry."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {clean_json(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [clean_json(v) for v in value]
    return value
