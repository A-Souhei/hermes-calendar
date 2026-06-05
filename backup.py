#!/usr/bin/env python3
"""Daily calendar DB backup — for a `hermes cron --no-agent` job.

Takes a CONSISTENT snapshot of ``$HERMES_HOME/calendar.db`` using SQLite's
online backup API (safe under WAL — a plain file copy could miss the ``-wal``
contents), gzips it to ``$HERMES_HOME/backups/calendar/calendar-YYYY-MM-DD.db.gz``,
prunes local snapshots older than ``CALENDAR_BACKUP_RETENTION_DAYS`` (default 14),
and — when MinIO is configured — uploads the gzip to an object store bucket.

Prints NOTHING on success (so the cron delivers nothing to chat). Prints a
single line to stdout ONLY on failure, so a broken backup pings you via the
cron's delivery channel instead of failing silently.

MinIO upload is OPTIONAL and additive (the local copy is always kept as a
fallback). Configure it via env (read from ~/.hermes/.env):
    CALENDAR_BACKUP_MINIO_ENDPOINT=100.x.x.x:9000
    CALENDAR_BACKUP_MINIO_ACCESS_KEY=...
    CALENDAR_BACKUP_MINIO_SECRET_KEY=...
    CALENDAR_BACKUP_MINIO_BUCKET=hermes
    CALENDAR_BACKUP_MINIO_SECURE=false        # http (false) vs https (true)
    CALENDAR_BACKUP_MINIO_PREFIX=calendar-backups   # optional object key prefix
If endpoint/keys/bucket are unset, the upload step is skipped silently.

Wire it up (script lives in ~/.hermes/scripts/):
    hermes cron create "30 3 * * *" --name calendar-backup --no-agent \
        --script backup.py
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def _load_env() -> None:
    """Load ~/.hermes/.env so MinIO creds are present regardless of how the cron
    invokes us (mirrors calendar_tick.py)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_hermes_home(), ".env"))
    except Exception:
        pass


def _db_path() -> str:
    return os.path.join(_hermes_home(), "calendar.db")


def _backup_dir() -> str:
    return os.path.join(_hermes_home(), "backups", "calendar")


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("CALENDAR_BACKUP_RETENTION_DAYS", "14")))
    except ValueError:
        return 14


def _snapshot(src_path: str, dest_path: str) -> None:
    """Consistent online backup of ``src_path`` into ``dest_path`` (WAL-safe).

    Uses a generous connect ``timeout`` and a per-step ``sleep`` so transient
    'database is locked' contention from concurrent writers is waited out rather
    than failing the backup.
    """
    src = sqlite3.connect(src_path, timeout=30)
    try:
        dst = sqlite3.connect(dest_path, timeout=30)
        try:
            with dst:
                # pages>0 copies in batches, releasing the lock between steps;
                # sleep waits out a busy writer instead of erroring immediately.
                src.backup(dst, pages=200, sleep=0.25)
        finally:
            dst.close()
    finally:
        src.close()


def _upload(local_path: str, object_name: str) -> str | None:
    """Upload ``local_path`` to the configured MinIO bucket as ``object_name``.

    Returns ``"bucket/object"`` on success, ``None`` when MinIO is not
    configured (upload skipped). Raises on a real upload failure so the caller
    can report it.
    """
    endpoint = (os.environ.get("CALENDAR_BACKUP_MINIO_ENDPOINT") or "").strip()
    access = os.environ.get("CALENDAR_BACKUP_MINIO_ACCESS_KEY")
    secret = os.environ.get("CALENDAR_BACKUP_MINIO_SECRET_KEY")
    bucket = os.environ.get("CALENDAR_BACKUP_MINIO_BUCKET")
    if not (endpoint and access and secret and bucket):
        return None  # not configured -> local-only

    # Accept an endpoint with or without a scheme. A scheme, if present, wins
    # over the SECURE flag (https -> secure, http -> insecure).
    secure = (os.environ.get("CALENDAR_BACKUP_MINIO_SECURE", "false")
              .strip().lower() in ("1", "true", "yes", "on"))
    if endpoint.startswith("https://"):
        endpoint, secure = endpoint[len("https://"):], True
    elif endpoint.startswith("http://"):
        endpoint, secure = endpoint[len("http://"):], False
    endpoint = endpoint.rstrip("/")

    try:
        from minio import Minio
    except ImportError:
        # Configured for upload but the SDK isn't installed — degrade to
        # local-only (README behaviour) while flagging it so it gets noticed.
        print("calendar-backup: minio SDK not installed — upload skipped, "
              "local backup kept (run: pip install minio)")
        return None

    client = Minio(endpoint, access_key=access, secret_key=secret, secure=secure)
    # Best-effort create; ignore if it already exists or we lack create rights
    # (the object PUT below will surface a genuine permission problem).
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except Exception:  # noqa: BLE001
        pass
    client.fput_object(bucket, object_name, local_path, content_type="application/gzip")
    return f"{bucket}/{object_name}"


def _object_name(stamp: str) -> str:
    prefix = (os.environ.get("CALENDAR_BACKUP_MINIO_PREFIX") or "calendar-backups").strip().strip("/")
    base = f"calendar-{stamp}.db.gz"
    return f"{prefix}/{base}" if prefix else base


def _prune(backup_dir: str, retention_days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for name in os.listdir(backup_dir):
        if not (name.startswith("calendar-") and name.endswith(".db.gz")):
            continue
        path = os.path.join(backup_dir, name)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
            if mtime < cutoff:
                os.remove(path)
        except OSError:
            pass


def main() -> int:
    _load_env()
    db = _db_path()
    if not os.path.exists(db):
        print(f"calendar-backup: DB not found at {db}")
        return 1

    bdir = _backup_dir()
    os.makedirs(bdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    final = os.path.join(bdir, f"calendar-{stamp}.db.gz")

    tmp_db = None
    tmp_gz = final + ".part"
    try:
        fd, tmp_db = tempfile.mkstemp(prefix="calbak-", suffix=".db", dir=bdir)
        os.close(fd)
        _snapshot(db, tmp_db)
        # gzip into a .part file, then atomically move into place so a reader
        # never sees a half-written backup.
        with open(tmp_db, "rb") as f_in, gzip.open(tmp_gz, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.replace(tmp_gz, final)
    except Exception as exc:  # noqa: BLE001
        print(f"calendar-backup FAILED: {exc}")
        return 1
    finally:
        # Clean up both temp artifacts; the .part lingers only if we failed
        # before the atomic os.replace() above.
        for leftover in (tmp_db, tmp_gz):
            if leftover and os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

    try:
        _prune(bdir, _retention_days())
    except Exception:  # noqa: BLE001 — pruning failure must not fail the backup
        pass

    # Upload to MinIO (additive — the local copy above is the fallback). A
    # failure here is reported (so the cron pings) but the local backup stands.
    try:
        _upload(final, _object_name(stamp))
    except Exception as exc:  # noqa: BLE001
        print(f"calendar-backup: local OK but MinIO upload FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
