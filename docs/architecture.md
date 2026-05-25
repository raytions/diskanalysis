# Architecture

## Overview

DiskAnalysis is split into two decoupled components:

```
┌─────────────────────────────────────────────────┐
│  Host machine                                   │
│                                                 │
│  scanner (Python CLI)                           │
│   ├─ reads /proc/mounts                         │
│   ├─ walks filesystem with os.scandir           │
│   └─ writes to SQLite → data/diskanalysis.db    │
│                                                 │
│  web panel (Docker, FastAPI + Jinja2)           │
│   ├─ reads from same SQLite                     │
│   ├─ triggers scans via background threads      │
│   └─ serves HTTP on :8000                       │
└─────────────────────────────────────────────────┘
```

The scanner runs on the host (as root or a privileged user). The web panel runs in Docker, reads from the shared SQLite database, and can also launch scans by spawning background threads that invoke the same scanner logic.

## Why the scanner runs on the host

- Needs `/proc/mounts` for accurate mount discovery
- Needs real `atime`/`mtime`/`st_dev` values — not virtualized by container overlay
- Needs permission escalation (root) to read NAS/NFS directories
- `xdev` (filesystem boundary) detection via `os.stat().st_dev` requires native access

## Data flow

```
1. Scanner runs (cron / systemd timer / web UI / manual):
   app.scanner scan --all-mounts
   — OR —
   POST /api/scans  (web panel spawns background thread)

2. For each mount:
   a. os.scandir walk, batch of 500 files
   b. Compute: size_bytes, atime, mtime, uid, extension, is_symlink
   c. Compute: is_cold (atime < now - cold_days), cold_days_bucket
   d. Bulk INSERT into files table
   e. After walk: aggregate directories table (cumulative sizes per dir)
   f. Update scans.cold_file_count, scans.cold_total_size_bytes
   g. Trigger background user attribution cache rebuild for scanned mounts

3. Web panel serves dashboard:
   - Latest scan IDs per mount (ix_files_mount_scan index)
   - UNION ALL per-mount queries for global overview tabs
   - Per-mount analytics: largest files, dirs, cold files, extension summary
   - By User tab: reads from user_attribution_cache (pre-warmed after scan)
```

## Database schema

```sql
mounts (id, mount_point, device, fs_type, is_network_fs,
        total_bytes, used_bytes, free_bytes, usage_percent,
        is_accessible, last_scanned_at,
        label, custom_host, custom_user, custom_paths,
        auth_method, ssh_key_path)

scans  (id, mount_id, scan_mode, target_path, status,
        started_at, finished_at, duration_seconds,
        total_files, total_dirs, total_bytes, error_count,
        cold_file_count, cold_total_size_bytes,
        cold_days, exclude_rules, filesystem_boundary_only,
        error_summary)

files  (id, scan_id, mount_id, path, size_bytes,
        atime, mtime, extension, uid, username,
        is_symlink, is_cold, cold_days_bucket)

directories (id, scan_id, mount_id, path,
             total_size_bytes, file_count,
             cold_file_count, cold_total_size_bytes)

scan_errors (id, scan_id, path, error_type, error_message)

user_attribution_cache (id, mount_id, scan_id, username,
                        total_size_bytes, file_count,
                        computed_at)

custom_users  (id, username, aliases, created_at)

app_settings  (key, value)
```

## Key indexes

| Index | Purpose |
|---|---|
| `ix_files_mount_scan` | Fast latest-scan lookup per mount |
| `ix_files_ms_size` | `ORDER BY size_bytes DESC` for largest-files queries |
| `ix_files_ms_cold_size` | Cold files sorted by size |
| `ix_files_ms_atime` | Cold files sorted by last access |
| `ix_files_ms_mtime` | Files sorted by modification time |
| `ix_files_scan_username` | Per-user file lookup |
| `ix_files_scan_uid_size` | Uid-based aggregation |
| `ix_files_mount_scan_path_uid` | Covering index for Phase 2 user attribution |
| `ix_dirs_mount_scan_size` | Largest directories per mount |

## Background thread model

The web panel launches long-running operations in daemon threads:

- **Scan execution** (`POST /api/scans`): each scan runs in its own thread; updates `scans.status`, `total_files`, `total_bytes` in real time; web clients poll `/api/scans/<id>` for progress.
- **User attribution cache rebuild**: triggered after each scan completes; runs as a separate thread; batch-queries all mount caches at once.
- **Mount record deletion**: when a mount is deleted, its database records (scan entries, file/dir rows, attribution cache) are purged in a background thread to avoid blocking the HTTP response.

Scans can be stopped via `POST /api/scans/<id>/stop`, which sets a thread-local stop flag; the scanner checks it between directory batches.

## Technology choices

| Concern | Choice | Reason |
|---|---|---|
| DB | SQLite (WAL mode) | Zero-ops, handles concurrent reads+writes, single file |
| Web | FastAPI + Jinja2 | Fast, minimal, SSR avoids JS bundle complexity |
| ORM | SQLAlchemy 2.x | Familiar, raw SQL escape hatch for performance |
| Scanner | stdlib only | `os.scandir`, `os.stat`, `shutil.disk_usage` — no extra deps |
| Config | pydantic-settings | `.env` file + env var override, typed |
| UI icons | Inline SVG sprite | Single `<svg style="display:none">` block in base.html; all icons referenced via `<use href="#i-name"/>` — no external font or icon library dependency |

## SQLite tuning

`db.py` sets the following pragmas on every connection:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-131072;      -- 128 MB page cache
PRAGMA temp_store=MEMORY;       -- sort buffers in RAM
PRAGMA mmap_size=536870912;     -- 512 MB memory-mapped I/O
PRAGMA wal_autocheckpoint=4000; -- checkpoint WAL every ~32 MB
```

A `PRAGMA wal_checkpoint(PASSIVE)` is also run in a background thread after each scan to prevent unbounded WAL growth under concurrent scan workloads.
