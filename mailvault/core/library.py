"""Open any supported file (or archive of files) into a Folder tree."""
from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import tarfile
import zipfile

from .mbox import open_mbox
from .mime import parse_eml
from .model import DAMAGED, Folder, Message
from .msg import parse_msg
from .pst import open_pst

MAIL_EXT = (".eml", ".msg", ".mbox", ".mbx", ".pst", ".ost", ".emlx", ".txt")
ARCHIVE_EXT = (".zip", ".tar", ".tgz", ".tar.gz", ".gz", ".tbz2", ".tar.bz2", ".bz2", ".xz",
               ".tar.xz", ".7z", ".rar")
MAX_DEPTH = 4
MAX_ARCHIVE_BYTES = 8 << 30  # zip-bomb guard


def sniff(name: str, data: bytes) -> str:
    n = name.lower()
    if data[:4] == b"!BDN":
        return "pst"
    if data[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
        return "msg"
    if data[:4] == b"PK\x03\x04" or data[:4] == b"PK\x05\x06":
        return "zip"
    if data[:2] == b"\x1f\x8b":
        return "gzip"
    if data[:3] == b"BZh":
        return "bz2"
    if data[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if data[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if data[:4] == b"Rar!":
        return "rar"
    if len(data) > 262 and data[257:262] == b"ustar":
        return "tar"
    if n.endswith(".emlx"):
        return "emlx"
    if data[:5] == b"From " or n.endswith((".mbox", ".mbx")):
        return "mbox"
    head = data[:4096].lower()
    if n.endswith(".eml") or any(h in head for h in (b"\nfrom:", b"\nsubject:", b"\nreceived:",
                                                       b"\nmessage-id:", b"\nmime-version:")) \
            or head.startswith((b"from:", b"received:", b"return-path:", b"subject:",
                                b"delivered-to:", b"x-", b"message-id:", b"date:", b"mime-version:")):
        return "eml"
    return "unknown"


def salvage_text(m: Message, data: bytes):
    """Last-resort recovery: pull readable UTF-16 and ASCII runs out of raw bytes."""
    import re
    runs = [r.decode("utf-16-le", "ignore") for r in re.findall(rb"(?:[\x20-\x7e]\x00){6,}", data)]
    runs += [r.decode("ascii") for r in re.findall(rb"[\x20-\x7e\r\n\t]{12,}", data)]
    seen, keep = set(), []
    for r in runs:
        r = r.strip()
        if r and r not in seen and not r.startswith(("__substg", "Root Entry", "__properties")):
            seen.add(r)
            keep.append(r)
    if keep:
        m.body_text = "[Recovered text fragments]\n\n" + "\n".join(keep)
        m.problems.append(f"Structure unreadable; salvaged {len(keep)} text fragments")


def _damaged_msg(label: str, err: Exception) -> Message:
    m = Message(subject=label, status=DAMAGED)
    m.problems.append(f"{type(err).__name__}: {err}")
    return m


def _single(name: str, loader, fmt: str) -> Folder:
    """Wrap a single-message file as a folder containing one item."""
    f = Folder(name=name, kind="file")

    def load():
        data = loader()
        try:
            if fmt == "msg":
                return parse_msg(data)
            if fmt == "emlx":
                nl = data.find(b"\n")
                ln = int(data[:nl].strip() or 0)
                m = parse_eml(data[nl + 1: nl + 1 + ln] if ln else data[nl + 1:], "eml")
                try:  # Apple Mail keeps flags in a plist after the message
                    import plistlib
                    from .state import from_emlx_flags
                    pl = plistlib.loads(data[nl + 1 + ln:].strip()) if ln else {}
                    if "flags" in pl:
                        m.states = from_emlx_flags(int(pl["flags"]))
                except Exception:  # noqa: BLE001
                    pass
                return m
            return parse_eml(data)
        except Exception as e:  # noqa: BLE001
            m = _damaged_msg(name, e)
            salvage_text(m, data)
            return m
    f.items.append(load)
    return f


def open_bytes(name: str, data: bytes, depth: int = 0) -> Folder:
    fmt = sniff(name, data)
    try:
        if fmt == "pst":
            return open_pst(name, data)
        if fmt == "mbox":
            return open_mbox(name, data)
        if fmt in ("eml", "msg", "emlx"):
            return _single(name, lambda: bytes(data), fmt)
        if fmt in ("zip", "tar", "gzip", "bz2", "xz", "7z", "rar"):
            if depth >= MAX_DEPTH:
                return Folder(name, kind="archive", error="Archive nested too deeply")
            return open_archive(name, data, fmt, depth)
    except Exception as e:  # noqa: BLE001
        f = Folder(name, kind="file", error=f"{type(e).__name__}: {e}")
        f.items.append(lambda e=e: _damaged_msg(name, e))
        return f
    return Folder(name, kind="file", error="Not a recognised mail format")


def _members(name: str, data: bytes, fmt: str):
    """Yield (path, loader) for archive members."""
    if fmt == "zip":
        z = zipfile.ZipFile(io.BytesIO(data))
        total = 0
        for info in z.infolist():
            if info.is_dir():
                continue
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                break
            yield info.filename, (lambda i=info: z.read(i))
    elif fmt == "tar" or (fmt in ("gzip", "bz2", "xz") and _is_tar(data, fmt)):
        mode = {"tar": "r:", "gzip": "r:gz", "bz2": "r:bz2", "xz": "r:xz"}[fmt]
        t = tarfile.open(fileobj=io.BytesIO(data), mode=mode)
        for m in t.getmembers():
            if m.isfile():
                yield m.name, (lambda m=m: t.extractfile(m).read())
    elif fmt in ("gzip", "bz2", "xz"):
        inner = name.rsplit(".", 1)[0] if "." in name else name + ".out"
        dec = {"gzip": gzip.decompress, "bz2": bz2.decompress, "xz": lzma.decompress}[fmt]
        yield inner, (lambda: dec(data))
    elif fmt == "7z":
        try:
            import py7zr  # optional
        except ImportError as e:
            raise RuntimeError("7z support needs the py7zr package") from e
        with py7zr.SevenZipFile(io.BytesIO(data)) as z:
            for path, bio in z.readall().items():
                yield path, (lambda b=bio: b.getvalue())
    elif fmt == "rar":
        try:
            import rarfile  # optional, needs unrar/bsdtar
        except ImportError as e:
            raise RuntimeError("RAR support needs the rarfile package") from e
        r = rarfile.RarFile(io.BytesIO(data))
        for info in r.infolist():
            if not info.is_dir():
                yield info.filename, (lambda i=info: r.read(i))


def _is_tar(data: bytes, fmt: str) -> bool:
    try:
        dec = {"gzip": gzip.GzipFile, "bz2": bz2.BZ2File, "xz": lzma.LZMAFile}[fmt]
        head = dec(io.BytesIO(data)).read(512)
        return head[257:262] == b"ustar"
    except Exception:  # noqa: BLE001
        return False


def open_archive(name: str, data: bytes, fmt: str, depth: int) -> Folder:
    root = Folder(name, kind="archive")
    dirs: dict[str, Folder] = {"": root}

    def folder_for(path: str) -> Folder:
        parent = os.path.dirname(path.rstrip("/"))
        if path in dirs:
            return dirs[path]
        f = Folder(os.path.basename(path), kind="folder")
        folder_for(parent).children.append(f)
        dirs[path] = f
        return f

    loose = Folder("Loose messages", kind="folder")
    for path, loader in _members(name, data, fmt):
        low = path.lower()
        if os.path.basename(path).startswith(".") or "__macosx" in low:
            continue
        d = folder_for(os.path.dirname(path))
        base = os.path.basename(path)
        if low.endswith((".eml", ".msg", ".emlx")):
            # Peek lazily: single-message files just become items
            fmt2 = "msg" if low.endswith(".msg") else ("emlx" if low.endswith(".emlx") else "eml")
            d.items.extend(_single(base, loader, fmt2).items)
            continue
        if low.endswith(MAIL_EXT + ARCHIVE_EXT) or "." not in base:
            try:
                child = open_bytes(base, loader(), depth + 1)
            except Exception as e:  # noqa: BLE001
                child = Folder(base, kind="file", error=str(e))
            if child.error == "Not a recognised mail format":
                continue
            d.children.append(child)
    if loose.items:
        root.children.append(loose)
    _prune(root)
    return root


def _prune(f: Folder) -> bool:
    f.children = [c for c in f.children if _prune(c)]
    return bool(f.items or f.children or f.error)


def open_path(path: str, name: str | None = None) -> Folder:
    """Memory-map the file so multi-GB PST/MBOX files open instantly."""
    import mmap
    fh = open(path, "rb")
    if os.path.getsize(path) == 0:
        fh.close()
        return Folder(name or os.path.basename(path), kind="file", error="Empty file")
    data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    return open_bytes(name or os.path.basename(path), data)
