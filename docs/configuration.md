# Configuration Reference

All settings are read from `.env` in the project root, and can be overridden by environment variables.

Copy `.env.example` to `.env` and edit as needed.

## Core settings

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `production` | Environment name (informational only) |
| `APP_HOST` | `0.0.0.0` | Bind address for the web panel |
| `APP_PORT` | `8000` | Port for the web panel |
| `DB_PATH` | `/data/diskanalysis.db` (Docker) | Path to the SQLite database. Both scanner and web must access the same file. Outside Docker, defaults to `./data/diskanalysis.db`. |
| `LOG_LEVEL` | `INFO` | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Scanner settings

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_COLD_DAYS` | `90` | Default cold-data threshold in days. Files not accessed in this many days are marked `is_cold=1`. |
| `DEFAULT_TOP_N` | `100` | Default result count for analytics queries |
| `SCAN_ROOTS` | `` | Comma-separated paths to scan when using `--all-mounts` (overrides auto-detected mounts) |
| `EXCLUDE_PATHS` | `/proc,/sys,/dev,/run,/snap` | Always-excluded paths during scanning |
| `ALLOWED_FS_TYPES` | `` | If non-empty, only scan mounts with these filesystem types (comma-separated) |
| `NETWORK_FS_TYPES` | `nfs,nfs4,cifs,smb3,...` | FS types treated as network mounts (shown with NFS badge in the UI) |

## User attribution settings

| Variable | Default | Description |
|---|---|---|
| `PATH_USER_ATTRIBUTION` | `true` | When `true`, attribute files to users by matching the top-level directory name against known usernames **before** falling back to uid. See [User Attribution](user-attribution.md) for details. Can also be toggled live in Settings. |

## Auth settings

| Variable | Default | Description |
|---|---|---|
| `ADMIN_PASSWORD` | `` | If set, enables session-based admin authentication. Accessing the UI without a valid session redirects to `/login`. Leave empty for open access (trusted network only). |

## UI-configurable settings

The following settings are also adjustable at runtime via **Settings** in the web panel, without restarting the server. Changes are persisted in the `app_settings` database table and take priority over `.env` values.

| Setting | Location in UI | Description |
|---|---|---|
| `PATH_USER_ATTRIBUTION` | Settings → Attribution | Toggle path-based vs uid-only attribution |
| `DEFAULT_COLD_DAYS` | Settings → Scan | Default cold-data threshold for new scans |
| `DEFAULT_TOP_N` | Settings → Scan | Default result count per tab |

## Example `.env`

```env
APP_PORT=8000
DB_PATH=/mnt/data/diskanalysis.db
DEFAULT_COLD_DAYS=90
LOG_LEVEL=INFO

# Auth (leave empty for no auth)
ADMIN_PASSWORD=changeme

# Path-based user attribution (useful for NFS4 all_squash environments)
PATH_USER_ATTRIBUTION=true
```
