# Recent Updates

This file tracks the latest user-facing changes so the GitHub project page and docs stay aligned with the running app.

## July 2026

### GitHub Documentation Refresh

- Reworked `README.md` into a GitHub landing page.
- Added project screenshots under `docs/assets/screenshots/`.
- Updated architecture, deployment, scanner, and configuration docs.
- Added this recent-update log.

### Auto Scan Visibility

- Auto Scan now lives on the Scans page.
- The Auto Scan panel shows:
  - enabled/disabled state
  - interval
  - next run time
  - last started time
  - latest scheduler status message
  - active scheduled scans
- Added `GET /api/settings/scan-schedule`.
- Scans page now polls schedule status and auto-refreshes when a scheduled scan appears.
- Scheduler now records start/skip/failure messages.

### Scan Control

- Removed incremental scan mode.
- All scans are now full scans.
- Added batch stop support for running/queued scans.
- Fixed stopped scan finalization so stopped scans are not treated as completed snapshots.
- Added scan stop tests in `tests/test_scan_stop.py`.

### SSHFS and Mount Management

- Added web-based SSHFS mount flows with password/key support.
- Added web-created local mount directories.
- Added narrow sudo mkdir mode for mount directory creation.
- Added Recent Mount Events.
- Added remount actions through context and selection menus rather than a crowded table column.
- Added stale SSHFS detection and lazy detach for broken FUSE mounts.
- Improved SSHFS failure diagnostics, including remote-path checks.
- Fixed status display so inaccessible FUSE mounts are not shown as healthy.

### UI Refresh

- Default theme is now dark mode.
- Improved Scans page unauthenticated layout.
- Updated Mounts page actions and multi-select behavior.
- Recent mount events were moved nearer mount point details.
- Reduced visual clutter in tables and action columns.

## Known Test Debt

The current full test suite still has three legacy analytics test failures. They pre-date the recent scan/SSHFS work and relate to old expected return shapes for largest-file/largest-directory helpers.

Passing targeted checks:

```bash
python3 -m compileall app
python3 -m pytest tests/test_scan_stop.py -q
```
