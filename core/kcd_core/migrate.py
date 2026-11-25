"""Minimal, safe SQL migration runner: `python -m kcd_core.migrate`.

- files are `NNN_name.sql`, applied in order, each in its own transaction
- applied files are recorded with a checksum; editing an applied file is an error (add a new migration instead)
- a Postgres advisory lock makes concurrent starts (several replicas, a one-shot job) safe
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import psycopg

log = logging.getLogger(__name__)

_NAME = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")
_LOCK_KEY = 0x4B43444D  # 'KCDM'


class MigrationError(RuntimeError):
    pass


def discover(directory: Path) -> list[Path]:
    files = sorted(p for p in directory.glob("*.sql"))
    bad = [p.name for p in files if not _NAME.match(p.name)]
    if bad:
        raise MigrationError(f"migration files must be named NNN_name.sql: {bad}")
    return files


def apply_migrations(dsn: str, directory: Path, *, options: str = "") -> list[str]:
    """Apply pending migrations. Returns the versions applied by this call."""
    files = discover(directory)
    applied_now: list[str] = []
    kwargs: dict[str, Any] = {"connect_timeout": 10, "application_name": "kcd-migrate"}
    if options:
        kwargs["options"] = options
    with psycopg.connect(dsn, autocommit=True, **kwargs) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migration ("
                " version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done: dict[str, str] = dict(conn.execute("SELECT version, checksum FROM schema_migration").fetchall())
            for path in files:
                text = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(text.encode()).hexdigest()
                if path.stem in done:
                    if done[path.stem] != checksum:
                        raise MigrationError(
                            f"{path.name} was edited after it was applied; add a new migration instead"
                        )
                    continue
                log.info("applying %s", path.name)
                with conn.transaction():
                    conn.execute(text.encode())  # no parameters: psycopg runs the whole script
                    conn.execute(
                        "INSERT INTO schema_migration (version, checksum) VALUES (%s, %s)", (path.stem, checksum)
                    )
                applied_now.append(path.stem)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dsn = os.environ.get("KCD_DATABASE_URL")
    if not dsn:
        print("KCD_DATABASE_URL is required", file=sys.stderr)
        return 2
    directory = Path(os.environ.get("KCD_MIGRATIONS_DIR", "infra/migrations"))
    try:
        applied = apply_migrations(dsn, directory)
    except (MigrationError, psycopg.Error) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'nothing to do'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
