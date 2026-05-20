# User Attribution

The "By User" tab shows disk usage broken down by user. This page explains how attribution works and how to configure it for NFS4 environments.

## The problem with uid-only attribution

On a typical Linux system, each file has a uid (user ID). Resolving uid → username via `/etc/passwd` or `pwd.getpwuid()` works well for local filesystems.

However, on NFS4 mounts **without uid isolation**, the NFS server assigns a single system uid (e.g. `1024`) to all files, regardless of who actually owns them. If uid `1024` happens to match a local user (e.g. `ula`), all 4TB of NAS data gets attributed to `ula` — which is wrong.

**Example:** `/media/NAS02/jiahao/` and `/media/NAS02/fanwen/` are owned by different people, but all files have `uid=1024` (the NFS4 nobody/system uid). Pure uid attribution would show all of this as `ula`'s data.

## Path-based attribution (default)

When `PATH_USER_ATTRIBUTION=true` (the default), the system uses a two-phase approach:

### Phase 1: Top-level directory matching (primary)

For each mount, the system looks at the **immediate child directories** of the mount point using the `directories` table (which stores cumulative sizes — each directory entry includes all descendants).

It then tries to match each directory name against the list of known usernames using **prefix matching**:

```
/media/NAS02/jiahao/     → jiahao  (exact match)
/media/NAS02/fanwen/     → fanwen  (exact match)
/media/hdd/yyfangspx/   → yyfang  (prefix: "yyfang" matches "yyfangspx")
/media/hdd/wangzi2022/  → wangzi  (prefix: "wangzi" matches "wangzi2022")
```

The prefix is matched case-insensitively. Longer username prefixes take priority (to avoid false matches when one username is a prefix of another).

This is accurate because the directory name is authoritative — whoever created `/media/NAS02/jiahao/` is jiahao, regardless of the file's uid.

### Phase 2: Uid-based attribution (fallback)

Files in directories that did not match any known username are attributed by uid, proportionally distributed based on each uid's share of the mount's total size.

This handles:
- Root files and system files on non-NFS mounts
- Files outside user-owned directories

### Where usernames come from

The system resolves known usernames from:
1. `username` column stored in the `files` table (set by scanner from `/etc/passwd` at scan time)
2. Live `pwd.getpwuid()` lookup for uids present in the current scan

Only usernames matching `[a-zA-Z0-9_\-\.]+` (safe filesystem name characters) are used for path matching.

## Configuration

### Enable/disable path attribution

In `.env`:

```env
# true (default): use directory-name prefix matching first, then uid fallback
PATH_USER_ATTRIBUTION=true

# false: uid-only attribution (original behaviour)
PATH_USER_ATTRIBUTION=false
```

### When to disable path attribution

- Your NFS/SMB mounts **do** have proper uid isolation (uid-per-user, squash_root off)
- You want pure uid-based accounting regardless of directory names
- Your directory layout does not reflect ownership (e.g. a shared project directory with multiple contributors)

### When path attribution is most useful

- NFS4 mounts with `all_squash` or `anonuid` (all files show same uid)
- CIFS/SMB mounts where Windows ACLs are not reflected in Linux uid
- Any setup where directories are named after users but uid isolation was not configured

## Performance

Phase 1 reads from the `directories` table (~600K rows on a typical large server), not the `files` table (~7.5M rows). This makes the "By User" query fast (~1–2 seconds) regardless of total file count.

Phase 2 uses a simple `GROUP BY uid` on the `files` table with a covering index, which is also fast.
