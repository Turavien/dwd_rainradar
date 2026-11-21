"""Move the shared radar archive before the first engine starts."""

import errno
import os
from pathlib import Path
import shutil
import stat

ARCHIVE_NAME = "dwd_rainradar"


def cache_directory(config_dir: Path) -> Path:
    return config_dir / ".cache" / ARCHIVE_NAME


def _kind(path: Path) -> int | None:
    try:
        return stat.S_IFMT(path.lstat().st_mode)
    except FileNotFoundError:
        return None


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _identical(source: Path, target: Path) -> bool:
    if source.stat().st_size != target.stat().st_size:
        return False
    with source.open("rb") as left, target.open("rb") as right:
        while True:
            chunk = left.read(1024 * 1024)
            if chunk != right.read(1024 * 1024):
                return False
            if not chunk:
                return True


def _check(source: Path, target: Path) -> None:
    source_kind = _kind(source)
    target_kind = _kind(target)
    if source_kind not in (stat.S_IFREG, stat.S_IFDIR):
        raise OSError(f"Unsupported archive entry: {source}")
    if target_kind is not None and target_kind != source_kind:
        raise OSError(f"Archive entry type conflict: {target}")
    if source_kind == stat.S_IFDIR:
        if target_kind and source.samefile(target):
            raise OSError(f"Source and target directories coincide: {target}")
        for child in source.iterdir():
            _check(child, target / child.name)
    elif target_kind and not _identical(source, target):
        raise OSError(f"Archive files differ: {source} and {target}")


def _copy_across_devices(source: Path, target: Path) -> None:
    """Verify an unnamed destination inode, then publish it without replacement."""
    directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # O_TMPFILE gives the kernel ownership of cleanup, even after SIGKILL.
        # Do not use O_EXCL here: the inode must be linkable after verification.
        fd = os.open(".", os.O_TMPFILE | os.O_RDWR, 0o600, dir_fd=directory_fd)
        try:
            temporary = Path(f"/proc/self/fd/{fd}")
            shutil.copy2(source, temporary)
            os.fsync(fd)
            if not _identical(source, temporary):
                raise OSError(f"Archive copy verification failed: {source}")

            # With dst_dir_fd Python uses linkat and follows the procfs fd link.
            # An existing name causes EEXIST, including one created mid-copy.
            os.link(temporary, target.name, dst_dir_fd=directory_fd)
        finally:
            os.close(fd)
    except OSError as err:
        raise OSError(f"Cannot migrate {source} to {target}: {err}") from err
    finally:
        os.close(directory_fd)


def _merge(source: Path, target: Path) -> None:
    if source.is_dir():
        target.mkdir(exist_ok=True)
        _sync_directory(target.parent)
        for child in source.iterdir():
            _merge(child, target / child.name)
        source.rmdir()
    else:
        if _kind(target) is None:
            try:
                os.link(source, target)
            except OSError as err:
                if err.errno not in (errno.EXDEV, errno.EOPNOTSUPP, errno.EPERM):
                    raise
                _copy_across_devices(source, target)
        elif not _identical(source, target):
            raise OSError(f"Archive files differ: {source} and {target}")
        with target.open("rb") as saved:
            os.fsync(saved.fileno())
        _sync_directory(target.parent)
        source.unlink()
    _sync_directory(source.parent)


def migrate_archive(config_dir: Path) -> None:
    """Migrate all legacy entries or raise before radar access begins."""
    source = config_dir / ARCHIVE_NAME
    target = cache_directory(config_dir)
    for directory in (target.parent, target, source):
        if _kind(directory) not in (None, stat.S_IFDIR):
            raise OSError(f"Expected an archive directory: {directory}")
    if _kind(source) is None:
        return

    _check(source, target)
    target.parent.mkdir(exist_ok=True)
    _sync_directory(config_dir)
    if _kind(target) is None:
        try:
            source.rename(target)
        except OSError as err:
            if err.errno != errno.EXDEV:
                raise
        else:
            _sync_directory(target.parent)
            _sync_directory(config_dir)
            return
    _merge(source, target)
