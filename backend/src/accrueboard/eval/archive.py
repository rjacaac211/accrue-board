"""Pack and unpack an evaluation's recorded model responses.

The archive is byte-for-byte reproducible (sorted entries, fixed timestamps and permissions), so
re-packing the same recordings does not produce a spurious change in version control.
"""

import gzip
import io
import tarfile
from collections.abc import Iterable
from pathlib import Path


def pack(root: Path, files: Iterable[Path], archive: Path) -> int:
    """Write ``files`` (paths under ``root``) to a gzipped tar. Returns the number packed."""
    paths = sorted({f.resolve() for f in files if f.is_file()})
    base = root.resolve()
    archive.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in paths:
            name = path.relative_to(base).as_posix()
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    with (
        archive.open("wb") as out,
        gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz,
    ):
        gz.write(buffer.getvalue())
    return len(paths)


def unpack(archive: Path, target: Path) -> int:
    """Extract an archive made by ``pack`` into ``target``. Returns the number of files."""
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        tar.extractall(target, members=members, filter="data")
    return len(members)
