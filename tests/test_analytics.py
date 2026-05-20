"""Tests for analytics service."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use in-memory SQLite for tests
os.environ["DB_PATH"] = ":memory:"

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Directory, File, Mount, Scan, ScanError
from app.services import analytics


def _make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


class TestAnalytics(unittest.TestCase):
    def setUp(self):
        self.db = _make_session()
        self._seed()

    def tearDown(self):
        self.db.close()

    def _seed(self):
        now = time.time()
        old = now - (200 * 86400)  # 200 days ago

        mount = Mount(
            mount_point="/mnt/test",
            device="/dev/sda1",
            fs_type="ext4",
            is_network_fs=False,
            total_bytes=100 * 1024 ** 3,
            used_bytes=50 * 1024 ** 3,
            free_bytes=50 * 1024 ** 3,
            usage_percent=50.0,
            is_accessible=True,
        )
        self.db.add(mount)
        self.db.flush()

        scan = Scan(
            mount_id=mount.id,
            scan_mode="single_mount",
            status="completed",
            target_path="/mnt/test",
            filesystem_boundary_only=True,
            cold_days=90,
            top_n=100,
            total_files=5,
            total_dirs=2,
            total_bytes=500 * 1024 ** 2,
        )
        self.db.add(scan)
        self.db.flush()

        files = [
            File(scan_id=scan.id, mount_id=mount.id, path="/mnt/test/big.iso",
                 size_bytes=400*1024**2, extension=".iso", atime=now, mtime=now, ctime=now,
                 is_symlink=False, is_cold=False, cold_days_bucket=None),
            File(scan_id=scan.id, mount_id=mount.id, path="/mnt/test/medium.tar",
                 size_bytes=80*1024**2, extension=".tar", atime=now, mtime=now, ctime=now,
                 is_symlink=False, is_cold=False, cold_days_bucket=None),
            File(scan_id=scan.id, mount_id=mount.id, path="/mnt/test/old/cold1.bak",
                 size_bytes=10*1024**2, extension=".bak", atime=old, mtime=old, ctime=old,
                 is_symlink=False, is_cold=True, cold_days_bucket=180),
            File(scan_id=scan.id, mount_id=mount.id, path="/mnt/test/old/cold2.log",
                 size_bytes=5*1024**2, extension=".log", atime=old, mtime=old, ctime=old,
                 is_symlink=False, is_cold=True, cold_days_bucket=180),
            File(scan_id=scan.id, mount_id=mount.id, path="/mnt/test/link",
                 size_bytes=0, extension=None, atime=now, mtime=now, ctime=now,
                 is_symlink=True, is_cold=False, cold_days_bucket=None),
        ]
        for f in files:
            self.db.add(f)

        dirs = [
            Directory(scan_id=scan.id, mount_id=mount.id, path="/mnt/test",
                      total_size_bytes=500*1024**2, file_count=5, cold_file_count=2, cold_total_size_bytes=15*1024**2),
            Directory(scan_id=scan.id, mount_id=mount.id, path="/mnt/test/old",
                      total_size_bytes=15*1024**2, file_count=2, cold_file_count=2, cold_total_size_bytes=15*1024**2),
        ]
        for d in dirs:
            self.db.add(d)

        self.db.commit()
        self.mount_id = mount.id
        self.scan_id = scan.id

    def test_largest_files_excludes_symlinks(self):
        files = analytics.get_largest_files(self.db, self.mount_id, limit=10, scan_id=self.scan_id)
        paths = [f["path"] for f in files]
        self.assertNotIn("/mnt/test/link", paths)

    def test_largest_files_ordered_by_size(self):
        files = analytics.get_largest_files(self.db, self.mount_id, limit=10, scan_id=self.scan_id)
        sizes = [f["size_bytes"] for f in files]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_cold_files_threshold(self):
        result = analytics.get_cold_files(self.db, self.mount_id, days=90, scan_id=self.scan_id)
        self.assertEqual(result["total_count"], 2)
        for f in result["files"]:
            self.assertTrue(f["is_cold"])

    def test_cold_files_high_threshold(self):
        result = analytics.get_cold_files(self.db, self.mount_id, days=365, scan_id=self.scan_id)
        self.assertEqual(result["total_count"], 0)

    def test_extension_summary(self):
        exts = analytics.get_extension_summary(self.db, self.mount_id, scan_id=self.scan_id)
        ext_names = [e["extension"] for e in exts]
        self.assertIn(".iso", ext_names)
        self.assertIn(".tar", ext_names)

    def test_largest_directories(self):
        dirs = analytics.get_largest_directories(self.db, self.mount_id, scan_id=self.scan_id)
        self.assertEqual(dirs[0]["path"], "/mnt/test")  # largest first

    def test_cold_directories(self):
        cold_dirs = analytics.get_cold_directories(self.db, self.mount_id, days=90, scan_id=self.scan_id)
        self.assertTrue(len(cold_dirs) > 0)
        self.assertTrue(all(d["cold_file_count"] > 0 for d in cold_dirs))


if __name__ == "__main__":
    unittest.main()
