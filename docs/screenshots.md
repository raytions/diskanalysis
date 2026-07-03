# Screenshots

Screenshots are stored in:

```text
docs/assets/screenshots/
```

Current files:

| File | Page |
|---|---|
| `dashboard.png` | Dashboard overview |
| `mounts.png` | Mount inventory and actions |
| `scans.png` | Scan history, Run Scan, and Auto Scan |
| `settings.png` | Settings and admin controls |

## README Usage

`README.md` references the images with relative paths:

```markdown
![DiskAnalysis dashboard](docs/assets/screenshots/dashboard.png)
```

GitHub renders these paths directly.

## Refresh Commands

Start the app first:

```bash
uvicorn app.main:app --host :: --port 8000
```

Install Playwright browser binaries if needed:

```bash
npx --yes playwright install chromium
```

Capture public screenshots:

```bash
npx --yes playwright screenshot --browser=chromium --viewport-size=1440,1000 'http://[::1]:8000/' docs/assets/screenshots/dashboard.png
npx --yes playwright screenshot --browser=chromium --viewport-size=1440,1000 'http://[::1]:8000/mounts' docs/assets/screenshots/mounts.png
npx --yes playwright screenshot --browser=chromium --viewport-size=1440,1000 'http://[::1]:8000/scans' docs/assets/screenshots/scans.png
npx --yes playwright screenshot --browser=chromium --viewport-size=1440,1000 'http://[::1]:8000/settings' docs/assets/screenshots/settings.png
```

If admin auth is enabled, log in first and save Playwright storage state, then pass `--load-storage`.

## Public Repository Notes

The committed screenshots are generated from a sanitized demo database. If you replace them with screenshots from a real deployment, review them for:

- private hostnames
- mount names
- user names
- capacity values
- internal paths

Replace screenshots with sanitized examples if needed.
