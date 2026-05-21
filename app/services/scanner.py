"""
Core directory scanner.

Design principles:
- Streams incrementally; never holds all file objects in memory.
- Commits every BATCH_SIZE files so SQLite write-lock is never held long.
- Updates scan.total_files/bytes after each commit so the web UI shows live progress.
- Respects filesystem boundaries (--one-filesystem / xdev).
- Handles permissions errors, disappeared files, broken symlinks gracefully.
"""
import fnmatch
import logging
import os
import pwd
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Directory, File, Mount, Scan, ScanError

logger = logging.getLogger(__name__)

BATCH_SIZE = 500        # rows per INSERT
PROGRESS_INTERVAL = 10  # seconds between scan-record progress updates
COLD_BUCKETS = [30, 90, 180]  # days


def _cold_bucket(atime: float, now_ts: float, cold_days: int) -> int | None:
    age_days = (now_ts - atime) / 86400
    if age_days < cold_days:
        return None
    for b in sorted(COLD_BUCKETS, reverse=True):
        if age_days >= b:
            return b
    return None


def _should_exclude(path: str, excludes: list[str], globs: list[str]) -> bool:
    for ex in excludes:
        if path == ex or path.startswith(ex + "/"):
            return True
    name = os.path.basename(path)
    for g in globs:
        if fnmatch.fnmatch(name, g) or fnmatch.fnmatch(path, g):
            return True
    return False


def _walk_one_fs(
    root: str,
    root_dev: int,
    one_filesystem: bool,
    excludes: list[str],
    globs: list[str],
) -> Iterator[tuple[str, os.stat_result | None, bool]]:
    """
    Yield (path, stat_result, is_symlink) for every file under root.
    stat_result is None when an error occurred (error sentinel).
    Directories crossing filesystem boundaries or matching excludes are skipped.
    """
    stack = [root]
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    path = entry.path
                    try:
                        is_link = entry.is_symlink()
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        yield path, None, False
                        logger.debug("stat failed %s: %s", path, e)
                        continue

                    if _should_exclude(path, excludes, globs):
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        if one_filesystem and st.st_dev != root_dev:
                            logger.debug("Skipping cross-fs dir %s", path)
                            continue
                        stack.append(path)
                    else:
                        yield path, st, is_link
        except PermissionError as e:
            yield current_dir, None, False
            logger.debug("Permission denied: %s — %s", current_dir, e)
        except OSError as e:
            yield current_dir, None, False
            logger.debug("OSError scanning %s: %s", current_dir, e)


def _find_mount_id(path: str, db: Session) -> int | None:
    mounts = db.query(Mount).all()
    best: Mount | None = None
    best_len = 0
    for m in mounts:
        mp = m.mount_point
        if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) > best_len:
            best = m
            best_len = len(mp)
    return best.id if best else None


def run_scan(
    db: Session,
    scan: Scan,
    root_path: str,
    one_filesystem: bool = True,
    exclude_paths: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    since_timestamp: float | None = None,   # incremental: skip files with mtime <= this
    cumulative_files: int = 0,
    cumulative_bytes: int = 0,
    cumulative_dirs: int = 0,
    cumulative_errors: int = 0,
    cumulative_cold_files: int = 0,
    cumulative_cold_bytes: int = 0,
    mark_completed: bool = True,
    stop_event: threading.Event | None = None,
) -> None:
    """
    Scan root_path and write results to the database incrementally.

    Commits happen every BATCH_SIZE files so the write lock is never
    held for more than a fraction of a second, keeping the web UI responsive.
    """
    excludes = list(settings.exclude_paths_list) + (exclude_paths or [])
    globs = exclude_globs or []
    now_ts = time.time()
    cold_days = scan.cold_days

    try:
        root_stat = os.stat(root_path)
        root_dev = root_stat.st_dev
    except OSError as e:
        scan.status = "failed"
        scan.error_summary = str(e)
        scan.finished_at = datetime.now()
        db.commit()
        logger.error("Cannot stat root %s: %s", root_path, e)
        return

    mount_id = _find_mount_id(root_path, db)

    dir_sizes: dict[str, int] = defaultdict(int)
    dir_file_count: dict[str, int] = defaultdict(int)
    dir_cold_count: dict[str, int] = defaultdict(int)
    dir_cold_size: dict[str, int] = defaultdict(int)

    total_files = cumulative_files
    total_bytes = cumulative_bytes
    error_count = cumulative_errors
    total_cold_files = cumulative_cold_files
    total_cold_bytes = cumulative_cold_bytes
    file_batch: list[dict] = []
    error_batch: list[ScanError] = []
    last_progress_commit = time.monotonic()
    _stopped = False

    def _flush_batch():
        """Insert the current file/error batch and commit — short lock window."""
        nonlocal file_batch, error_batch, last_progress_commit, _stopped
        if file_batch:
            db.bulk_insert_mappings(File, file_batch)
            file_batch = []
        for e in error_batch:
            db.add(e)
        error_batch = []

        # Update progress counters on the scan record so the web UI can poll them
        scan.total_files = total_files
        scan.total_bytes = total_bytes
        scan.error_count = error_count
        db.commit()
        last_progress_commit = time.monotonic()
        if stop_event is not None and stop_event.is_set():
            _stopped = True

    for path, st, is_link in _walk_one_fs(root_path, root_dev, one_filesystem, excludes, globs):
        if _stopped:
            break
        if st is None:
            error_count += 1
            error_batch.append(ScanError(
                scan_id=scan.id,
                path=path,
                error_type="OSError",
                error_message="Permission denied or file disappeared",
            ))
            if len(error_batch) >= BATCH_SIZE:
                _flush_batch()
            continue

        size = st.st_size
        atime = st.st_atime
        mtime = st.st_mtime
        # Incremental mode: skip files that haven't changed since last scan
        if since_timestamp and mtime <= since_timestamp and not is_link:
            continue
        ctime = st.st_ctime
        ext = Path(path).suffix.lower() or None
        bucket = _cold_bucket(atime, now_ts, cold_days)
        is_cold = bucket is not None

        parent = os.path.dirname(path)
        while True:
            dir_sizes[parent] += size
            dir_file_count[parent] += 1
            if is_cold:
                dir_cold_count[parent] += 1
                dir_cold_size[parent] += size
            if parent == root_path or parent == os.path.dirname(parent):
                break
            parent = os.path.dirname(parent)

        total_files += 1
        total_bytes += size
        if is_cold:
            total_cold_files += 1
            total_cold_bytes += size

        uid = st.st_uid
        try:
            uname = pwd.getpwuid(uid).pw_name
        except (KeyError, TypeError):
            uname = None

        file_batch.append({
            "scan_id": scan.id,
            "mount_id": mount_id,
            "path": path,
            "size_bytes": size,
            "extension": ext,
            "atime": atime,
            "mtime": mtime,
            "ctime": ctime,
            "is_symlink": is_link,
            "is_cold": is_cold,
            "cold_days_bucket": bucket,
            "uid": uid,
            "username": uname,
        })

        if len(file_batch) >= BATCH_SIZE:
            _flush_batch()

    # Flush any remaining files
    _flush_batch()

    if _stopped:
        scan.total_files = total_files
        scan.total_dirs = cumulative_dirs + len(dir_sizes)
        scan.total_bytes = total_bytes
        scan.cold_file_count = total_cold_files
        scan.cold_total_size_bytes = total_cold_bytes
        scan.error_count = error_count
        scan.status = "stopped"
        scan.error_summary = "Stopped by user"
        scan.finished_at = datetime.now()
        scan.duration_seconds = (scan.finished_at - scan.started_at).total_seconds()
        db.commit()
        logger.info("Scan %d stopped by user after %d files", scan.id, total_files)
        return

    # Write directory aggregations in batches
    dir_batch: list[dict] = []
    for dir_path, dir_size in dir_sizes.items():
        dir_batch.append({
            "scan_id": scan.id,
            "mount_id": mount_id,
            "path": dir_path,
            "total_size_bytes": dir_size,
            "file_count": dir_file_count[dir_path],
            "cold_file_count": dir_cold_count[dir_path],
            "cold_total_size_bytes": dir_cold_size[dir_path],
        })
        if len(dir_batch) >= BATCH_SIZE:
            db.bulk_insert_mappings(Directory, dir_batch)
            db.commit()
            dir_batch = []
    if dir_batch:
        db.bulk_insert_mappings(Directory, dir_batch)

    # Finalise the scan record
    scan.total_files = total_files
    scan.total_dirs = cumulative_dirs + len(dir_sizes)
    scan.total_bytes = total_bytes
    scan.cold_file_count = total_cold_files
    scan.cold_total_size_bytes = total_cold_bytes
    scan.error_count = error_count
    if mark_completed:
        scan.finished_at = datetime.now()
        scan.duration_seconds = (scan.finished_at - scan.started_at).total_seconds()
        scan.status = "completed"

    if mount_id:
        mount_obj = db.get(Mount, mount_id)
        if mount_obj:
            mount_obj.last_scanned_at = scan.finished_at or datetime.now()

    db.commit()
    logger.info(
        "Scan %d done: %d files, %d dirs, %d errors, %.1fs",
        scan.id, total_files, len(dir_sizes), error_count,
        scan.duration_seconds or 0,
    )
