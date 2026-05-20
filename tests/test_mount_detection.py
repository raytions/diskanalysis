"""Tests for mount detection service."""
import os
import sys
import unittest
from unittest.mock import patch, mock_open

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.mounts import _parse_proc_mounts, _is_network_fs, discover_mounts


FAKE_PROC_MOUNTS = """\
sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
/dev/sda1 / ext4 rw,relatime 0 0
/dev/sda2 /home xfs rw,relatime 0 0
192.168.1.10:/exports/data /mnt/nas1 nfs4 rw,relatime 0 0
//fileserver/share /mnt/smb1 cifs rw,relatime 0 0
tmpfs /tmp tmpfs rw,nosuid,nodev 0 0
"""


class TestParseProcMounts(unittest.TestCase):
    def test_parses_all_lines(self):
        with patch("builtins.open", mock_open(read_data=FAKE_PROC_MOUNTS)):
            mounts = list(_parse_proc_mounts())
        # sysfs, proc, ext4, xfs, nfs4, cifs, tmpfs = 7 entries
        self.assertEqual(len(mounts), 7)

    def test_fields_present(self):
        with patch("builtins.open", mock_open(read_data=FAKE_PROC_MOUNTS)):
            mounts = list(_parse_proc_mounts())
        first = mounts[0]
        self.assertIn("device", first)
        self.assertIn("mount_point", first)
        self.assertIn("fs_type", first)
        self.assertIn("options", first)

    def test_nfs_mount_parsed(self):
        with patch("builtins.open", mock_open(read_data=FAKE_PROC_MOUNTS)):
            mounts = list(_parse_proc_mounts())
        nfs = [m for m in mounts if m["fs_type"] == "nfs4"]
        self.assertEqual(len(nfs), 1)
        self.assertEqual(nfs[0]["mount_point"], "/mnt/nas1")


class TestNetworkFsDetection(unittest.TestCase):
    def test_nfs_is_network(self):
        self.assertTrue(_is_network_fs("nfs"))
        self.assertTrue(_is_network_fs("nfs4"))
        self.assertTrue(_is_network_fs("NFS4"))  # case-insensitive

    def test_cifs_is_network(self):
        self.assertTrue(_is_network_fs("cifs"))
        self.assertTrue(_is_network_fs("smb3"))

    def test_ext4_not_network(self):
        self.assertFalse(_is_network_fs("ext4"))
        self.assertFalse(_is_network_fs("xfs"))
        self.assertFalse(_is_network_fs("btrfs"))


class TestDiscoverMounts(unittest.TestCase):
    def test_filters_pseudo_fs(self):
        with patch("builtins.open", mock_open(read_data=FAKE_PROC_MOUNTS)):
            with patch("os.path.isdir", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("shutil.disk_usage") as mock_du:
                        mock_du.return_value = type("u", (), {"total": 1024**3, "used": 512**3, "free": 512**3})()
                        mounts = discover_mounts()

        # sysfs, proc, tmpfs should be filtered out
        fs_types = {m["fs_type"] for m in mounts}
        self.assertNotIn("sysfs", fs_types)
        self.assertNotIn("proc", fs_types)
        self.assertNotIn("tmpfs", fs_types)

    def test_network_fs_flagged(self):
        with patch("builtins.open", mock_open(read_data=FAKE_PROC_MOUNTS)):
            with patch("os.path.isdir", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("shutil.disk_usage") as mock_du:
                        mock_du.return_value = type("u", (), {"total": 1024**3, "used": 512**3, "free": 512**3})()
                        mounts = discover_mounts()

        nfs_mounts = [m for m in mounts if m["fs_type"] == "nfs4"]
        self.assertTrue(all(m["is_network_fs"] for m in nfs_mounts))

        local_mounts = [m for m in mounts if m["fs_type"] in ("ext4", "xfs")]
        self.assertTrue(all(not m["is_network_fs"] for m in local_mounts))


if __name__ == "__main__":
    unittest.main()
