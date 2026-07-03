# Architecture

DiskAnalysis is a host-aware storage analytics app. It uses FastAPI for the web/API layer, Jinja2 for server-rendered pages, SQLite for persistence, and subprocess workers for long-running scans.

## System Overview

```text
Browser
  |
  | HTTP
  v
FastAPI app
  |-- Jinja pages: dashboard, mounts, scans, settings
  |-- JSON APIs: /api/mounts, /api/scans, /api/settings
  |-- scan scheduler thread
  |-- scan subprocess launcher
  |
  v
SQLite database, WAL mode
  |
  v
Host filesystems and network mounts
```

The app is designed for Linux hosts with many mounted storage volumes. It can be run directly on the host or in Docker, but accurate scanning requires that the process can see the real mount points and `/proc/mounts`.

## Main Components

| Component | Path | Responsibility |
|---|---|---|
| FastAPI app | `app/main.py` | App lifespan, startup cleanup, scheduler startup, route registration |
| Page routes | `app/web/routes_pages.py` | HTML routes and template context |
| Mount API | `app/api/routes_mounts.py` | Mount inventory, mount/unmount, SSHFS operations, analytics endpoints |
| Scan API | `app/api/routes_scans.py` | Create scans, stop scans, batch stop/delete, process monitoring |
| Settings API | `app/api/routes_settings.py` | Runtime settings, users, scan schedule status |
| Scheduler | `app/services/scan_scheduler.py` | Auto Scan interval loop and status messages |
| Scan worker | `app/scan_worker.py` | Subprocess entry point for web-triggered scans |
| Scanner engine | `app/services/scanner.py` | Filesystem traversal and aggregate writes |
| Analytics | `app/services/analytics.py` | Largest files/dirs, cold data, extension/user summaries |
| Mount discovery | `app/services/mounts.py` | `/proc/mounts` parsing and disk usage collection |

## Scan Execution Model

Web-triggered scans are not run in request threads. The API creates a `scans` row, then launches:

```text
python -m app.scan_worker <scan_id> <request_json_b64>
```

This subprocess model is deliberate. Network filesystems can block in uninterruptible kernel I/O, and Python threads cannot reliably recover from that. With subprocesses, the app can:

- set `stop_requested=true` in the database
- send SIGTERM to the worker
- escalate to SIGKILL if the worker does not exit
- mark scans as `stopped` or `interrupted`

The worker polls the database stop flag and also handles SIGTERM. The scanner checks the stop event between filesystem entries and progress flushes.

## Scan Statuses

| Status | Meaning |
|---|---|
| `queued` | A parallel scan was created but has not started yet |
| `running` | Worker subprocess is active |
| `completed` | Full scan finished and wrote a coherent snapshot |
| `stopped` | User requested stop; partial progress is kept |
| `interrupted` | Worker disappeared, server restarted, or process exited without normal completion |
| `failed` | Scanner could not start or root path could not be scanned |

Only `completed` scans trigger old-scan data cleanup and cache prewarm. Stopped/interrupted scans remain visible in history but are not treated as fresh full snapshots.

## Auto Scan

Auto Scan is configured from the Scans page and stored in `app_settings`:

- `scan_schedule_enabled`
- `scan_schedule_interval_minutes`
- `scan_schedule_mode`
- `scan_schedule_mount_ids`
- `scan_schedule_target_path`
- `scan_schedule_next_run_at`
- `scan_schedule_last_started_at`
- `scan_schedule_last_checked_at`
- `scan_schedule_last_status`
- `scan_schedule_last_message`

The scheduler thread wakes every 30 seconds. If the schedule is due and no scans are active, it creates a normal scan request through the same API path. If active scans exist, it skips that run, records a status message, and schedules the next run.

The Scans page polls `/api/settings/scan-schedule` so users can see whether Auto Scan is enabled, when it will run next, what happened last, and whether any scheduled scans are currently active.

## Mount and SSHFS Management

Mount data comes from both `/proc/mounts` and manually registered records. The Mounts UI supports:

- live accessible/inaccessible state
- right-click and multi-select actions
- mount detail pages
- SSHFS mount/unmount/remount
- stale FUSE detection
- lazy detach for broken SSHFS mounts
- remote-path diagnostics for SSHFS failures
- recent mount event history

SSHFS mount attempts verify the local mount point after `sshfs` exits. If the command exits successfully but the path returns `EIO` or is not readable, the app detaches the bad FUSE mount and records diagnostics.

## Database Model

Core tables:

```text
mounts
scans
files
directories
scan_errors
scan_ext_summary
scan_uid_summary
mount_events
custom_users
user_candidates
user_attribution_cache
app_settings
```

Scans do not store every file row forever. The scanner keeps top-N largest and coldest file rows, plus aggregate tables:

- `directories`: cumulative directory size/count aggregates up to a bounded depth
- `scan_ext_summary`: extension-level counts and bytes
- `scan_uid_summary`: uid/user aggregates by top-level and second-level directories
- `scans`: total counts, byte totals, cold totals, duration, errors

After a completed scan for a mount, old detailed records for previous completed scans of that mount are purged. The historical `scans` metadata rows remain.

## SQLite Settings

`app/db.py` configures SQLite for concurrent web reads and scan writes:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-131072;
PRAGMA temp_store=MEMORY;
PRAGMA mmap_size=536870912;
PRAGMA wal_autocheckpoint=4000;
```

The scan monitor runs a WAL checkpoint after completed scans.

## Authentication Model

If `ADMIN_PASSWORD` is empty, the app runs in trusted single-user mode. If set, public pages remain visible but write operations require an admin session.

Protected operations include:

- creating/stopping/deleting scans
- modifying mount records
- mounting/unmounting/remounting
- changing settings
- managing confirmed users

## Design Tradeoffs

- **Full scans only**: incremental scan mode was removed because it still traversed the whole tree and produced partial snapshots that could mislead analytics.
- **SQLite over service database**: simple deployment and enough performance for single-host operational workloads.
- **Server-rendered UI**: no build pipeline, predictable deployment, easy screenshots for documentation.
- **Subprocess workers**: better control over stuck network filesystem calls than threads.
