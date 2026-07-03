"""
Core directory scanner.

Design principles:
- Keeps only top-N files in memory via two min-heaps (largest + coldest).
- No individual file rows inserted during walk — only top-500 at the end.
- Pre-computes extension and uid/dir aggregates in memory; inserts summary rows.
- This keeps DB storage ~10 MB per scan regardless of file count.
- Commits scan progress every PROGRESS_INTERVAL seconds so the web UI shows live progress.
- Respects filesystem boundaries (--one-filesystem / xdev).
- Handles permissions errors, disappeared files, broken symlinks gracefully.
"""
import fnmatch
import heapq
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
from app.models import Directory, File, Mount, Scan, ScanError, ScanExtSummary, ScanUidSummary

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
PROGRESS_INTERVAL = 10  # seconds between scan-record progress updates
COLD_BUCKETS = [30, 90, 180]  # days
TOP_N_FILES = 500       # keep this many largest + this many coldest files
MAX_DIR_DEPTH = 5       # max depth (relative to scan root) to store in directories table


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
    Scan root_path and write aggregate results to the database.

    Individual file rows are NOT stored for all files — only the top-500
    largest and top-500 coldest files are persisted. Extension and uid/dir
    aggregates are stored in scan_ext_summary and scan_uid_summary tables.
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
    root_stripped = root_path.rstrip('/')
    root_len = len(root_stripped)

    # Directory cumulative size aggregates — keyed by absolute path
    dir_sizes: dict[str, int] = defaultdict(int)
    dir_file_count: dict[str, int] = defaultdict(int)
    dir_cold_count: dict[str, int] = defaultdict(int)
    dir_cold_size: dict[str, int] = defaultdict(int)

    # Top-N heaps: min-heap of (score, tiebreak, row_dict)
    # _top_files: score = size_bytes (keep largest)
    # _top_cold:  score = -atime (keep coldest = smallest atime = most negative -atime wins)
    _top_files: list = []
    _top_cold: list = []
    _ctr = 0

    # Extension summary: ext -> [count, bytes, cold_count, cold_bytes]
    ext_agg: dict = defaultdict(lambda: [0, 0, 0, 0])

    # UID/dir summary: (top_dir, sub_dir, uid) -> [file_count, total_bytes, username]
    # top_dir = first path component relative to root_stripped
    # sub_dir = second path component relative to root_stripped (or "" if at depth ≤ 1)
    uid_dir_agg: dict = defaultdict(lambda: [0, 0, None])

    total_files = cumulative_files
    total_bytes = cumulative_bytes
    error_count = cumulative_errors
    total_cold_files = cumulative_cold_files
    total_cold_bytes = cumulative_cold_bytes
    error_batch: list[ScanError] = []
    last_progress_commit = time.monotonic()
    _stopped = False

    def _flush_progress():
        nonlocal error_batch, last_progress_commit, _stopped
        for e in error_batch:
            db.add(e)
        error_batch = []
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
                _flush_progress()
            continue

        size = st.st_size
        atime = st.st_atime
        mtime = st.st_mtime
        ctime = st.st_ctime
        ext = Path(path).suffix.lower() or None
        bucket = _cold_bucket(atime, now_ts, cold_days)
        is_cold = bucket is not None

        # Update directory cumulative aggregates for all ancestors
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

        # Extension aggregate
        ea = ext_agg[ext]
        ea[0] += 1
        ea[1] += size
        if is_cold:
            ea[2] += 1
            ea[3] += size

        # UID/dir aggregate — compute depth-1 and depth-2 components
        after = path[root_len:].lstrip('/')
        parts = after.split('/', 2)
        if len(parts) > 1:
            top_dir = parts[0]
            sub_dir = parts[1] if len(parts) > 2 else ""
        else:
            top_dir = ""
            sub_dir = ""
        ud = uid_dir_agg[(top_dir, sub_dir, uid)]
        ud[0] += 1
        ud[1] += size
        if ud[2] is None:
            ud[2] = uname

        # Top-N heaps
        row = {
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
        }
        _ctr += 1
        if len(_top_files) < TOP_N_FILES:
            heapq.heappush(_top_files, (size, _ctr, row))
        elif size > _top_files[0][0]:
            heapq.heapreplace(_top_files, (size, _ctr, row))

        if is_cold:
            neg_a = -atime
            if len(_top_cold) < TOP_N_FILES:
                heapq.heappush(_top_cold, (neg_a, _ctr, row))
            elif neg_a > _top_cold[0][0]:
                heapq.heapreplace(_top_cold, (neg_a, _ctr, row))

        # Periodic progress update
        if total_files % BATCH_SIZE == 0 or (time.monotonic() - last_progress_commit >= PROGRESS_INTERVAL):
            _flush_progress()

    # Flush final progress/errors
    _flush_progress()

    # ── Write all aggregates ──────────────────────────────────────────────────

    # Top-N files (deduplicated between largest and coldest heaps)
    seen_paths: set[str] = set()
    file_rows: list[dict] = []
    for _, _, row in _top_files:
        if row["path"] not in seen_paths:
            seen_paths.add(row["path"])
            file_rows.append(row)
    for _, _, row in _top_cold:
        if row["path"] not in seen_paths:
            seen_paths.add(row["path"])
            file_rows.append(row)
    if file_rows:
        db.bulk_insert_mappings(File, file_rows)

    # Directories — only store up to MAX_DIR_DEPTH levels from scan root
    dir_batch: list[dict] = []
    for dp, ds in dir_sizes.items():
        if dp[root_len:].count('/') > MAX_DIR_DEPTH:
            continue
        dir_batch.append({
            "scan_id": scan.id,
            "mount_id": mount_id,
            "path": dp,
            "total_size_bytes": ds,
            "file_count": dir_file_count[dp],
            "cold_file_count": dir_cold_count.get(dp, 0),
            "cold_total_size_bytes": dir_cold_size.get(dp, 0),
        })
        if len(dir_batch) >= BATCH_SIZE:
            db.bulk_insert_mappings(Directory, dir_batch)
            db.commit()
            dir_batch = []
    if dir_batch:
        db.bulk_insert_mappings(Directory, dir_batch)

    # Extension summary
    ext_rows = [
        {
            "scan_id": scan.id,
            "mount_id": mount_id,
            "extension": ext,
            "file_count": v[0],
            "total_bytes": v[1],
            "cold_count": v[2],
            "cold_bytes": v[3],
        }
        for ext, v in ext_agg.items()
    ]
    if ext_rows:
        db.bulk_insert_mappings(ScanExtSummary, ext_rows)

    # UID/dir summary
    uid_rows = [
        {
            "scan_id": scan.id,
            "mount_id": mount_id,
            "top_dir": k[0],
            "sub_dir": k[1],
            "uid": k[2],
            "username": v[2],
            "file_count": v[0],
            "total_bytes": v[1],
        }
        for k, v in uid_dir_agg.items()
    ]
    if uid_rows:
        db.bulk_insert_mappings(ScanUidSummary, uid_rows)

    # ── Finalize scan record ──────────────────────────────────────────────────
    scan.total_files = total_files
    scan.total_dirs = cumulative_dirs + len(dir_sizes)
    scan.total_bytes = total_bytes
    scan.cold_file_count = total_cold_files
    scan.cold_total_size_bytes = total_cold_bytes
    scan.error_count = error_count

    if _stopped:
        scan.status = "stopped"
        scan.error_summary = "Stopped by user"
        scan.finished_at = datetime.now()
        scan.duration_seconds = (scan.finished_at - scan.started_at).total_seconds()
        db.commit()
        logger.info("Scan %d stopped by user after %d files", scan.id, total_files)
        return

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
