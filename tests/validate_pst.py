"""Strict structural validator for Unicode PST files written by MailVault.

Checks what Outlook/SCANPST rely on: header CRCs, page/block CRCs and
signatures, B-tree ordering and keys, block overlap, AMap coverage and free
count, map pages at every region, EOF, mandatory nodes, and that folder
tables agree with the node tree. Usage: python tests/validate_pst.py file.pst
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mailvault.core.pst import PST, _block_sig, pst_crc  # noqa: E402

REGION, FIRST = 253952, 0x4400


def validate(path: str) -> list[str]:
    d = open(path, "rb").read()
    errs: list[str] = []
    e = errs.append
    ver = struct.unpack_from("<H", d, 10)[0]
    ansi = ver in (14, 15)
    if d[:4] != b"!BDN" or d[8:10] != b"SM" or ver not in (14, 15, 23):
        e("bad header magic/version")
    if struct.unpack_from("<I", d, 4)[0] != pst_crc(d[8:8 + 471]):
        e("dwCRCPartial wrong")
    if not ansi and struct.unpack_from("<I", d, 524)[0] != pst_crc(d[8:8 + 516]):
        e("dwCRCFull wrong")
    if ansi:
        eof, amap_last, amap_free = struct.unpack_from("<III", d, 168)
        valid = d[200]
        nbt_bid, nbt_ib, bbt_bid, bbt_ib = struct.unpack_from("<IIII", d, 184)
    else:
        eof, amap_last, amap_free = struct.unpack_from("<QQQ", d, 184)
        valid = d[248]
        nbt_bid, nbt_ib, bbt_bid, bbt_ib = struct.unpack_from("<QQQQ", d, 216)
    if eof != len(d):
        e(f"ibFileEof {eof} != size {len(d)}")
    if (len(d) - FIRST) % REGION:
        e("file does not end on an AMap region boundary")
    if valid != 2:
        e("fAMapValid is not VALID_AMAP2")
    TL = 12 if ansi else 16
    AREA = 496 if ansi else 488

    pages, blocks = [], {}

    def walk(ib, ptype, lo_key, depth=0):
        pg = d[ib: ib + 512]
        t = pg[512 - TL:]
        if t[0] != ptype or t[1] != ptype:
            e(f"page {ib:#x}: wrong ptype")
            return
        if ansi:
            bid, crc = struct.unpack_from("<II", t, 4)
        else:
            crc, bid = struct.unpack_from("<IQ", t, 4)
        if pst_crc(pg[:512 - TL]) != crc:
            e(f"page {ib:#x}: CRC")
        if struct.unpack_from("<H", t, 2)[0] != _block_sig(ib, bid):
            e(f"page {ib:#x}: signature")
        pages.append(ib)
        c, cmax, cb, lvl = pg[AREA:AREA + 4]
        keys = []
        for i in range(c):
            if lvl == 0:
                if ptype == 0x80:
                    bid_, ib_, cb_, ref = struct.unpack_from("<IIHH" if ansi else "<QQHH", pg, i * cb)
                    blocks[bid_] = (ib_, cb_)
                    keys.append(bid_)
                    if ref < 2:
                        e(f"block {bid_:#x}: cRef {ref}")
                else:
                    keys.append(struct.unpack_from("<I" if ansi else "<Q", pg, i * cb)[0])
            else:
                k, cbid, cib = struct.unpack_from("<III" if ansi else "<QQQ", pg, i * cb)
                keys.append(k)
                walk(cib, ptype, k, depth + 1)
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            e(f"page {ib:#x}: keys not strictly ascending")
        if keys and lo_key is not None and keys[0] != lo_key:
            e(f"page {ib:#x}: parent key {lo_key:#x} != first key {keys[0]:#x}")

    walk(bbt_ib, 0x80, None)
    walk(nbt_ib, 0x81, None)
    # blocks
    spans = []
    for bid, (ib, cb) in blocks.items():
        size = (cb + TL + 63) & ~63
        t = d[ib + size - TL: ib + size]
        if ansi:
            tcb, sig, tbid, crc = struct.unpack("<HHII", t)
        else:
            tcb, sig, crc, tbid = struct.unpack("<HHIQ", t)
        if tcb != cb or tbid != bid:
            e(f"block {bid:#x}: trailer mismatch")
        if pst_crc(d[ib:ib + cb]) != crc:
            e(f"block {bid:#x}: CRC")
        if sig != _block_sig(ib, bid):
            e(f"block {bid:#x}: signature")
        if ib % 64:
            e(f"block {bid:#x}: misaligned")
        spans.append((ib, size, f"block {bid:#x}"))
    spans += [(ib, 512, f"page {ib:#x}") for ib in pages]
    regions = (len(d) - FIRST) // REGION
    for r in range(regions):
        spans.append((FIRST + r * REGION, 512, f"AMap {r}"))
    spans.sort()
    for (a, sa, na), (b, sb, nb) in zip(spans, spans[1:]):
        if a + sa > b:
            e(f"overlap: {na} and {nb}")
    # AMaps
    free = 0
    for r in range(regions):
        ib = FIRST + r * REGION
        pg = d[ib: ib + 512]
        if ansi:
            ok = pg[500] == 0x84 and struct.unpack_from("<II", pg, 504) == (ib, pst_crc(pg[:500]))
            bits = pg[4:500]
        else:
            ok = pg[496] == 0x84 and struct.unpack_from("<I", pg, 500)[0] == pst_crc(pg[:496]) and \
                struct.unpack_from("<Q", pg, 504)[0] == ib
            bits = pg[:496]
        if not ok:
            e(f"AMap {r}: bad page")
        free += (496 * 8 - sum(bin(x).count("1") for x in bits)) * 64
        if r % 8 == 0 and d[ib + 0x200 + 512 - TL] != 0x83:
            e(f"PMap missing in region {r}")
    if free != amap_free:
        e(f"cbAMapFree {amap_free} != {free}")
    for ib, size, name in spans:
        for off in range(ib, ib + size, 64):
            slot = (off - FIRST) // 64
            r, s = divmod(slot, 496 * 8)
            if not d[FIRST + r * REGION + (4 if ansi else 0) + s // 8] & (0x80 >> (s % 8)):
                e(f"{name}: not marked allocated in AMap")
                break
    if amap_last != FIRST + (regions - 1) * REGION:
        e("ibAMapLast wrong")
    # nodes
    pst = PST(d)
    for nid in (0x21, 0x61, 0x122, 0x1E1, 0x201, 0x60D, 0x60E, 0x60F, 0x610, 0x671, 0x692):
        if nid not in pst.nbt:
            e(f"mandatory node {nid:#x} missing")
    for nid, ref in pst.nbt.items():
        for b in (ref.bid_data, ref.bid_sub):
            if b and (b & ~1) not in blocks:
                e(f"node {nid:#x}: block {b:#x} not in BBT")
    for nid, ref in pst.nbt.items():
        if nid & 0x1F != 2:
            continue
        idx = nid & ~0x1F
        for t in (0x0D, 0x0E, 0x0F):
            if idx | t not in pst.nbt:
                e(f"folder {nid:#x}: table {t:#x} missing")
        msgs = {n for n, r2 in pst.nbt.items() if n & 0x1F == 4 and r2.parent == nid}
        rows = {r.get(0x67F2) for r in pst.node(pst.nbt[idx | 0x0E].bid_data, pst.nbt[idx | 0x0E].bid_sub, []).tc()}
        if rows != msgs:
            e(f"folder {nid:#x}: contents table rows != messages")
        kids = {n for n, r2 in pst.nbt.items() if n & 0x1F in (2, 3) and r2.parent == nid and n != nid}
        hrows = {r.get(0x67F2) for r in pst.node(pst.nbt[idx | 0x0D].bid_data, pst.nbt[idx | 0x0D].bid_sub, []).tc()}
        if hrows != kids:
            e(f"folder {nid:#x}: hierarchy rows != subfolders")
        props = pst.props_of(nid, [])
        if props.get(0x3602) != len(msgs):
            e(f"folder {nid:#x}: content count wrong")
    if pst.problems:
        errs += ["reader: " + p for p in pst.problems]
    return errs


if __name__ == "__main__":
    problems = validate(sys.argv[1])
    print("\n".join(problems) or "PST is structurally valid")
    sys.exit(1 if problems else 0)
