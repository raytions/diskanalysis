# Configuration Reference

DiskAnalysis reads base configuration from `.env` and stores runtime UI settings in the SQLite `app_settings` table.

Copy the example file:

```bash
cp .env.example .env
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `production` | Environment label |
| `APP_HOST` | `0.0.0.0` | Web bind address |
| `APP_PORT` | `8000` | Web port |
| `DB_PATH` | `/data/diskanalysis.db` | SQLite database path |
| `DEFAULT_COLD_DAYS` | `90` | Default cold-data threshold |
| `DEFAULT_TOP_N` | `100` | Default top-N setting for scan result rows |
| `SCAN_ROOTS` | empty | Optional comma-separated scan roots, instead of auto-discovered mounts |
| `EXCLUDE_PATHS` | `/proc,/sys,/dev,/run,/snap` | Always-excluded scan paths |
| `ALLOWED_FS_TYPES` | empty | Optional filesystem type allow-list |
| `NETWORK_FS_TYPES` | `nfs,nfs4,cifs,...` | Filesystem types treated as network mounts |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ADMIN_PASSWORD` | empty | Enables admin login when set |
| `PATH_USER_ATTRIBUTION` | `true` | Match user directories before uid fallback |

Example:

```env
APP_PORT=8000
DB_PATH=/data/diskanalysis.db
DEFAULT_COLD_DAYS=90
LOG_LEVEL=INFO
ADMIN_PASSWORD=
PATH_USER_ATTRIBUTION=true
```

## Runtime Settings

Runtime settings are saved in the `app_settings` table and can be changed without restarting the app.

| Setting | UI Location | Description |
|---|---|---|
| Path attribution | Settings | Enable directory-name user attribution |
| Max parallel scans | Settings | Controls parallel mount scans |
| Sudo mkdir mode | Settings | Allows web-created SSHFS mount directories through a narrow sudo flow |
| Auto Scan | Scans | Enables recurring full scans |
| Auto Scan target | Scans | All mounts, selected mounts, or a specific path |
| Auto Scan interval | Scans | Minutes between scheduled scan attempts |
| Auto Scan cold threshold | Scans | Cold-data threshold for scheduled scans |
| Auto Scan exclude rules | Scans | Per-schedule scan excludes |

## Auto Scan Settings Keys

Auto Scan stores:

```text
scan_schedule_enabled
scan_schedule_interval_minutes
scan_schedule_mode
scan_schedule_mount_ids
scan_schedule_target_path
scan_schedule_filesystem_boundary_only
scan_schedule_exclude_rules
scan_schedule_cold_days
scan_schedule_next_run_at
scan_schedule_last_started_at
scan_schedule_last_checked_at
scan_schedule_last_status
scan_schedule_last_message
```

`scan_schedule_scan_type` may exist from older installations, but the app now treats all scheduled scans as full scans.

## Authentication

If `ADMIN_PASSWORD` is empty:

- the app runs in trusted single-user mode
- write operations are open

If `ADMIN_PASSWORD` is set:

- users can view public pages
- admin-only controls are hidden until login
- API write operations require an admin session

Protected actions include:

- starting/stopping scans
- deleting scans
- adding/editing/deleting mounts
- SSHFS mount/unmount/remount
- changing settings
- managing users

## Filesystem Settings

`NETWORK_FS_TYPES` controls which filesystems are displayed as network filesystems. The default includes:

```text
nfs,nfs4,cifs,smb3,smbfs,davfs,samba,fuse.sshfs,fuse.s3fs,glusterfs,cephfs,lustre
```

`ALLOWED_FS_TYPES` is empty by default. If set, mount discovery only includes matching filesystem types.

## User Attribution

`PATH_USER_ATTRIBUTION=true` is recommended for NFS/SMB layouts where files are uid-squashed but directory names identify users.

See [user-attribution.md](user-attribution.md).

## Security Notes

- Do not commit `.env`.
- Prefer key-based SSH for SSHFS when possible.
- Treat sudo mkdir mode as a host-level privilege feature.
- If exposing beyond a trusted network, set `ADMIN_PASSWORD` and put the app behind HTTPS.
