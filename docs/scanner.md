# Scanner Reference

DiskAnalysis scans filesystems and writes storage aggregates to SQLite. Scans can be started from the web UI, the Auto Scan scheduler, or the host-side CLI.

## Scan Types

DiskAnalysis currently runs **full scans only**.

The previous incremental mode was removed because it still traversed the full directory tree, then wrote only changed-file aggregates. That made the latest scan look like a partial snapshot, which was unsafe for capacity dashboards. A scan is now expected to represent a coherent full view of the selected mount/path.

## Web Scans

The Scans page supports:

- selected mounted volumes
- all accessible mounts
- a specific path
- cold-data threshold
- filesystem boundary protection
- exclude rules
- live progress polling
- single scan stop
- batch stop for running/queued scans
- scheduled recurring scans through Auto Scan

Web scans are executed by subprocess workers:

```text
python -m app.scan_worker <scan_id> <request_json_b64>
```

This allows the API to terminate workers that get stuck in network filesystem calls.

## CLI Usage

Run commands from the project root with the same environment/database used by the web app.

```bash
python -m app.scanner doctor
python -m app.scanner mounts
python -m app.scanner scan --all-mounts
python -m app.scanner scan --mount /mnt/research-a
python -m app.scanner scan --path /mnt/research-a/project-alpha
```

### `doctor`

```bash
python -m app.scanner doctor
```

Checks:

- SQLite connectivity
- mount discovery
- inaccessible mounts
- key config values

### `mounts`

```bash
python -m app.scanner mounts
```

Lists discovered mounts from `/proc/mounts`, including filesystem type, network flag, usage percentage, total size, and free space.

### `scan`

```bash
python -m app.scanner scan --all-mounts
python -m app.scanner scan --mount /mnt/research-a
python -m app.scanner scan --path /mnt/research-a/project-alpha
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--all-mounts` | off | Scan all accessible discovered mounts |
| `--mount PATH` | none | Scan one registered mount point |
| `--path PATH` | none | Scan an arbitrary path |
| `--cold-days N` | `DEFAULT_COLD_DAYS` | Mark files not accessed in N days as cold |
| `--top-n N` | `DEFAULT_TOP_N` | Store top-N result rows |
| `--exclude PATHS` | none | Comma-separated paths/globs to exclude |
| `--one-filesystem` | on | Stay within the starting filesystem |
| `--follow-mounts` | off | Allow crossing filesystem boundaries |

Examples:

```bash
python -m app.scanner scan --all-mounts --cold-days 180
python -m app.scanner scan --mount /mnt/archive-b --exclude /mnt/archive-b/.trash,*.tmp
python -m app.scanner scan --path /mnt/research-a/project-alpha --follow-mounts
```

## Scanner Output

A completed scan updates:

- `scans.total_files`
- `scans.total_dirs`
- `scans.total_bytes`
- `scans.cold_file_count`
- `scans.cold_total_size_bytes`
- `scans.error_count`
- `files` top largest/coldest file rows
- `directories` aggregate directory rows
- `scan_ext_summary`
- `scan_uid_summary`

Errors are recorded in `scan_errors` and do not abort the scan unless the root path cannot be accessed.

## Filesystem Boundary Protection

`--one-filesystem` is enabled by default. It compares `st_dev` values and avoids descending into nested mounts.

Keep this enabled when scanning parent directories such as `/media` or `/mnt`. Otherwise one scan can accidentally traverse every mounted NAS volume beneath that directory.

## Exclude Rules

Global excludes come from `EXCLUDE_PATHS`. Per-scan excludes can be supplied from the UI or CLI.

Examples:

```text
/tmp
/mnt/research-a/.snapshot
*.cache
*.tmp
```

## Cold Data Caveats

Cold data uses access time (`atime`). Many Linux filesystems use `relatime` or `noatime`, so cold detection is best-effort.

Check mount options with:

```bash
cat /proc/mounts | grep -E 'relatime|noatime'
```

## Stop Behavior

When a scan is stopped:

1. The API sets `scans.stop_requested=true`.
2. The worker receives SIGTERM if its PID is known.
3. The worker exits cleanly if it can.
4. If it does not exit, the monitor escalates to SIGKILL.
5. The scan is marked `stopped`.

Stopped scans are visible in history but do not replace the latest completed full snapshot.
