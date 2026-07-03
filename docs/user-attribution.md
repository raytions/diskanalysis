# User Attribution

The "By User" tab shows disk usage broken down by user. This page explains how attribution works, how to configure it, and the performance characteristics.

## The problem with uid-only attribution

On a typical Linux system, each file has a uid (user ID). Resolving uid → username via `/etc/passwd` or `pwd.getpwuid()` works well for local filesystems.

However, on NFS4 mounts **without uid isolation**, the NFS server assigns a single system uid (e.g. `1024`) to all files, regardless of who actually owns them. If uid `1024` happens to match a local user (e.g. `ula`), all 4 TB of NAS data gets attributed to `ula` — which is wrong.

**Example:** `/mnt/research-a/alice/` and `/mnt/research-a/ben/` belong to different people, but all files have `uid=1024` (the NFS4 squash uid). Pure uid attribution would show everything under one local account.

---

## Path-based attribution (default)

When `PATH_USER_ATTRIBUTION=true` (the default), the system uses a multi-phase strategy. **Each file is counted exactly once.**

### Phase 1a — Depth-1 directory matching

For each mount, the system looks at the **immediate child directories** using the `directories` table (which stores cumulative sizes including all descendants).

Each directory name is matched against the **Confirmed Users** list using **prefix matching**:

```
/mnt/research-a/alice/       → alice  (exact match)
/mnt/research-a/ben/         → ben    (exact match)
/mnt/archive-b/chen-data/    → chen   (prefix: "chen" matches "chen-data")
/mnt/archive-b/dana2026/     → dana   (prefix: "dana" matches "dana2026")
```

Matching is case-insensitive. Longer usernames take priority (avoids false matches when one name is a prefix of another). Only directories that match are attributed in Phase 1a; their entire subtree is credited to the matched user.

### Phase 1b — Depth-2 directory matching (intermediate directories)

For depth-1 directories that **did not match** any user (e.g. an intermediate container directory like `USER_PATH`), the system looks **one level deeper** and matches their immediate children against the Confirmed Users list:

```
/mnt/research-a/USER_PATH/alice/   → alice  (matched at depth 2, under unmatched USER_PATH)
/mnt/research-a/USER_PATH/ben/     → ben    (matched at depth 2)
/mnt/research-a/chen/              → chen   (matched at depth 1 as normal)
```

This handles NAS layouts where user directories are placed inside an intermediate container directory rather than directly under the mount point.

### Phase 2 — Uid fallback for unmatched files

Files that are **not** under any Phase 1a or 1b–matched directory are attributed by uid. The query excludes matched directories (using `NOT IN` for depth-1 and `NOT LIKE` for depth-2 prefixes), so there is no double-counting.

This handles:
- Root-owned and system files
- Files outside user-named directories
- Mounts where uid isolation works correctly

### Username sources

Known usernames come from:
1. The **Confirmed Users** list in Settings
2. The `username` column stored in the `files` table (recorded by the scanner from `/etc/passwd` at scan time)
3. Live `pwd.getpwuid()` lookup for uids present in the scan data

Only names matching `[a-zA-Z0-9_\-\.]+` are used for path matching.

---

## Managing users — Settings page

Go to **Settings** (gear icon in the top-right nav) to manage user lists.

The Settings page uses a sidebar layout: click **Users** in the left nav to see both Confirmed Users and Candidates on one page.

### Confirmed Users

The authoritative list. These names are used for Phase 1 directory matching and uid resolution.

- **Add**: type a username and press Enter (or click the add button)
- **Alias**: click the link icon next to an entry to assign an alias (e.g. merge `jh` → `jiahao`)
- **Delete**: click the trash icon to remove a user

### Candidates

A read-only pool populated by **Sync from Scans**, which scans the `directories` table and `/etc/passwd` to find likely usernames. Candidates do **not** affect attribution — they are suggestions only.

- **Add to Confirmed**: move a candidate into the Confirmed Users list
- **Dismiss**: discard candidates that are not relevant

This two-stage workflow prevents accidental overwriting of the curated Confirmed list when re-syncing.

---

## Attribution cache

Computing the "By User" breakdown involves scanning millions of file records. To avoid recalculating this on every page load, results are cached in the `user_attribution_cache` table.

### Cache lifecycle

| Event | Cache action |
|---|---|
| Scan completes | Cache rebuilt automatically for all scanned mounts (background thread) |
| Confirmed Users changed | Cache invalidated — recomputed on next "By User" load |
| Path attribution toggle changed | Cache invalidated — recomputed on next "By User" load |
| First "By User" load after a cache miss | Recomputed and stored (may take 1–5 minutes for very large mounts) |

The cache rebuild after a scan is batched: all mounts are queried at once rather than one by one, which avoids N separate DB connections for servers with 20+ mounts.

### Manual cache management

In **Settings → Attribution**:

- **Rebuild Now** — invalidates the cache and starts a background rebuild for all mounts
- Cache status and last-computed timestamps are shown per-mount

---

## Configuration

### Enable/disable path attribution

In `.env` (or toggle in Settings UI):

```env
# true (default): directory-name prefix matching first, then uid fallback
PATH_USER_ATTRIBUTION=true

# false: uid-only attribution
PATH_USER_ATTRIBUTION=false
```

### When to disable path attribution

- NFS/SMB mounts **with** proper uid isolation (uid-per-user, `no_root_squash` is fine)
- You want pure uid-based accounting regardless of directory names
- Directory layout does not reflect ownership (shared project directories, etc.)

### When path attribution is most useful

- NFS4 mounts with `all_squash` or `anonuid` (all files share the same uid)
- CIFS/SMB mounts where Windows ACLs are not reflected in Linux uid
- Any setup where directories are named after users but uid isolation was not configured

---

## Performance

### Query design

Phase 1 reads from the `directories` table (top-level dirs only — a small subset). Phase 2 queries the `files` table filtered to files whose top-level directory did **not** match in Phase 1, grouped by uid.

### Indexes

The following indexes accelerate the attribution query:

| Index | Purpose |
|---|---|
| `ix_files_mount_scan` on `(mount_id, scan_id)` | Narrow to the right scan |
| `ix_files_mount_scan_path_uid` on `(mount_id, scan_id, path, uid, size_bytes)` | Covering index for Phase 2 — eliminates heap reads |
| `ix_dirs_mount_scan_size` on `(mount_id, scan_id, total_size_bytes DESC)` | Fast Phase 1 directory lookup |

With these indexes and warm cache, the "By User" tab loads in under 1 second. On a cache miss for a mount with 10M+ files, the first computation can take 1–5 minutes depending on I/O speed. Results are cached immediately after.

### SQLite tuning

`db.py` sets the following pragmas on every connection:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-131072;      -- 128 MB page cache
PRAGMA temp_store=MEMORY;       -- sort buffers in RAM
PRAGMA mmap_size=536870912;     -- 512 MB memory-mapped I/O
PRAGMA wal_autocheckpoint=4000; -- checkpoint WAL every ~32 MB
```
