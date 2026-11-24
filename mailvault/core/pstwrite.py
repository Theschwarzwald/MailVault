"""Unicode PST writer (MS-PST): create Outlook data files from any source.

Layout follows the MS-PST spec:
  * 564-byte HEADER with partial/full CRCs, ROOT, rgnid counters
  * DList page at 0x4200; an AMap every 253,952 bytes from 0x4400, a PMap in
    every 8th region, FMap/FPMap pages at their periodic slots (Outlook uses
    the AMaps and DList, the deprecated maps are kept for compatibility)
  * blocks (64-byte aligned, permute "compressible" encryption, CRC +
    signature trailers), XBLOCK/XXBLOCK data trees, SLBLOCK/SIBLOCK subnodes
  * node and block B-trees with intermediate levels
  * LTP: multi-block heaps (HNHDR/HNPAGEHDR/HNBITMAPHDR), multi-level BTHs,
    property contexts and table contexts (rows split across blocks)
  * messaging: message store, named-property map with hash buckets, the
    mandatory folders, search objects and table templates, folders with
    hierarchy/contents/FAI tables, messages with recipient and attachment
    tables, attachments, and embedded messages.

Messages are written as they arrive, so memory stays small for big exports.
"""
from __future__ import annotations

import datetime as dt
import os
import struct
import uuid

from .mapi import PS_MAPI, PS_PUBLIC_STRINGS, to_filetime
from .model import Message
from .propmap import (BIN, BOOL, CURRENCY, DOUBLE, ERROR, I2, I8, LONG, MV, OBJECT, R4, STR8, TIME, UNI,
                      APPTIME, GUID, Namer, attachment_props, message_props, recipient_rows)
from .pst import _block_sig, encrypt_permute, pst_crc

PAGE = 512
REGION = 496 * 8 * 64            # 253,952 bytes covered by each AMap
FIRST_AMAP = 0x4400
RESERVED = 0x800                 # AMap + PMap + FMap + FPMap slots at each region start
MAX_DATA = 8192 - 16             # 8176 bytes of data per block
MAX_ALLOC = 3580                 # largest heap allocation
XB_MAX = (MAX_DATA - 8) // 8     # 1021 BIDs per XBLOCK

# Node IDs
NID_MESSAGE_STORE, NID_NAME_TO_ID_MAP, NID_ROOT_FOLDER = 0x21, 0x61, 0x122
NID_SMQ, NID_SAL = 0x1E1, 0x201
NID_HIER_TEMPLATE, NID_CONTENTS_TEMPLATE, NID_ASSOC_TEMPLATE, NID_SEARCH_TEMPLATE = 0x60D, 0x60E, 0x60F, 0x610
NID_ATTACH_TABLE, NID_RECIP_TABLE = 0x671, 0x692
T_FOLDER, T_SEARCH_FOLDER, T_MESSAGE, T_ATTACHMENT, T_SUQ = 0x02, 0x03, 0x04, 0x05, 0x06
T_HIER, T_CONTENTS, T_ASSOC, T_SEARCH_CONTENTS, T_LTP = 0x0D, 0x0E, 0x0F, 0x10, 0x1F

FIXED = {I2: 2, LONG: 4, R4: 4, ERROR: 4, BOOL: 1, DOUBLE: 8, CURRENCY: 8, APPTIME: 8, I8: 8, TIME: 8}

# Table column sets (MS-PST 2.4.4.4.1, 2.4.4.5.1, 2.4.4.6.1, 2.4.5.3.1, 2.4.6.1.1)
HIER_COLS = [(0x0E30, LONG), (0x0E33, I8), (0x0E34, BIN), (0x0E38, LONG), (0x3001, UNI), (0x3602, LONG),
             (0x3603, LONG), (0x360A, BOOL), (0x3613, UNI), (0x6635, LONG), (0x6636, LONG)]
CONTENTS_COLS = [(0x0017, LONG), (0x001A, UNI), (0x0036, LONG), (0x0037, UNI), (0x0039, TIME), (0x0042, UNI),
                 (0x0057, BOOL), (0x0058, BOOL), (0x0070, UNI), (0x0071, BIN), (0x0E03, UNI), (0x0E04, UNI),
                 (0x0E06, TIME), (0x0E07, LONG), (0x0E08, LONG), (0x0E17, LONG), (0x0E30, LONG), (0x0E33, I8),
                 (0x0E34, BIN), (0x0E38, LONG), (0x0E3C, BIN), (0x0E3D, BIN), (0x1097, LONG), (0x3008, TIME),
                 (0x65C6, LONG)]
ASSOC_COLS = [(0x001A, UNI), (0x0E07, LONG), (0x0E17, LONG), (0x3001, UNI), (0x6800, UNI), (0x6803, BOOL),
              (0x6805, MV | LONG), (0x7003, LONG), (0x7004, BIN), (0x7005, BIN), (0x7006, UNI), (0x7007, LONG)]
SEARCH_COLS = CONTENTS_COLS + [(0x0E05, UNI), (0x6834, LONG)]
RECIP_COLS = [(0x0C15, LONG), (0x0E0F, BOOL), (0x0FF9, BIN), (0x0FFE, LONG), (0x0FFF, BIN), (0x3001, UNI),
              (0x3002, UNI), (0x3003, UNI), (0x300B, BIN), (0x3900, LONG), (0x39FF, UNI), (0x3A40, BOOL),
              (0x39FE, UNI)]
ATTACH_COLS = [(0x0E20, LONG), (0x3704, UNI), (0x3705, LONG), (0x370B, LONG)]

CONTAINER = {"mail": "IPF.Note", "appointment": "IPF.Appointment", "contact": "IPF.Contact",
             "task": "IPF.Task", "note": "IPF.StickyNote"}


def _align(n: int, a: int) -> int:
    return (n + a - 1) & ~(a - 1)


ANSI_CODEPAGES = [(1252, "cp1252"), (1250, "cp1250"), (1251, "cp1251"), (1253, "cp1253"), (1254, "cp1254"),
                  (1257, "cp1257"), (1255, "cp1255"), (1256, "cp1256"), (874, "cp874"), (1258, "cp1258"),
                  (932, "cp932"), (936, "gbk"), (949, "cp949"), (950, "big5")]


def pick_codepage(*texts) -> tuple[int, str]:
    """First Windows code page that can hold all of the text (ANSI PSTs store 8-bit strings)."""
    blob = "".join(t for t in texts if isinstance(t, str))
    for cpid, name in ANSI_CODEPAGES:
        try:
            blob.encode(name)
            return cpid, name
        except UnicodeEncodeError:
            continue
    return 1252, "cp1252"


def encode_value(t: int, v, cp: str = "cp1252") -> bytes:
    """Binary form of a property value (as stored in heap/subnode)."""
    if t & MV:
        base = t & ~MV
        vals = v if isinstance(v, (list, tuple)) else [v]
        if base in FIXED:
            return b"".join(encode_value(base, x) for x in vals)
        parts = [encode_value(base, x, cp) for x in vals]
        offs, pos = [], 4 + 4 * len(parts)
        for x in parts:
            offs.append(pos)
            pos += len(x)
        return struct.pack(f"<I{len(parts)}I", len(parts), *offs) + b"".join(parts)
    if t == UNI:
        return str(v).encode("utf-16-le")
    if t == STR8:
        return (v if isinstance(v, bytes) else str(v).encode(cp, "replace"))
    if t == TIME:
        return struct.pack("<Q", to_filetime(v) if isinstance(v, dt.datetime) else int(v or 0))
    if t in (I8, CURRENCY):
        return struct.pack("<q", int(v))
    if t in (DOUBLE, APPTIME):
        return struct.pack("<d", float(v))
    if t == R4:
        return struct.pack("<f", float(v))
    if t in (LONG, ERROR):
        return struct.pack("<i" if int(v) < 0 else "<I", int(v))
    if t == I2:
        return struct.pack("<h", int(v))
    if t == BOOL:
        return b"\x01" if v else b"\x00"
    if t == GUID:
        return v if isinstance(v, bytes) else uuid.UUID(str(v)).bytes_le
    return bytes(v) if not isinstance(v, str) else v.encode("utf-8")


# ======================================================================= NDB
class NDB:
    def __init__(self, path: str, ansi: bool = False):
        self.ansi = ansi
        self.tl = 12 if ansi else 16                  # block/page trailer size
        self.max_data = 8192 - self.tl                # 8180 (ANSI) / 8176 (Unicode)
        self.bf = "I" if ansi else "Q"                # BID/IB field width
        self.xb_max = (self.max_data - 8) // (4 if ansi else 8)
        self.f = open(path, "w+b")
        self.f.truncate(FIRST_AMAP)
        self.cursor = FIRST_AMAP + RESERVED
        self.extents: list[tuple[int, int]] = []
        self.bbt: list[tuple[int, int, int]] = []     # bid, ib, cb
        self.nbt: dict[int, tuple[int, int, int]] = {}
        self.next_bid = 4
        self.next_pbid = 1
        self.max_nid: dict[int, int] = {}

    # -------------------------------------------------- space
    def alloc(self, size: int, align: int) -> int:
        pos = _align(self.cursor, align)
        region = (pos - FIRST_AMAP) // REGION
        start = FIRST_AMAP + region * REGION
        if pos < start + RESERVED:
            pos = start + RESERVED
        if pos + size > start + REGION:
            pos = start + REGION + RESERVED
        self.cursor = pos + size
        self.extents.append((pos, size))
        return pos

    def write_at(self, ib: int, data: bytes):
        self.f.seek(ib)
        self.f.write(data)

    # -------------------------------------------------- blocks
    def block(self, data: bytes, internal: bool = False) -> int:
        if len(data) > self.max_data:
            raise ValueError("block too large")
        bid = self.next_bid | (2 if internal else 0)
        self.next_bid += 4
        stored = data if internal else encrypt_permute(data)
        size = _align(len(stored) + self.tl, 64)
        ib = self.alloc(size, 64)
        if self.ansi and ib + size > 0x7FF00000:
            raise ValueError("ANSI PST files are limited to 2 GB; export as Unicode PST instead")
        buf = bytearray(size)
        buf[:len(stored)] = stored
        sig, crc = _block_sig(ib, bid), pst_crc(stored)
        if self.ansi:
            struct.pack_into("<HHII", buf, size - 12, len(stored), sig, bid, crc)
        else:
            struct.pack_into("<HHIQ", buf, size - 16, len(stored), sig, crc, bid)
        self.write_at(ib, bytes(buf))
        self.bbt.append((bid, ib, len(stored)))
        return bid

    def data_tree(self, data: bytes) -> int:
        """Store data of any size: one block, XBLOCK, or XXBLOCK."""
        md = self.max_data
        if len(data) <= md:
            return self.block(data)
        bids = [self.block(data[i:i + md]) for i in range(0, len(data), md)]
        return self._xtree(bids, len(data), [min(md, len(data) - i) for i in range(0, len(data), md)])

    def blocks_tree(self, chunks: list[bytes]) -> int:
        """Store pre-split blocks (heap pages, table rows) as one data tree."""
        if len(chunks) == 1:
            return self.block(chunks[0])
        bids = [self.block(c) for c in chunks]
        return self._xtree(bids, sum(map(len, chunks)), [len(c) for c in chunks])

    def _xtree(self, bids: list[int], total: int, sizes: list[int]) -> int:
        f, xm = "<" + self.bf, self.xb_max
        if len(bids) <= xm:
            return self.block(struct.pack("<BBHI", 1, 1, len(bids), total) +
                              b"".join(struct.pack(f, b) for b in bids), internal=True)
        xbs = []
        for i in range(0, len(bids), xm):
            part = bids[i:i + xm]
            sz = sum(sizes[i:i + xm])
            xbs.append(self.block(struct.pack("<BBHI", 1, 1, len(part), sz) +
                                  b"".join(struct.pack(f, b) for b in part), internal=True))
        if len(xbs) > xm:
            raise ValueError("item too large")
        return self.block(struct.pack("<BBHI", 1, 2, len(xbs), total) +
                          b"".join(struct.pack(f, b) for b in xbs), internal=True)

    def subnodes(self, subs: dict[int, tuple[int, int]]) -> int:
        if not subs:
            return 0
        items = sorted(subs.items())
        hdr = (lambda lvl, n: struct.pack("<BBH", 2, lvl, n)) if self.ansi else \
            (lambda lvl, n: struct.pack("<BBHI", 2, lvl, n, 0))
        ent, sient = ("<III", "<II") if self.ansi else ("<QQQ", "<QQ")
        per = (self.max_data - 8) // struct.calcsize(ent)
        leaves = []
        for i in range(0, len(items), per):
            chunk = items[i:i + per]
            body = hdr(0, len(chunk)) + b"".join(struct.pack(ent, nid, bd, bs) for nid, (bd, bs) in chunk)
            leaves.append((chunk[0][0], self.block(body, internal=True)))
        if len(leaves) == 1:
            return leaves[0][1]
        if len(leaves) > (self.max_data - 8) // struct.calcsize(sient):
            raise ValueError("too many subnodes")
        return self.block(hdr(1, len(leaves)) + b"".join(struct.pack(sient, nid, b) for nid, b in leaves),
                          internal=True)

    def node(self, nid: int, bid_data: int, bid_sub: int, parent: int):
        self.nbt[nid] = (bid_data, bid_sub, parent)
        t, idx = nid & 0x1F, nid >> 5
        self.max_nid[t] = max(self.max_nid.get(t, 0), idx)

    # -------------------------------------------------- B-trees and maps
    def _trailer(self, pg: bytearray, ptype: int, sig: int, bid: int):
        if self.ansi:
            struct.pack_into("<BBHII", pg, 500, ptype, ptype, sig, bid, pst_crc(bytes(pg[:500])))
        else:
            struct.pack_into("<BBHIQ", pg, 496, ptype, ptype, sig, pst_crc(bytes(pg[:496])), bid)

    @property
    def area(self) -> int:
        return 496 if self.ansi else 488

    def _page(self, entries: list[bytes], ptype: int, cb_ent: int, level: int) -> tuple[int, int]:
        pg = bytearray(PAGE)
        for i, e in enumerate(entries):
            pg[i * cb_ent:(i + 1) * cb_ent] = e
        a = self.area
        pg[a:a + 4] = bytes([len(entries), a // cb_ent, cb_ent, level])
        ib = self.alloc(PAGE, PAGE)
        bid = self.next_pbid
        self.next_pbid += 1
        self._trailer(pg, ptype, _block_sig(ib, bid), bid)
        self.write_at(ib, bytes(pg))
        return bid, ib

    def _btree(self, entries: list[tuple[int, bytes]], ptype: int, cb_ent: int) -> tuple[int, int]:
        per = self.area // cb_ent
        icb, ifmt = (12, "<III") if self.ansi else (24, "<QQQ")
        level, items = 0, []
        entries = entries or []
        for i in range(0, max(len(entries), 1), per):
            chunk = entries[i:i + per]
            bid, ib = self._page([e for _, e in chunk], ptype, cb_ent, 0)
            items.append((chunk[0][0] if chunk else 0, bid, ib))
        while len(items) > 1:
            level += 1
            nxt = []
            iper = self.area // icb
            for i in range(0, len(items), iper):
                chunk = items[i:i + iper]
                bid, ib = self._page([struct.pack(ifmt, k, b, o) for k, b, o in chunk], ptype, icb, level)
                nxt.append((chunk[0][0], bid, ib))
            items = nxt
        return items[0][1], items[0][2]

    def finish(self, header_fields: dict):
        if self.ansi:
            nbt_entries = [(nid, struct.pack("<IIII", nid, bd, bs, par)) for nid, (bd, bs, par) in sorted(self.nbt.items())]
            bbt_entries = [(bid, struct.pack("<IIHH", bid, ib, cb, 2)) for bid, ib, cb in sorted(self.bbt)]
            nbt_ref, bbt_ref = self._btree(nbt_entries, 0x81, 16), self._btree(bbt_entries, 0x80, 12)
        else:
            nbt_entries = [(nid, struct.pack("<QQQII", nid, bd, bs, par, 0))
                           for nid, (bd, bs, par) in sorted(self.nbt.items())]
            bbt_entries = [(bid, struct.pack("<QQHHI", bid, ib, cb, 2, 0)) for bid, ib, cb in sorted(self.bbt)]
            nbt_ref, bbt_ref = self._btree(nbt_entries, 0x81, 32), self._btree(bbt_entries, 0x80, 24)
        # whole AMap regions, like Outlook
        regions = (self.cursor - FIRST_AMAP + REGION - 1) // REGION
        eof = FIRST_AMAP + regions * REGION
        self.f.truncate(eof)
        free = self._maps(regions)
        if self.ansi:
            self._header_ansi(header_fields, nbt_ref, bbt_ref, eof, FIRST_AMAP + (regions - 1) * REGION, free)
        else:
            self._dlist()
            self._header(header_fields, nbt_ref, bbt_ref, eof, FIRST_AMAP + (regions - 1) * REGION, free)
        self.f.close()

    def _maps(self, regions: int) -> int:
        nslots = regions * 496 * 8
        bits = bytearray(regions * 496)

        def mark(a, b):        # slots [a, b), MSB-first
            while a < b and a % 8:
                bits[a // 8] |= 0x80 >> (a % 8)
                a += 1
            full = (b - a) // 8
            if full > 0:
                bits[a // 8:a // 8 + full] = b"\xff" * full
                a += full * 8
            while a < b:
                bits[a // 8] |= 0x80 >> (a % 8)
                a += 1
        spans = sorted([(FIRST_AMAP + r * REGION, RESERVED) for r in range(regions)] + self.extents)
        cur_a = cur_b = None
        for ib, size in spans:
            a, b = (ib - FIRST_AMAP) // 64, min((ib + size - FIRST_AMAP + 63) // 64, nslots)
            if cur_a is None:
                cur_a, cur_b = a, b
            elif a <= cur_b:
                cur_b = max(cur_b, b)
            else:
                mark(cur_a, cur_b)
                cur_a, cur_b = a, b
        if cur_a is not None:
            mark(cur_a, cur_b)
        free = 0
        for r in range(regions):
            base = FIRST_AMAP + r * REGION
            page = bytes(bits[r * 496:(r + 1) * 496])
            free += (496 * 8 - sum(bin(x).count("1") for x in page)) * 64
            self._map_page(base, 0x84, page)
            if r % 8 == 0:
                self._map_page(base + 0x200, 0x83, b"\xff" * 496)
            if r >= 128 and (r - 128) % 496 == 0:
                self._map_page(base + 0x400, 0x82, b"\x00" * 496)
            if r >= 8192 and (r - 8192) % (496 * 8 * 8) == 0:
                self._map_page(base + 0x600, 0x85, b"\x00" * 496)
        return free

    def _map_page(self, ib: int, ptype: int, body: bytes):
        pg = bytearray(PAGE)
        if self.ansi:              # ANSI map pages start with 4 bytes of padding
            pg[4:500] = body
        else:
            pg[:496] = body
        self._trailer(pg, ptype, 0, ib)
        self.write_at(ib, bytes(pg))

    def _dlist(self):
        pg = bytearray(PAGE)       # empty density list: Outlook rebuilds it
        bid = self.next_pbid
        self.next_pbid += 1
        struct.pack_into("<BBHIQ", pg, 496, 0x86, 0x86, _block_sig(0x4200, bid), pst_crc(bytes(pg[:496])), bid)
        self.write_at(0x4200, bytes(pg))

    def _rgnid(self) -> list[int]:
        rg = [0x400] * 32
        for t, idx in self.max_nid.items():
            rg[t] = max(rg[t], idx + 1)
        rg[T_MESSAGE] = max(rg[T_MESSAGE], 0x10000)
        return [(i << 5) | t for t, i in enumerate(rg)]

    def _header_ansi(self, h: dict, nbt, bbt, eof, amap_last, free):
        b = bytearray(512)
        b[0:4] = b"!BDN"
        b[8:10] = b"SM"
        struct.pack_into("<HHBB", b, 10, 14, 19, 1, 1)
        struct.pack_into("<III", b, 24, self.next_bid, self.next_pbid, h.get("unique", 1))
        struct.pack_into("<32I", b, 36, *self._rgnid())
        struct.pack_into("<IIIIIIIIIBBH", b, 164, 0, eof, amap_last, free, 0, nbt[0], nbt[1], bbt[0], bbt[1],
                         2, 0, 0)
        b[204:332] = b"\xff" * 128
        b[332:460] = b"\xff" * 128
        b[460] = 0x80
        b[461] = 1
        struct.pack_into("<I", b, 4, pst_crc(bytes(b[8:8 + 471])))
        self.write_at(0, bytes(b))

    def _header(self, h: dict, nbt, bbt, eof, amap_last, free):
        b = bytearray(564)
        b[0:4] = b"!BDN"
        b[8:10] = b"SM"
        struct.pack_into("<HHBB", b, 10, 23, 19, 1, 1)
        struct.pack_into("<QQI", b, 24, 0, self.next_pbid, h.get("unique", 1))
        struct.pack_into("<32I", b, 44, *self._rgnid())   # next NID per type
        # ROOT
        struct.pack_into("<IQQQQQQQQBBH", b, 180, 0, eof, amap_last, free, 0, nbt[0], nbt[1], bbt[0], bbt[1],
                         2, 0, 0)
        b[256:384] = b"\xff" * 128   # rgbFM (deprecated)
        b[384:512] = b"\xff" * 128   # rgbFP (deprecated)
        b[512] = 0x80                # bSentinel
        b[513] = 1                   # NDB_CRYPT_PERMUTE
        struct.pack_into("<Q", b, 516, self.next_bid)
        struct.pack_into("<I", b, 4, pst_crc(bytes(b[8:8 + 471])))
        struct.pack_into("<I", b, 524, pst_crc(bytes(b[8:8 + 516])))
        self.write_at(0, bytes(b))


# ======================================================================= LTP
class Heap:
    """Heap-on-node builder with multi-block support."""

    def __init__(self, client_sig: int, max_data: int = MAX_DATA):
        self.sig = client_sig
        self.max_data = max_data
        self.blocks: list[list[bytes]] = [[]]

    def _header_len(self, i: int) -> int:
        if i == 0:
            return 12
        return 66 if (i - 8) % 128 == 0 and i >= 8 else 2

    def _used(self, i: int, extra: int = 0, extra_n: int = 0) -> int:
        allocs = self.blocks[i]
        n = len(allocs) + extra_n
        body = self._header_len(i) + sum(map(len, allocs)) + extra
        return _align(body, 2) + 4 + 2 * (n + 1)

    def alloc(self, data: bytes) -> int:
        if len(data) > MAX_ALLOC:
            raise ValueError("heap allocation too large")
        i = len(self.blocks) - 1
        if self._used(i, len(data), 1) > self.max_data or len(self.blocks[i]) >= 2047:
            self.blocks.append([])
            i += 1
        self.blocks[i].append(data)
        return (i << 16) | (len(self.blocks[i]) << 5)

    @staticmethod
    def _fill(free: int) -> int:
        for lvl, lim in enumerate((3584, 2560, 2048, 1792, 1536, 1280, 1024, 768, 512, 256, 128, 64, 32, 16, 8)):
            if free >= lim:
                return lvl
        return 15

    def build(self, user_root: int) -> list[bytes]:
        out = []
        fills = [self._fill(self.max_data - self._used(i)) for i in range(len(self.blocks))]
        for i, allocs in enumerate(self.blocks):
            hl = self._header_len(i)
            body = bytearray(hl)
            offs = []
            for a in allocs:
                offs.append(len(body))
                body += a
            offs.append(len(body))
            if len(body) % 2:
                body += b"\0"
            ibm = len(body)
            body += struct.pack("<HH", len(allocs), 0) + b"".join(struct.pack("<H", o) for o in offs)
            struct.pack_into("<H", body, 0, ibm)
            if i == 0:
                nib = 0
                for k in range(min(8, len(fills))):
                    nib |= fills[k] << (4 * k)
                struct.pack_into("<BBII", body, 2, 0xEC, self.sig, user_root, nib)
            elif hl == 66:
                fl = bytearray(64)
                for k in range(128):
                    j = i + k
                    if j < len(fills):
                        fl[k // 2] |= fills[j] << (4 * (k % 2))
                body[2:66] = fl
            out.append(bytes(body))
        return out


def bth(heap: Heap, cb_key: int, cb_ent: int, records: list[tuple[bytes, bytes]]) -> int:
    """Build a (multi-level) BTH; returns the HID of its header."""
    records = sorted(records, key=lambda r: r[0][::-1])     # little-endian keys: compare as integers
    if not records:
        return heap.alloc(struct.pack("<BBBBI", 0xB5, cb_key, cb_ent, 0, 0))
    level = 0
    per = MAX_ALLOC // (cb_key + cb_ent)
    nodes = []
    for i in range(0, len(records), per):
        chunk = records[i:i + per]
        nodes.append((chunk[0][0], heap.alloc(b"".join(k + v for k, v in chunk))))
    while len(nodes) > 1:
        level += 1
        per = MAX_ALLOC // (cb_key + 4)
        nxt = []
        for i in range(0, len(nodes), per):
            chunk = nodes[i:i + per]
            nxt.append((chunk[0][0], heap.alloc(b"".join(k + struct.pack("<I", h) for k, h in chunk))))
        nodes = nxt
    return heap.alloc(struct.pack("<BBBBI", 0xB5, cb_key, cb_ent, level, nodes[0][1]))


class NodeBuilder:
    """Collects one node's heap plus its subnodes."""

    def __init__(self, ndb: NDB, client_sig: int):
        self.ndb = ndb
        self.heap = Heap(client_sig, ndb.max_data)
        self.subs: dict[int, tuple[int, int]] = {}
        self._next_sub = 1

    def sub_nid(self, t: int = T_LTP) -> int:
        nid = (self._next_sub << 5) | t
        self._next_sub += 1
        return nid

    def value_ref(self, data: bytes) -> int:
        """HNID for a variable-size value: heap allocation, or subnode when large."""
        if len(data) <= MAX_ALLOC:
            return self.heap.alloc(data)
        nid = self.sub_nid()
        self.subs[nid] = (self.ndb.data_tree(data), 0)
        return nid

    def commit(self, user_root: int) -> tuple[int, int]:
        bd = self.ndb.blocks_tree(self.heap.build(user_root))
        return bd, self.ndb.subnodes(self.subs)


def _ansi_type(t: int) -> int:
    return STR8 if t == UNI else (MV | STR8 if t == MV | UNI else t)


def write_pc(ndb: NDB, props: dict, objects: dict | None = None, subs: dict | None = None,
             cp: str = "cp1252") -> tuple[int, int]:
    """props: pid -> (ptype, value). objects: pid -> (subnode nid, size, (bd, bs))."""
    if ndb.ansi:
        props = {pid: (_ansi_type(t), v) for pid, (t, v) in props.items()}
    nb = NodeBuilder(ndb, 0xBC)
    if subs:
        nb.subs.update(subs)
    recs = []
    for pid, (t, v) in sorted(props.items()):
        if v is None:
            continue
        if t in (I2, LONG, R4, ERROR, BOOL):
            raw = encode_value(t, v)
            raw = raw + b"\0" * (4 - len(raw))
        else:
            data = encode_value(t, v, cp)
            raw = struct.pack("<I", nb.value_ref(data))
        recs.append((struct.pack("<H", pid), struct.pack("<H", t) + raw))
    for pid, (snid, size, node) in (objects or {}).items():
        nb.subs[snid] = node
        recs.append((struct.pack("<H", pid), struct.pack("<H", OBJECT) +
                     struct.pack("<I", nb.heap.alloc(struct.pack("<II", snid, size)))))
    root = bth(nb.heap, 2, 6, recs)
    return nb.commit(root)


def write_tc(ndb: NDB, columns: list[tuple[int, int]], rows: list[dict]) -> tuple[int, int]:
    """columns: (pid, ptype) excluding the row id/version; rows: pid -> value (0x67F2 = row id)."""
    nb = NodeBuilder(ndb, 0x7C)
    if ndb.ansi:
        columns = [(pid, _ansi_type(t)) for pid, t in columns]
    cols = [(0x67F2, LONG), (0x67F3, LONG)] + [c for c in columns if c[0] not in (0x67F2, 0x67F3)]

    def width(t):
        return FIXED[t] if t in FIXED else 4          # variable-size values are 4-byte HNIDs
    layout: dict = {0x67F2: (LONG, 0, 4), 0x67F3: (LONG, 4, 4)}
    off = 8
    for want in (8, 4, 2, 1):                          # TCI_4b covers 8- and 4-byte cells, then 2, then 1
        for pid, t in cols[2:]:
            if width(t) == want:
                layout[pid] = (t, off, want)
                off += want
        if want == 4:
            end4 = off
        elif want == 2:
            end2 = off
        elif want == 1:
            end1 = off
    ibits = {pid: i for i, (pid, _) in enumerate(cols)}
    rowsize = end1 + (len(cols) + 7) // 8
    matrix = []
    for r in rows:
        cp = r.get("__cp", "cp1252")
        row = bytearray(rowsize)
        for pid, (t, o, sz) in layout.items():
            if pid not in r or r[pid] is None:
                continue
            v = r[pid]
            if t in FIXED and not t & MV:
                row[o:o + sz] = encode_value(t, v).ljust(sz, b"\0")[:sz]
            else:
                data = encode_value(t, v, cp)
                struct.pack_into("<I", row, o, nb.value_ref(data))
            bit = ibits[pid]
            row[end1 + bit // 8] |= 0x80 >> (bit % 8)
        matrix.append(bytes(row))
    hnid_rows = 0
    if matrix:
        allbytes = b"".join(matrix)
        if len(allbytes) <= MAX_ALLOC:
            hnid_rows = nb.heap.alloc(allbytes)
        else:
            per = ndb.max_data // rowsize
            chunks = [b"".join(matrix[i:i + per]) for i in range(0, len(matrix), per)]
            hnid_rows = nb.sub_nid()
            nb.subs[hnid_rows] = (ndb.blocks_tree(chunks), 0)
    if ndb.ansi and len(rows) > 0xFFFF:
        raise ValueError("ANSI PST folders hold at most 65,535 items; export as Unicode PST instead")
    ri = "<H" if ndb.ansi else "<I"            # row index entries: 2 bytes (ANSI) or 4 bytes (Unicode)
    idx = bth(nb.heap, 4, struct.calcsize(ri), [(struct.pack("<I", r[0x67F2]), struct.pack(ri, i))
                                                for i, r in enumerate(rows)])
    descs = sorted((((pid << 16) | t), o, sz, ibits[pid]) for pid, (t, o, sz) in layout.items())
    info = struct.pack("<BBHHHHIII", 0x7C, len(cols), end4, end2, end1, rowsize, idx, hnid_rows, 0)
    info += b"".join(struct.pack("<IHBB", tag, o, sz, bit) for tag, o, sz, bit in descs)
    return nb.commit(nb.heap.alloc(info))


# ================================================================= messaging
class PSTWriter:
    """Usage:  w = PSTWriter(path, "My archive"); w.add(["Inbox"], message) ...; w.close()"""

    def __init__(self, path: str, display_name: str = "Personal Folders", ansi: bool = False):
        self.ndb = NDB(path, ansi)
        self.ansi = ansi
        self.name = display_name
        self.uid = uuid.uuid4().bytes
        self.namer = Namer()
        self.next_folder = 0x405
        self.next_msg = 0x10000
        self.ipm = (0x401 << 5) | T_FOLDER
        self.search_root = (0x402 << 5) | T_FOLDER
        self.spam = (0x403 << 5) | T_SEARCH_FOLDER
        self.deleted = (0x404 << 5) | T_FOLDER
        # folder nid -> info
        self.folders: dict[int, dict] = {
            self.ipm: {"name": "Top of Personal Folders", "parent": NID_ROOT_FOLDER, "rows": [], "children": [],
                       "kinds": {}},
            self.deleted: {"name": "Deleted Items", "parent": self.ipm, "rows": [], "children": [], "kinds": {}},
        }
        self.folders[self.ipm]["children"].append(self.deleted)
        self.by_path: dict[tuple, int] = {("deleted items",): self.deleted}
        self.count = 0

    def eid(self, nid: int) -> bytes:
        return b"\0\0\0\0" + self.uid + struct.pack("<I", nid)

    def folder(self, path: list[str]) -> int:
        key: tuple = ()
        parent = self.ipm
        for part in path:
            part = part or "Untitled"
            key = key + (part.lower(),)
            if key not in self.by_path:
                nid = (self.next_folder << 5) | T_FOLDER
                self.next_folder += 1
                self.folders[nid] = {"name": part, "parent": parent, "rows": [], "children": [], "kinds": {}}
                self.folders[parent]["children"].append(nid)
                self.by_path[key] = nid
            parent = self.by_path[key]
        return parent

    # ----------------------------------------------------------- messages
    def _message_node(self, m: Message) -> tuple[int, int, dict, int]:
        """Write a message's PC and subnodes; returns (bid_data, bid_sub, props, size)."""
        ndb = self.ndb
        props = message_props(m, self.namer)
        cp = "cp1252"
        if self.ansi:
            cpid, cp = pick_codepage(*[v for t, v in props.values() if isinstance(v, str)],
                                     *[a.filename for a in m.attachments], *[str(x) for x in m.to + m.cc + m.bcc])
            props[0x3FFD] = (LONG, cpid)        # PidTagMessageCodepage: how the 8-bit strings are encoded
            if 0x1013 not in props:
                props[0x3FDE] = (LONG, cpid)
        self._cp = cp
        subs: dict[int, tuple[int, int]] = {}
        size = 0
        # recipients
        rrows = []
        for i, r in enumerate(recipient_rows(m)):
            row = {pid: v for pid, (t, v) in r.items()}
            row[0x67F2] = i
            enc = (lambda x: (x + "\0").encode(cp, "replace")) if self.ansi else \
                (lambda x: (x + "\0").encode("utf-16-le"))
            row[0x0FFF] = b"\0\0\0\0" + uuid.UUID("812b1fa4-bea3-1019-9d6e-00dd010f5402").bytes + \
                (b"\0\0\x01\x10" if self.ansi else b"\0\0\x01\x90") + enc(r[0x3001][1]) + enc("SMTP") + \
                enc(r[0x3003][1])
            row["__cp"] = cp
            rrows.append(row)
        subs[NID_RECIP_TABLE] = write_tc(ndb, RECIP_COLS, rrows)
        # attachments
        if m.attachments:
            arows = []
            for i, a in enumerate(m.attachments):
                anid = ((i + 1) << 5) | T_ATTACHMENT
                ap = attachment_props(a, i)
                objects = {}
                if a.embedded is not None:
                    ebd, ebs, _, esz = self._message_node(a.embedded)
                    ap[0x3705] = (LONG, 5)
                    enid = (1 << 5) | T_MESSAGE
                    objects[0x3701] = (enid, esz, (ebd, ebs))
                    asize = esz
                else:
                    data = a.data or b""
                    ap[0x3705] = (LONG, 1)
                    ap[0x3701] = (BIN, data)
                    asize = len(data)
                ap[0x0E20] = (LONG, asize)
                size += asize
                subs[anid] = write_pc(ndb, ap, objects, cp=cp)
                arows.append({0x67F2: anid, 0x0E20: asize, 0x3704: a.filename[:255], 0x3705: ap[0x3705][1],
                              0x370B: -1, "__cp": cp})
            subs[NID_ATTACH_TABLE] = write_tc(ndb, ATTACH_COLS, arows)
        body = props.get(0x1000, (UNI, ""))[1] or ""
        size += len(str(body)) * 2 + 1024
        props[0x0E08] = (LONG, min(size, 0x7FFFFFFF))
        bd, bs = write_pc(ndb, props, subs=subs, cp=cp)
        self._cp = cp
        return bd, bs, props, size

    def add(self, path: list[str], m: Message):
        fnid = self.folder(path)
        nid = (self.next_msg << 5) | T_MESSAGE
        self.next_msg += 1
        bd, bs, props, size = self._message_node(m)
        self.ndb.node(nid, bd, bs, fnid)
        g = lambda pid: props.get(pid, (None, None))[1]  # noqa: E731
        row = {0x67F2: nid, 0x67F3: 0, "__cp": self._cp}
        for pid, t in CONTENTS_COLS:
            v = g(pid)
            if v is not None:
                row[pid] = v
        row[0x0E08] = min(size, 0x7FFFFFFF)
        f = self.folders[fnid]
        f["rows"].append(row)
        f["kinds"][m.kind] = f["kinds"].get(m.kind, 0) + 1
        self.count += 1

    # ------------------------------------------------------------- finish
    def _folder_props(self, name, count, unread, has_children, container=None):
        p = {0x3001: (UNI, name), 0x3602: (LONG, count), 0x3603: (LONG, unread), 0x360A: (BOOL, has_children)}
        if container:
            p[0x3613] = (UNI, container)
        return p

    def _hier_row(self, nid: int, name: str, count: int, unread: int, kids: bool, container: str | None):
        r = {0x67F2: nid, 0x67F3: 0, 0x3001: name, 0x3602: count, 0x3603: unread, 0x360A: kids,
             "__cp": pick_codepage(name)[1]}
        if container:
            r[0x3613] = container
        return r

    def _write_folder(self, nid: int, name: str, parent: int, rows: list, children: list[tuple],
                      container: str | None, search: bool = False):
        ndb = self.ndb
        unread = sum(1 for r in rows if not (r.get(0x0E07, 0) & 1))
        fprops = self._folder_props(name, len(rows), unread, bool(children), container)
        cpid, cpname = pick_codepage(name)
        if self.ansi and cpid != 1252:
            fprops[0x3FFD] = (LONG, cpid)
        pbd, pbs = write_pc(ndb, fprops, cp=cpname)
        ndb.node(nid, pbd, pbs, parent)
        idx = nid & ~0x1F
        if search:
            bd, bs = write_tc(ndb, SEARCH_COLS, [])
            ndb.node(idx | T_SEARCH_CONTENTS, bd, bs, 0)
            ndb.node(idx | T_SUQ, 0, 0, 0)
            return
        bd, bs = write_tc(ndb, HIER_COLS, [self._hier_row(*c) for c in children])
        ndb.node(idx | T_HIER, bd, bs, 0)
        bd, bs = write_tc(ndb, CONTENTS_COLS, rows)
        ndb.node(idx | T_CONTENTS, bd, bs, 0)
        bd, bs = write_tc(ndb, ASSOC_COLS, [])
        ndb.node(idx | T_ASSOC, bd, bs, 0)

    def _container(self, f: dict) -> str:
        if not f["kinds"]:
            return "IPF.Note"
        kind = max(f["kinds"].items(), key=lambda kv: kv[1])[0]
        return CONTAINER.get(kind, "IPF.Note")

    def close(self):
        ndb = self.ndb
        # templates
        for nid, cols in ((NID_HIER_TEMPLATE, HIER_COLS), (NID_CONTENTS_TEMPLATE, CONTENTS_COLS),
                          (NID_ASSOC_TEMPLATE, ASSOC_COLS), (NID_SEARCH_TEMPLATE, SEARCH_COLS),
                          (NID_ATTACH_TABLE, ATTACH_COLS), (NID_RECIP_TABLE, RECIP_COLS)):
            bd, bs = write_tc(ndb, cols, [])
            ndb.node(nid, bd, bs, 0)
        ndb.node(NID_SMQ, 0, 0, 0)
        ndb.node(NID_SAL, 0, 0, 0)
        # user folders (children before parents is not required; write all)
        def child_tuple(nid):
            f = self.folders[nid]
            unread = sum(1 for r in f["rows"] if not (r.get(0x0E07, 0) & 1))
            return (nid, f["name"], len(f["rows"]), unread, bool(f["children"]),
                    None if nid == self.ipm else self._container(f))
        for nid, f in self.folders.items():
            container = None if nid == self.ipm else self._container(f)
            self._write_folder(nid, f["name"], f["parent"], f["rows"], [child_tuple(c) for c in f["children"]],
                               container)
        # root, search root, spam search folder
        root_children = [child_tuple(self.ipm), (self.search_root, "Search Root", 0, 0, False, None),
                         (self.spam, "SPAM Search Folder 2", 0, 0, False, None)]
        self._write_folder(NID_ROOT_FOLDER, "", NID_ROOT_FOLDER, [], root_children, None)
        self._write_folder(self.search_root, "Search Root", NID_ROOT_FOLDER, [], [], None)
        self._write_folder(self.spam, "SPAM Search Folder 2", NID_ROOT_FOLDER, [], [], None, search=True)
        # named property map
        self._name_map()
        # message store
        store = {0x0FF9: (BIN, self.uid), 0x3001: (UNI, self.name), 0x35E0: (BIN, self.eid(self.ipm)),
                 0x35E3: (BIN, self.eid(self.deleted)), 0x35E7: (BIN, self.eid(self.search_root)),
                 0x67FF: (LONG, 0), 0x340D: (LONG, 0x0000000D if self.ansi else 0x0004000D), 0x6633: (BOOL, True)}
        bd, bs = write_pc(ndb, store)
        ndb.node(NID_MESSAGE_STORE, bd, bs, 0)
        ndb.finish({"unique": self.count + 1})

    def _name_map(self):
        guids: list[bytes] = []
        entries, strings = b"", b""
        buckets: dict[int, bytes] = {}
        for i, (kind, guid, ident) in enumerate(self.namer.keys):
            if guid == PS_MAPI:
                g = 1
            elif guid == PS_PUBLIC_STRINGS:
                g = 2
            else:
                gb = uuid.UUID(guid).bytes_le
                if gb not in guids:
                    guids.append(gb)
                g = guids.index(gb) + 3
            if kind == "name":
                nm = ident.encode("utf-16-le")
                off = len(strings)
                strings += struct.pack("<I", len(nm)) + nm
                strings += b"\0" * ((-len(strings)) % 4)
                wg = (g << 1) | 1
                entries += struct.pack("<IHH", off, wg, i)
                hval = pst_crc(nm)
            else:
                wg = g << 1
                entries += struct.pack("<IHH", ident, wg, i)
                hval = ident
            bucket = (hval ^ wg) % 251
            buckets[bucket] = buckets.get(bucket, b"") + struct.pack("<IHH", hval, wg, i)
        props = {0x0001: (LONG, 251), 0x0002: (BIN, b"".join(guids)), 0x0003: (BIN, entries),
                 0x0004: (BIN, strings)}
        for k, v in buckets.items():
            props[0x1000 + k] = (BIN, v)
        bd, bs = write_pc(self.ndb, props)
        self.ndb.node(NID_NAME_TO_ID_MAP, bd, bs, 0)


def write_pst(path: str, items, display_name: str = "MailVault export", progress=None, cancelled=None,
              ansi: bool = False) -> int:
    """items: iterable of (folder path string, Message or loader). ansi=True writes Outlook 97-2002 format."""
    w = PSTWriter(path, display_name, ansi)
    items = list(items)
    for i, (folder, m) in enumerate(items):
        if cancelled and cancelled():
            w.ndb.f.close()
            os.remove(path)
            from .export import Cancelled
            raise Cancelled()
        if callable(m):
            m = m()
        parts = [p for p in str(folder).replace("\\", "/").split("/") if p]
        if len(parts) > 1:
            parts = parts[1:]         # drop the source mailbox name: it becomes the store name
        elif parts and parts[0].lower().endswith((".pst", ".ost", ".mbox", ".msg", ".eml", ".zip")):
            parts = ["Imported"]
        w.add(parts or ["Imported"], m)
        if progress:
            progress(i + 1, len(items))
    w.close()
    return w.count
