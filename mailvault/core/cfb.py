"""Compound File Binary (OLE2) reader and writer, used for Outlook .msg files.

The reader tolerates broken FAT chains (stops at the first bad sector and keeps
what it read) so damaged .msg files still yield partial content.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
FREESECT, ENDOFCHAIN, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF


class CFBError(Exception):
    pass


@dataclass
class Entry:
    name: str
    type: int            # 1 storage, 2 stream, 5 root
    left: int
    right: int
    child: int
    start: int
    size: int
    sid: int = 0
    children: dict = field(default_factory=dict)


class CFBReader:
    def __init__(self, data: bytes):
        if data[:8] != MAGIC:
            raise CFBError("Not a compound file")
        self.data = data
        self.warnings: list[str] = []
        (minor, major, bom, ssz, mssz) = struct.unpack_from("<HHHHH", data, 24)
        self.ss = 1 << ssz
        self.mss = 1 << mssz
        (self.n_dir, self.n_fat, self.dir_start, _txn, self.cutoff,
         self.minifat_start, self.n_minifat, self.difat_start, self.n_difat) = \
            struct.unpack_from("<IIIIIIIII", data, 40)
        difat = list(struct.unpack_from("<109I", data, 76))
        sect, guard = self.difat_start, 0
        while sect not in (ENDOFCHAIN, FREESECT) and guard < 100000:
            guard += 1
            blk = self._sector(sect)
            if not blk:
                self.warnings.append("DIFAT chain truncated")
                break
            vals = struct.unpack_from(f"<{self.ss // 4}I", blk)
            difat.extend(vals[:-1])
            sect = vals[-1]
        self.fat: list[int] = []
        for s in difat:
            if s in (FREESECT, ENDOFCHAIN):
                continue
            blk = self._sector(s)
            if len(blk) < self.ss:
                self.warnings.append("FAT sector beyond end of file")
                break
            self.fat.extend(struct.unpack_from(f"<{self.ss // 4}I", blk))
        dir_bytes = self._chain(self.dir_start)
        self.entries: list[Entry] = []
        for i in range(len(dir_bytes) // 128):
            e = dir_bytes[i * 128:(i + 1) * 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            name = e[:max(0, min(nlen, 64) - 2)].decode("utf-16-le", "replace")
            typ = e[66]
            left, right, child = struct.unpack_from("<III", e, 68)
            start, size = struct.unpack_from("<IQ", e, 116)
            if self.ss == 512:
                size &= 0xFFFFFFFF
            self.entries.append(Entry(name, typ, left, right, child, start, size, i))
        if not self.entries:
            raise CFBError("Empty directory")
        root = self.entries[0]
        self.ministream = self._chain(root.start, root.size) if root.start != ENDOFCHAIN else b""
        mf = self._chain(self.minifat_start) if self.n_minifat else b""
        self.minifat = list(struct.unpack_from(f"<{len(mf) // 4}I", mf)) if mf else []
        self._link(0, set())

    def _sector(self, s: int) -> bytes:
        off = (s + 1) * self.ss
        return self.data[off: off + self.ss]

    def _chain(self, start: int, size: int | None = None) -> bytes:
        out, s, seen = [], start, set()
        while s not in (ENDOFCHAIN, FREESECT) and s < len(self.fat) + 1:
            if s in seen:
                self.warnings.append("Loop in FAT chain")
                break
            seen.add(s)
            blk = self._sector(s)
            if not blk:
                self.warnings.append("FAT chain points past end of file")
                break
            out.append(blk)
            if size is not None and len(out) * self.ss >= size:
                break
            s = self.fat[s] if s < len(self.fat) else ENDOFCHAIN
        b = b"".join(out)
        return b[:size] if size is not None else b

    def _minichain(self, start: int, size: int) -> bytes:
        out, s, seen = [], start, set()
        while s not in (ENDOFCHAIN, FREESECT) and len(out) * self.mss < size:
            if s in seen or s >= len(self.minifat):
                self.warnings.append("Broken mini FAT chain")
                break
            seen.add(s)
            out.append(self.ministream[s * self.mss:(s + 1) * self.mss])
            s = self.minifat[s]
        return b"".join(out)[:size]

    def _link(self, sid: int, seen: set):
        e = self.entries[sid]
        if e.child == NOSTREAM:
            return
        stack = [e.child]
        while stack:
            c = stack.pop()
            if c == NOSTREAM or c >= len(self.entries) or c in seen:
                continue
            seen.add(c)
            ce = self.entries[c]
            e.children[ce.name.upper()] = ce
            stack += [ce.left, ce.right]
            if ce.type in (1, 5):
                self._link(c, seen)

    def read(self, entry: Entry) -> bytes:
        if entry.size < self.cutoff and entry.type == 2:
            return self._minichain(entry.start, entry.size)
        return self._chain(entry.start, entry.size)

    @property
    def root(self) -> Entry:
        return self.entries[0]


# ---------------------------------------------------------------- writer
class _Node:
    def __init__(self, name, data=None):
        self.name, self.data, self.children = name, data, []  # data None => storage


def _cmp_key(name: str):
    return (len(name), name.upper())


class CFBWriter:
    """Minimal v3 (512-byte sector) writer with proper mini-stream handling."""

    def __init__(self):
        self.root = _Node("Root Entry")

    def storage(self, path: str) -> _Node:
        node = self.root
        for part in [p for p in path.split("/") if p]:
            nxt = next((c for c in node.children if c.name == part and c.data is None), None)
            if not nxt:
                nxt = _Node(part)
                node.children.append(nxt)
            node = nxt
        return node

    def add(self, path: str, data: bytes):
        parent, _, name = path.rpartition("/")
        self.storage(parent).children.append(_Node(name, data))

    def tobytes(self) -> bytes:
        SS, MSS, CUT = 512, 64, 4096
        nodes = []

        def collect(n):
            nodes.append(n)
            for c in n.children:
                collect(c)
        collect(self.root)
        sid = {id(n): i for i, n in enumerate(nodes)}

        # mini stream
        mini = bytearray()
        minifat: list[int] = []
        big: list[tuple[_Node, bytes]] = []
        starts = {}
        for n in nodes[1:]:
            if n.data is None:
                continue
            if len(n.data) < CUT:
                if not n.data:
                    starts[id(n)] = ENDOFCHAIN
                    continue
                first = len(minifat)
                cnt = (len(n.data) + MSS - 1) // MSS
                minifat += [first + i + 1 for i in range(cnt - 1)] + [ENDOFCHAIN]
                mini += n.data + b"\0" * (cnt * MSS - len(n.data))
                starts[id(n)] = first
            else:
                big.append((n, n.data))

        sectors: list[bytes] = []
        fat: list[int] = []

        def put(data: bytes) -> int:
            if not data:
                return ENDOFCHAIN
            first = len(sectors)
            cnt = (len(data) + SS - 1) // SS
            for i in range(cnt):
                sectors.append(data[i * SS:(i + 1) * SS].ljust(SS, b"\0"))
                fat.append(first + i + 1 if i < cnt - 1 else ENDOFCHAIN)
            return first

        for n, d in big:
            starts[id(n)] = put(d)
        mini_start = put(bytes(mini))
        mf_bytes = struct.pack(f"<{len(minifat)}I", *minifat) if minifat else b""
        if mf_bytes:
            mf_bytes += struct.pack("<I", FREESECT) * ((-len(mf_bytes) // 4) % (SS // 4))
        minifat_start = put(mf_bytes)

        # directory entries
        def tree(children):
            items = sorted(children, key=lambda c: _cmp_key(c.name))
            links = {}

            def build(lo, hi):
                if lo > hi:
                    return NOSTREAM
                mid = (lo + hi) // 2
                links[id(items[mid])] = (build(lo, mid - 1), build(mid + 1, hi))
                return sid[id(items[mid])]
            return build(0, len(items) - 1), links

        lr: dict = {}
        childof: dict = {}
        for n in nodes:
            if n.data is None:
                top, links = tree(n.children)
                childof[id(n)] = top
                lr.update(links)
        d = bytearray()
        for n in nodes:
            nm = n.name.encode("utf-16-le")[:62] + b"\0\0"
            e = bytearray(128)
            e[:len(nm)] = nm
            struct.pack_into("<H", e, 64, len(nm))
            is_root = n is self.root
            e[66] = 5 if is_root else (1 if n.data is None else 2)
            e[67] = 1  # black
            left, right = lr.get(id(n), (NOSTREAM, NOSTREAM))
            struct.pack_into("<III", e, 68, left, right, childof.get(id(n), NOSTREAM))
            if is_root:
                struct.pack_into("<IQ", e, 116, mini_start, len(mini))
            elif n.data is not None:
                struct.pack_into("<IQ", e, 116, starts[id(n)], len(n.data))
            else:
                struct.pack_into("<IQ", e, 116, 0, 0)
            d += e
        while len(d) % SS:
            d += struct.pack("<64sHBBIII16sIQQIQ", b"", 0, 0, 0, NOSTREAM, NOSTREAM, NOSTREAM,
                             b"", 0, 0, 0, 0, 0)
        dir_start = put(bytes(d))

        # FAT sectors (they must describe themselves too)
        n_fat = 1
        while (len(fat) + n_fat) > n_fat * (SS // 4):
            n_fat += 1
        if n_fat > 109:
            raise CFBError("File too large for this simple writer (>~7MB)")
        fat_first = len(sectors)
        fat += [FATSECT] * n_fat
        fat += [FREESECT] * (n_fat * (SS // 4) - len(fat))
        fat_bytes = struct.pack(f"<{len(fat)}I", *fat)
        for i in range(n_fat):
            sectors.append(fat_bytes[i * SS:(i + 1) * SS])

        hdr = bytearray(512)
        hdr[:8] = MAGIC
        struct.pack_into("<HHHHH", hdr, 24, 0x3E, 3, 0xFFFE, 9, 6)
        struct.pack_into("<IIIIIIIII", hdr, 40, 0, n_fat, dir_start, 0, CUT,
                         minifat_start if minifat else ENDOFCHAIN, len(mf_bytes) // SS,
                         ENDOFCHAIN, 0)
        difat = [fat_first + i for i in range(n_fat)] + [FREESECT] * (109 - n_fat)
        struct.pack_into("<109I", hdr, 76, *difat)
        return bytes(hdr) + b"".join(sectors)
