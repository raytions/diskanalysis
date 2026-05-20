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
│   └─ serves HTTP on :8000                       │
└─────────────────────────────────────────────────┘
```

The scanner runs on the host (as root or a privileged user). The web panel runs in Docker and only reads from the shared SQLite database.

## Why the scanner runs on the host

- Needs `/proc/mounts` for accurate mount discovery
- Needs real `atime`/`mtime`/`st_dev` values — not virtualized by container overlay
- Needs permission escalation (root) to read NAS/NFS directories
- `xdev` (filesystem boundary) detection via `os.stat().st_dev` requires native access

## Data flow

```
1. Scanner runs (cron / systemd timer / manual):
   app.scanner scan --all-mounts

2. For each mount:
   a. os.scandir walk, batch of 500 files
   b. Compute: size_bytes, atime, mtime, uid, extension, is_symlink
   c. Compute: is_cold (atime < now - cold_days), cold_days_bucket
   d. Bulk INSERT into files table
   e. After walk: aggregate directories table (cumulative sizes per dir)
   f. Update scans.cold_file_count, scans.cold_total_size_bytes

3. Web panel serves dashboard:
   - Latest scan IDs per mount (ix_files_mount_scan index)
   - UNION ALL per-mount queries for global overview tabs
   - Per-mount analytics: largest files, dirs, cold files, extension summary
```

## Database schema

```sql
mounts (id, mount_point, device, fs_type, is_network_fs,
        total_bytes, used_bytes, free_bytes, usage_percent,
        is_accessible, last_scanned_at)

scans  (id, mount_id, scan_mode, target_path, status,
        started_at, finished_at, duration_seconds,
        total_files, total_bytes, error_count,
        cold_file_count, cold_total_size_bytes)

files  (id, scan_id, mount_id, path, size_bytes,
        atime, mtime, extension, uid, username,
        is_symlink, is_cold, cold_days_bucket)

directories (id, scan_id, mount_id, path,
             total_size_bytes, file_count,
             cold_file_count, cold_total_size_bytes)

scan_errors (id, scan_id, path, error_message)
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
| `ix_dirs_mount_scan_size` | Largest directories per mount |

## Technology choices

| Concern | Choice | Reason |
|---|---|---|
| DB | SQLite (WAL mode) | Zero-ops, handles concurrent reads+writes, single file |
| Web | FastAPI + Jinja2 | Fast, minimal, SSR avoids JS bundle complexity |
| ORM | SQLAlchemy 2.x | Familiar, raw SQL escape hatch for performance |
| Scanner | stdlib only | `os.scandir`, `os.stat`, `shutil.disk_usage` — no extra deps |
| Config | pydantic-settings | `.env` file + env var override, typed |
