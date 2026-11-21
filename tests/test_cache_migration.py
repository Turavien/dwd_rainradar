"""Exercise the on-disk cache migration with real files and directories."""

import errno
import os
import subprocess
import sys
from textwrap import dedent
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.dwd_rainradar import cache_migration as migration


class TestArchiveMigration:
    @pytest.fixture(autouse=True)
    def setup_archive(self, tmp_path: Path) -> None:
        self.config = tmp_path
        self.source = self.config / "dwd_rainradar"
        self.target = self.config / ".cache" / "dwd_rainradar"

    def migrate(self):
        migration.migrate_archive(self.config)

    def test_fresh_install(self):
        self.migrate()
        assert not self.source.exists()
        assert migration.cache_directory(self.config) == self.target

    def test_entire_tree_moves_once(self):
        (self.source / "unknown" / "empty").mkdir(parents=True)
        data = self.source / ".hidden"
        data.write_bytes(b"content")
        inode = data.stat().st_ino
        brands = self.target.parent / "brands"
        brands.mkdir(parents=True)
        self.migrate()
        self.migrate()
        assert not self.source.exists()
        assert (self.target / ".hidden").stat().st_ino == inode
        assert (self.target / "unknown" / "empty").is_dir()
        assert brands.is_dir()

    def test_merge_with_duplicates(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        for directory in (self.source, self.target):
            (directory / "metadata.json").write_bytes(b"{}")
        (self.source / "old").write_bytes(b"1")
        (self.target / "new").write_bytes(b"2")
        self.migrate()
        assert not self.source.exists()
        assert {f.name: f.read_bytes() for f in self.target.iterdir()} == {
            "metadata.json": b"{}",
            "old": b"1",
            "new": b"2",
        }

    def test_conflict_preserves_both_trees(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        (self.source / "other").touch()
        (self.source / "metadata.json").write_bytes(b"aaa")
        (self.target / "metadata.json").write_bytes(b"bbb")
        with pytest.raises(OSError, match="metadata.json"):
            self.migrate()
        assert (self.source / "other").exists()
        assert (self.source / "metadata.json").read_bytes() == b"aaa"
        assert (self.target / "metadata.json").read_bytes() == b"bbb"

    def test_type_conflict(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        (self.source / "rw").mkdir()
        (self.target / "rw").touch()
        with pytest.raises(OSError, match="rw"):
            self.migrate()
        assert (self.source / "rw").is_dir()

    def test_symlink_parent(self):
        self.source.mkdir()
        outside = self.config / "elsewhere"
        outside.mkdir()
        self.target.parent.symlink_to(outside)
        with pytest.raises(OSError):
            self.migrate()
        assert self.source.exists()
        assert list(outside.iterdir()) == []

    def test_permission_failure_keeps_source(self):
        self.source.mkdir()
        with (
            patch.object(Path, "rename", side_effect=PermissionError),
            pytest.raises(PermissionError),
        ):
            self.migrate()
        assert self.source.exists()
        assert not self.target.exists()

    def test_copy_failure_and_retry(self):
        self.source.mkdir()
        data = b"original radar"
        (self.source / "radar").write_bytes(data)
        exdev = OSError(errno.EXDEV, "cross-device")
        link = os.link

        def cross_device_link(src, dst, **kwargs):
            if src == self.source / "radar":
                raise exdev
            return link(src, dst, **kwargs)

        with (
            patch.object(Path, "rename", side_effect=exdev),
            patch.object(migration.os, "link", side_effect=cross_device_link),
        ):
            with (
                patch.object(
                    migration.shutil,
                    "copy2",
                    side_effect=OSError(errno.ENOSPC, "disk full"),
                ),
                pytest.raises(OSError),
            ):
                self.migrate()
            assert (self.source / "radar").read_bytes() == data
            assert list(self.target.iterdir()) == []
            self.migrate()
        assert (self.target / "radar").read_bytes() == data
        assert not self.source.exists()

    def test_retry_after_publication_before_unlink(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        (self.source / "radar").write_bytes(b"original")
        with (
            patch.object(Path, "unlink", side_effect=OSError("interrupted")),
            pytest.raises(OSError),
        ):
            self.migrate()
        assert (self.target / "radar").read_bytes() == b"original"
        assert (self.source / "radar").exists()
        self.migrate()
        assert not self.source.exists()

    def test_nested_symbolic_link_is_preserved_on_failure(self):
        self.source.mkdir()
        link = self.source / "unknown"
        link.symlink_to(self.config / "absent")
        with pytest.raises(OSError, match="Unsupported archive entry"):
            self.migrate()
        assert link.is_symlink()
        assert not self.target.exists()

    def test_directory_alias_is_rejected(self):
        self.source.mkdir()
        with pytest.raises(OSError, match="directories coincide"):
            migration._check(self.source, self.source)
        assert self.source.is_dir()

    def test_merge_detects_late_conflict(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        old = self.source / "radar"
        new = self.target / "radar"
        old.write_bytes(b"original")
        new.write_bytes(b"original")
        migration._check(old, new)
        new.write_bytes(b"another value")
        with pytest.raises(OSError, match="files differ"):
            migration._merge(old, new)
        assert old.read_bytes() == b"original"
        assert new.read_bytes() == b"another value"

    def test_link_io_failure_keeps_original(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        old = self.source / "radar"
        old.write_bytes(b"original")
        with (
            patch.object(migration.os, "link", side_effect=OSError(errno.EIO, "I/O")),
            pytest.raises(OSError),
        ):
            self.migrate()
        assert old.read_bytes() == b"original"
        assert not (self.target / "radar").exists()

    def test_corrupt_cross_device_copy_is_not_published(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        old = self.source / "radar"
        old.write_bytes(b"original")
        with patch.object(
            migration.shutil,
            "copy2",
            side_effect=lambda src, dst: dst.write_bytes(b"truncated"),
        ):
            with pytest.raises(OSError, match="verification failed"):
                migration._copy_across_devices(old, self.target / "radar")
        assert old.read_bytes() == b"original"
        assert list(self.target.iterdir()) == []

    def test_copy_process_exit_and_retry(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        source = self.source / "radar"
        target = self.target / "radar"
        source.write_bytes(b"original")
        child = dedent("""
            import os
            import sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            import cache_migration

            def interrupt_copy(source, temporary):
                temporary.write_bytes(b"partial")
                os._exit(42)

            cache_migration.shutil.copy2 = interrupt_copy
            cache_migration._copy_across_devices(Path(sys.argv[2]), Path(sys.argv[3]))
        """)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                child,
                Path(migration.__file__).parent,
                source,
                target,
            ],
            check=False,
            timeout=30,
        )
        assert result.returncode == 42
        assert source.read_bytes() == b"original"
        assert list(self.target.iterdir()) == []
        self.migrate()
        assert target.read_bytes() == b"original"
        assert not self.source.exists()

    def test_destination_created_while_copying(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        source = self.source / "radar"
        target = self.target / "radar"
        source.write_bytes(b"original")
        copy = migration.shutil.copy2

        def competing_write(src, temporary):
            copy(src, temporary)
            target.write_bytes(b"independent content")

        with patch.object(migration.shutil, "copy2", side_effect=competing_write):
            with pytest.raises(OSError, match="radar") as raised:
                migration._copy_across_devices(source, target)
        assert isinstance(raised.value.__cause__, FileExistsError)
        assert source.read_bytes() == b"original"
        assert target.read_bytes() == b"independent content"
        assert list(self.target.iterdir()) == [target]

    def test_unsupported_copy_target_preserves_source(self):
        self.source.mkdir()
        self.target.mkdir(parents=True)
        source = self.source / "radar"
        target = self.target / "radar"
        source.write_bytes(b"original")
        real_open = os.open

        def open_without_tmpfile(path, flags, *args, **kwargs):
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                raise OSError(errno.EOPNOTSUPP, "unnamed files unsupported")
            return real_open(path, flags, *args, **kwargs)

        with patch.object(os, "open", side_effect=open_without_tmpfile):
            with pytest.raises(OSError, match="unnamed files unsupported") as raised:
                migration._copy_across_devices(source, target)
        assert str(target) in str(raised.value)
        assert source.read_bytes() == b"original"
        assert list(self.target.iterdir()) == []
