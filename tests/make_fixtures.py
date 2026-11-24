"""Generate test mail files, including a PST with damaged, orphaned and deleted items.

The PST writer here is a test tool: it builds structurally valid Unicode PSTs
(NDB + LTP layers, permute encryption) so the reader can be exercised offline.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import struct
import sys
import tarfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mailvault.core.model import Address, Attachment, Message  # noqa: E402
from mailvault.core.msg import build_msg  # noqa: E402
from mailvault.core.mime import build_eml  # noqa: E402
from mailvault.core.pst import encrypt_permute, pst_crc  # noqa: E402

U, L, B, BIN, TIME, OBJ, DBL, MVU = 0x1F, 0x3, 0xB, 0x102, 0x40, 0xD, 0x5, 0x101F


class PSTWriter:
    def __init__(self):
        self.blocks = []        # (bid, stored_bytes, raw_len)
        self.next_bid = 4
        self.nodes = []         # (nid, bid_data, bid_sub, parent)
        self.stale_nodes = []
        self.stale_blocks = set()
        self.drop_from_bbt = set()
        self.corrupt = set()

    def _bid(self, internal=False):
        b = self.next_bid
        self.next_bid += 4
        return b | (2 if internal else 0)

    def block(self, data: bytes, internal=False) -> int:
        bid = self._bid(internal)
        stored = data if internal else encrypt_permute(data)
        self.blocks.append((bid, stored))
        return bid

    def data_node(self, data: bytes) -> int:
        MAX = 8176
        if len(data) <= MAX:
            return self.block(data)
        bids = [self.block(data[i:i + MAX]) for i in range(0, len(data), MAX)]
        x = struct.pack("<BBHI", 1, 1, len(bids), len(data)) + b"".join(struct.pack("<Q", b) for b in bids)
        return self.block(x, internal=True)

    def subnode_block(self, subs: dict) -> int:
        if not subs:
            return 0
        body = struct.pack("<BBHI", 2, 0, len(subs), 0)
        for nid in sorted(subs):
            bd, bs = subs[nid]
            body += struct.pack("<QQQ", nid, bd, bs)
        return self.block(body, internal=True)

    # ---------------------------------------------------------------- LTP
    @staticmethod
    def heap(client: int, allocs: list[bytes], user_root_index: int) -> bytes:
        body = bytearray(12)
        offs = []
        for a in allocs:
            offs.append(len(body))
            body += a
        offs.append(len(body))
        if len(body) % 2:
            body += b"\0"
        ibm = len(body)
        body += struct.pack("<HH", len(allocs), 0) + b"".join(struct.pack("<H", o) for o in offs)
        struct.pack_into("<HBBI", body, 0, ibm, 0xEC, client, user_root_index << 5)
        return bytes(body)

    def pc(self, props: list, subs: dict) -> bytes:
        allocs, recs = [], []
        for pid, ptype, val in sorted(props, key=lambda p: p[0]):
            if ptype in (L, B):
                raw = struct.pack("<I", int(val))
            elif ptype == OBJ:
                allocs.append(struct.pack("<II", val, 0))
                raw = struct.pack("<I", len(allocs) << 5)
            else:
                if ptype == U:
                    b = val.encode("utf-16-le")
                elif ptype == MVU:
                    enc = [v.encode("utf-16-le") for v in val]
                    hdr = 4 + 4 * len(enc)
                    offs, pos = [], hdr
                    for e in enc:
                        offs.append(pos)
                        pos += len(e)
                    b = struct.pack(f"<I{len(enc)}I", len(enc), *offs) + b"".join(enc)
                elif ptype == DBL:
                    b = struct.pack("<d", val)
                elif ptype == TIME:
                    b = struct.pack("<Q", val)
                else:
                    b = bytes(val)
                if len(b) > 3580:
                    snid = ((len(subs) + 1) << 5) | 0x1F
                    subs[snid] = (self.data_node(b), 0)
                    raw = struct.pack("<I", snid)
                else:
                    allocs.append(b)
                    raw = struct.pack("<I", len(allocs) << 5)
            recs.append(struct.pack("<HH", pid, ptype) + raw)
        allocs.append(b"".join(recs))
        rec_hid = len(allocs) << 5
        allocs.append(struct.pack("<BBBBI", 0xB5, 2, 6, 0, rec_hid))
        return self.heap(0xBC, allocs, len(allocs))

    def tc(self, cols: list, rows: list[dict]) -> bytes:
        allocs = []
        cols = [(0x67F2, L), (0x67F3, L)] + [c for c in cols if c[0] not in (0x67F2, 0x67F3)]
        layout, off = [], 0
        for pid, ptype in cols:
            size = 8 if ptype == TIME else 4
            layout.append((pid, ptype, off, size))
            off += size
        ceb = off
        rowsize = ceb + (len(cols) + 7) // 8
        rowbytes = bytearray()
        for r in rows:
            row = bytearray(rowsize)
            for i, (pid, ptype, o, size) in enumerate(layout):
                if pid not in r:
                    continue
                v = r[pid]
                if ptype == L:
                    struct.pack_into("<I", row, o, v)
                elif ptype == TIME:
                    struct.pack_into("<Q", row, o, v)
                else:
                    allocs.append(v.encode("utf-16-le") if ptype == U else bytes(v))
                    struct.pack_into("<I", row, o, len(allocs) << 5)
                row[ceb + i // 8] |= 0x80 >> (i % 8)
            rowbytes += row
        allocs.append(bytes(rowbytes))
        rows_hid = len(allocs) << 5
        allocs.append(b"".join(struct.pack("<II", r[0x67F2], i) for i, r in enumerate(rows)))
        idx_rec = len(allocs) << 5
        allocs.append(struct.pack("<BBBBI", 0xB5, 4, 4, 0, idx_rec if rows else 0))
        idx_hid = len(allocs) << 5
        info = struct.pack("<BBHHHHIII", 0x7C, len(cols), ceb, ceb, ceb, rowsize,
                           idx_hid, rows_hid if rows else 0, 0)
        for pid, ptype, o, size in layout:
            info += struct.pack("<IHBB", (pid << 16) | ptype, o, size, layout.index((pid, ptype, o, size)))
        allocs.append(info)
        return self.heap(0x7C, allocs, len(allocs))

    def node(self, nid, parent, props, subs=None, tc=None):
        subs = subs if subs is not None else {}
        data = self.tc(*tc) if tc else self.pc(props, subs)
        bd = self.data_node(data)
        self.nodes.append((nid, bd, self.subnode_block(subs), parent))
        return bd

    # --------------------------------------------------------------- NDB
    def write(self) -> bytes:
        out = bytearray(0x4400)
        bbt = []
        for bid, stored in self.blocks:
            size = (len(stored) + 16 + 63) & ~63
            ib = len(out)
            crc = pst_crc(stored)
            if bid in self.corrupt:
                stored = stored[:20] + bytes([stored[20] ^ 0x5A]) + stored[21:]
            blk = bytearray(size)
            blk[:len(stored)] = stored
            struct.pack_into("<HHIQ", blk, size - 16, len(stored), 0, crc, bid)
            out += blk
            if bid not in self.drop_from_bbt:
                (bbt if bid not in self.stale_blocks else self._stale_bbt).append((bid, ib, len(stored)))
        while len(out) % 512:
            out += b"\0"
        next_page = [0x10000]

        def page(entries, ptype, cb, level, pack):
            pg = bytearray(512)
            for i, e in enumerate(entries):
                pg[i * cb:(i + 1) * cb] = pack(e)
            pg[488:492] = bytes([len(entries), 488 // cb, cb, level])
            pbid = next_page[0]
            next_page[0] += 4
            struct.pack_into("<BBHIQ", pg, 496, ptype, ptype, 0, pst_crc(bytes(pg[:496])), pbid)
            ib = len(out)
            out.extend(pg)
            return pbid, ib

        def tree(entries, ptype, cb, pack, keyf):
            per = 488 // cb
            level, items = 0, []
            for i in range(0, max(len(entries), 1), per):
                chunk = entries[i:i + per]
                pbid, ib = page(chunk, ptype, cb, 0, pack)
                items.append((keyf(chunk[0]) if chunk else 0, pbid, ib))
            while len(items) > 1:
                level += 1
                nxt = []
                for i in range(0, len(items), 20):
                    chunk = items[i:i + 20]
                    pbid, ib = page(chunk, ptype, 24, level, lambda e: struct.pack("<QQQ", *e))
                    nxt.append((chunk[0][0], pbid, ib))
                items = nxt
            return items[0][1], items[0][2]

        nb = lambda e: struct.pack("<QQQII", e[0], e[1], e[2], e[3], 0)  # noqa: E731
        bb = lambda e: struct.pack("<QQHHI", e[0], e[1], e[2], 2, 0)  # noqa: E731
        bbt.sort()
        live_nodes = sorted(self.nodes)
        bbt_ref = tree(bbt, 0x80, 24, bb, lambda e: e[0])
        nbt_ref = tree(live_nodes, 0x81, 32, nb, lambda e: e[0])
        # stale pages left behind after "deletion"
        if self.stale_nodes:
            page(sorted(self.stale_nodes), 0x81, 32, 0, nb)
        if self._stale_bbt:
            page(sorted(self._stale_bbt), 0x80, 24, 0, bb)
        hdr = bytearray(564)
        hdr[0:4] = b"!BDN"
        hdr[8:10] = b"SM"
        struct.pack_into("<HH", hdr, 10, 23, 19)
        hdr[14], hdr[15] = 1, 1
        struct.pack_into("<QQ", hdr, 32, next_page[0], 0)
        struct.pack_into("<QQ", hdr, 184, len(out), 0)
        struct.pack_into("<QQ", hdr, 216, *nbt_ref)
        struct.pack_into("<QQ", hdr, 232, *bbt_ref)
        hdr[512] = 0x80
        hdr[513] = 1  # permute encryption
        out[:564] = hdr
        return bytes(out)

    _stale_bbt: list = []


def ft(y, mo, d, h=9, mi=0):
    t = dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc)
    return int((t - dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)).total_seconds() * 1e7)


import uuid as _uuid

APPT, ADDR, TASK, NOTE = ("00062002-0000-0000-c000-000000000046", "00062004-0000-0000-c000-000000000046",
                          "00062003-0000-0000-c000-000000000046", "0006200e-0000-0000-c000-000000000046")
NAMED = [(APPT, 0x820D), (APPT, 0x820E), (APPT, 0x8208), (APPT, 0x8215), (APPT, 0x8205), (ADDR, 0x8083),
         (TASK, 0x8105), (TASK, 0x8101), (TASK, 0x8102), (TASK, 0x811C), (NOTE, 0x8B00), ("PS_PUBLIC", "Keywords"),
         (APPT, 0x8216), (APPT, 0x8233), (APPT, 0x8234), (APPT, 0x8223), (APPT, 0x8228)]
NP = {k: 0x8000 + i for i, k in enumerate(NAMED)}


def name_map_props():
    guids = [APPT, ADDR, TASK, NOTE]
    gstream = b"".join(_uuid.UUID(g).bytes_le for g in guids)
    entries, strings = b"", b""
    for i, (g, lid) in enumerate(NAMED):
        if g == "PS_PUBLIC":
            off = len(strings)
            nm = lid.encode("utf-16-le")
            strings += struct.pack("<I", len(nm)) + nm
            strings += b"\0" * ((-len(strings)) % 4)
            entries += struct.pack("<IHH", off, (2 << 1) | 1, i)
        else:
            entries += struct.pack("<IHH", lid, ((guids.index(g) + 3) << 1), i)
    return [(0x0001, L, 251), (0x0002, BIN, gstream), (0x0003, BIN, entries), (0x0004, BIN, strings)]


def message_props(subject, sender, email, to, body, when, html=None, rtf=None, read=True, flags=0, extra=()):
    p = [(0x001A, U, "IPM.Note"), (0x0037, U, subject), (0x0C1A, U, sender), (0x0C1F, U, email),
         (0x5D01, U, email), (0x0E04, U, to), (0x1000, U, body), (0x0039, TIME, when),
         (0x0E06, TIME, when), (0x0E07, L, (1 if read else 0) | flags), (0x0017, L, 1),
         (0x1035, U, f"<{abs(hash(subject))}@example.com>"), (0x3FFD, L, 65001)]
    p += list(extra)
    if html:
        p.append((0x1013, BIN, html.encode()))
    if rtf:
        p.append((0x1009, BIN, rtf))
    return p


def recip_tc(rs):
    cols = [(0x3001, U), (0x3003, U), (0x39FE, U), (0x0C15, L)]
    rows = [{0x67F2: i, 0x67F3: 0, 0x3001: n, 0x3003: e, 0x39FE: e, 0x0C15: t}
            for i, (n, e, t) in enumerate(rs)]
    return cols, rows


def make_pst(path):
    w = PSTWriter()
    PSTWriter._stale_bbt = []
    IPM, INBOX, SENT, DELETED, PROJ = 0x8022, 0x8042, 0x8062, 0x8082, 0x80A2
    w.node(0x21, 0, [(0x3001, U, "Archive 2019"), (0x35E0, BIN, b"\0" * 20 + struct.pack("<I", IPM))])
    w.node(0x61, 0, name_map_props())
    w.node(0x122, 0x122, [(0x3001, U, "")])
    w.node(IPM, 0x122, [(0x3001, U, "Top of Personal Folders")])
    CAL, CON, TSK, NOTES, ARCH = 0x80C2, 0x80E2, 0x8102, 0x8122, 0x8142
    for nid, name, par, cls in [(INBOX, "Inbox", IPM, "IPF.Note"), (SENT, "Sent Items", IPM, "IPF.Note"),
                                (DELETED, "Deleted Items", IPM, "IPF.Note"), (PROJ, "Projects", INBOX, "IPF.Note"),
                                (CAL, "Calendar", IPM, "IPF.Appointment"), (CON, "Contacts", IPM, "IPF.Contact"),
                                (TSK, "Tasks", IPM, "IPF.Task"), (NOTES, "Notes", IPM, "IPF.StickyNote"),
                                (ARCH, "Archive", IPM, "IPF.Note")]:
        w.node(nid, par, [(0x3001, U, name), (0x3613, U, cls)])
    # calendar
    def appt(nid, subj, s, e, loc, allday=False, busy=2):
        w.node(nid, CAL, [(0x001A, U, "IPM.Appointment"), (0x0037, U, subj), (0x0C1A, U, "You"),
                          (0x0C1F, U, "you@example.com"), (0x1000, U, f"Agenda for {subj}"),
                          (0x0060, TIME, s), (0x0061, TIME, e), (NP[(APPT, 0x820D)], TIME, s),
                          (NP[(APPT, 0x820E)], TIME, e), (NP[(APPT, 0x8208)], U, loc),
                          (NP[(APPT, 0x8215)], B, 1 if allday else 0), (NP[(APPT, 0x8205)], L, busy),
                          (0x0E04, U, "Dana Whitfield")])
    appt(0x200104, "Q3 budget review", ft(2019, 11, 14, 15), ft(2019, 11, 14, 16), "Room 4B")
    appt(0x200124, "Team offsite", ft(2019, 11, 21, 0), ft(2019, 11, 22, 0), "Lake House", allday=True, busy=3)
    appt(0x200144, "Dentist", ft(2019, 11, 5, 13, 30), ft(2019, 11, 5, 14, 30), "", busy=2)
    # weekly Mon+Wed standup, 8 times, one deleted, one moved (US Eastern time zone)
    from mailvault.core.recur import build_blob
    loc0 = dt.datetime(2019, 11, 4, 9, 30)
    blob = build_blob(0x200B, 1, 1, (1 << 1) | (1 << 3), loc0, loc0 + dt.timedelta(minutes=30), count=8,
                      deleted=[dt.datetime(2019, 11, 13)],
                      exceptions=[(dt.datetime(2019, 11, 18, 9, 30), dt.datetime(2019, 11, 18, 11, 0),
                                   dt.datetime(2019, 11, 18, 11, 30), "Standup (moved)")])
    tzs = struct.pack("<iiiH8HH8H", 300, 0, -60, 0, 0, 11, 0, 1, 2, 0, 0, 0, 0, 0, 3, 0, 2, 2, 0, 0, 0)
    # the moved occurrence keeps its own details in a hidden exception attachment
    exc_subs = {0x692: (w.data_node(w.tc(*recip_tc([("Dana Whitfield", "dana@example.com", 1),
                                                    ("Marcus Lee", "marcus@example.com", 2)]))), 0)}
    exc_pc = w.pc([(0x001A, U, "IPM.OLE.CLASS.{00061055-0000-0000-C000-000000000046}"),
                   (0x0037, U, "Standup (moved)"), (0x1000, U, "Moved for the customer demo. Bring laptops."),
                   (NP[(APPT, 0x820D)], TIME, ft(2019, 11, 18, 16, 0)), (NP[(APPT, 0x820E)], TIME, ft(2019, 11, 18, 16, 30)),
                   (NP[(APPT, 0x8208)], U, "Room 4B"), (NP[(APPT, 0x8228)], TIME, ft(2019, 11, 18, 14, 30))], exc_subs)
    exc_node = (w.data_node(exc_pc), w.subnode_block(exc_subs))
    att_subs = {0x8041: exc_node}
    exc_att = w.pc([(0x3707, U, "Standup (moved)"), (0x3705, L, 5), (0x3701, OBJ, 0x8041), (0x7FFE, B, 1),
                    (0x3714, L, 2), (0x7FF9, TIME, ft(2019, 11, 18, 14, 30)), (0x7FFB, TIME, ft(2019, 11, 18, 16, 0)),
                    (0x7FFC, TIME, ft(2019, 11, 18, 16, 30))], att_subs)
    standup_subs = {0x8025: (w.data_node(exc_att), w.subnode_block(att_subs))}
    w.node(0x200164 + 0x400, CAL, [(0x001A, U, "IPM.Appointment"), (0x0037, U, "Team standup"), (0x0C1A, U, "You"),
                      (0x0C1F, U, "you@example.com"), (0x1000, U, "Daily-ish sync"),
                      (NP[(APPT, 0x820D)], TIME, ft(2019, 11, 4, 14, 30)), (NP[(APPT, 0x820E)], TIME, ft(2019, 11, 4, 15, 0)),
                      (NP[(APPT, 0x8208)], U, "Teams"), (NP[(APPT, 0x8216)], BIN, blob), (NP[(APPT, 0x8233)], BIN, tzs),
                      (NP[(APPT, 0x8234)], U, "(UTC-05:00) Eastern Time (US & Canada)"), (NP[(APPT, 0x8223)], B, 1)],
           standup_subs)
    # contacts
    def contact(nid, first, last, company, title, email, mobile):
        w.node(nid, CON, [(0x001A, U, "IPM.Contact"), (0x0037, U, f"{first} {last}"), (0x3001, U, f"{first} {last}"),
                          (0x3A06, U, first), (0x3A11, U, last), (0x3A16, U, company), (0x3A17, U, title),
                          (0x3A1C, U, mobile), (NP[(ADDR, 0x8083)], U, email), (0x3A42, TIME, ft(1988, 4, 2))])
    contact(0x200164, "Dana", "Whitfield", "Acme Corp", "Accounts manager", "dana@example.com", "+1 555 0100")
    contact(0x200184, "Marcus", "Lee", "Lee Logistics", "Owner", "marcus@example.com", "+1 555 0101")
    # tasks
    def task(nid, subj, due, status, pct):
        w.node(nid, TSK, [(0x001A, U, "IPM.Task"), (0x0037, U, subj), (NP[(TASK, 0x8105)], TIME, due),
                          (NP[(TASK, 0x8101)], L, status), (NP[(TASK, 0x8102)], DBL, pct),
                          (NP[(TASK, 0x811C)], B, 1 if status == 2 else 0), (0x1000, U, f"Notes for {subj}")])
    task(0x2001A4, "Send Q3 numbers to Dana", ft(2019, 11, 15), 1, 0.5)
    task(0x2001C4, "Renew domain", ft(2019, 10, 1), 2, 1.0)
    w.node(0x2001E4, NOTES, [(0x001A, U, "IPM.StickyNote"), (0x0037, U, "Wi-Fi password"),
                             (0x1000, U, "Wi-Fi password\nguest network: lakehouse2019"), (NP[(NOTE, 0x8B00)], L, 3)])
    # sent + replied + archived + categories
    w.node(0x200204, SENT, message_props("Re: shipping dates", "You", "you@example.com", "Marcus Lee",
                                         "Thursday works.", ft(2019, 11, 10, 11), flags=0x20,
                                         extra=[(NP[("PS_PUBLIC", "Keywords")], MVU, ["Customers", "Follow up"])]))
    w.node(0x200224, ARCH, message_props("2018 tax documents", "Accountant", "cpa@example.com", "You",
                                         "Attached are your filings.", ft(2019, 3, 1),
                                         extra=[(0x1081, L, 104), (0x301F, TIME, ft(2019, 6, 1))]))
    w.node(0x200244, DRAFT_PARENT if False else IPM, message_props("Unfinished draft", "You", "you@example.com",
                                                                  "", "half written", ft(2019, 11, 13), flags=0x8))

    att_data = bytes(range(256)) * 80  # 20 KB -> XBLOCK
    m1 = 0x200004
    subs = {}
    att_subs = {}
    att_pc = w.pc([(0x3707, U, "invoice_q3.pdf"), (0x3705, L, 1), (0x370E, U, "application/pdf"),
                   (0x0E20, L, len(att_data)), (0x3701, BIN, att_data)], att_subs)
    subs[0x8025] = (w.data_node(att_pc), w.subnode_block(att_subs))
    subs[0x692] = (w.data_node(w.tc(*recip_tc([("You", "you@example.com", 1),
                                               ("Accounts", "ap@example.com", 2)]))), 0)
    w.node(m1, INBOX, message_props("Q3 invoice attached", "Dana Whitfield", "dana@example.com",
                                    "You", "Hi, the Q3 invoice is attached.", ft(2019, 11, 12, 15, 14),
                                    html="<p>Hi, the <b>Q3 invoice</b> is attached.</p>"), subs)
    rtf = b"{\\rtf1\\ansi\\ansicpg1252 Password reset requested.\\par Click the portal link.}"
    mela = struct.pack("<IIII", len(rtf) + 12, len(rtf), 0x414C454D, 0) + rtf
    w.node(0x200024, INBOX, message_props("Password reset", "IT Helpdesk", "it@example.com", "You",
                                          "", ft(2019, 11, 11), rtf=mela, read=False))
    w.node(0x200044, INBOX, message_props("Not in folder index", "Marcus Lee", "marcus@example.com",
                                          "You", "This item is missing from the contents table.", ft(2019, 11, 10)))
    w.node(0x200064, 0x9999, message_props("Lost parent folder", "Old Server", "old@example.com",
                                           "You", "Parent folder no longer exists.", ft(2019, 6, 1)))
    emb_subs = {}
    emb_pc = w.pc(message_props("Original proposal", "Priya N", "priya@example.com", "Dana",
                                "Embedded original message body.", ft(2019, 10, 1)), emb_subs)
    emb_node = (w.data_node(emb_pc), w.subnode_block(emb_subs))
    a2_subs = {0x8041: emb_node}
    a2 = w.pc([(0x3707, U, "Original proposal.msg"), (0x3705, L, 5), (0x3701, OBJ, 0x8041)], a2_subs)
    w.node(0x200084, SENT, message_props("Fwd: proposal", "You", "you@example.com", "Dana",
                                         "See the attached original.", ft(2019, 10, 2)),
           {0x8045: (w.data_node(a2), w.subnode_block(a2_subs))})
    bd = w.node(0x2000A4, PROJ, message_props("Kickoff notes", "Dana Whitfield", "dana@example.com",
                                              "Team", "Agenda: scope, budget, timeline.", ft(2019, 9, 3)))
    w.corrupt.add(bd)
    bd = w.node(0x2000C4, INBOX, message_props("Unreachable data", "X", "x@example.com", "You",
                                               "never readable", ft(2019, 1, 1)))
    w.drop_from_bbt.add(bd)
    # Fully orphaned message (no index entries anywhere) with attachment + recipients
    first = len(w.blocks)
    osubs, oatt_subs = {}, {}
    oatt = w.pc([(0x3707, U, "contract.pdf"), (0x3705, L, 1), (0x370E, U, "application/pdf"),
                 (0x0E20, L, 12000), (0x3701, BIN, b"%PDF-1.4 orphan" + b"z" * 11985)], oatt_subs)
    osubs[0x8025] = (w.data_node(oatt), w.subnode_block(oatt_subs))
    osubs[0x692] = (w.data_node(w.tc(*recip_tc([("Legal", "legal@example.com", 1)]))), 0)
    big_body = "Signed contract attached. " * 400        # > 8 KB heap -> multi-block (XBLOCK)
    opc = w.pc(message_props("Signed contract", "Priya N", "priya@example.com", "Legal", big_body[:3000],
                             ft(2019, 7, 2), extra=[(0x0E04, U, "Legal")]), osubs)
    obid = w.data_node(opc)
    w.subnode_block(osubs)
    w.drop_from_bbt.update(b for b, _ in w.blocks[first:])
    # Deleted message: data block still in file, only referenced from stale pages
    deleted_pc = w.pc(message_props("Deleted: budget draft", "Dana Whitfield", "dana@example.com",
                                    "You", "Budget draft v2 (deleted).", ft(2019, 8, 20)), {})
    dbid = w.data_node(deleted_pc)
    w.stale_blocks.add(dbid)
    w.stale_nodes.append((0x2000E4, dbid, 0, DELETED))
    # Contents table for Inbox lists m1, m2 and m7 but not m3 (with list columns)
    cols = [(0x0037, U), (0x0042, U), (0x0E04, U), (0x0E06, TIME), (0x0E08, L), (0x0E07, L), (0x1081, L)]
    rows = [{0x67F2: m1, 0x67F3: 0, 0x0037: "Q3 invoice attached", 0x0042: "Dana Whitfield", 0x0E04: "You",
             0x0E06: ft(2019, 11, 12, 15, 14), 0x0E08: 21312, 0x0E07: 0x11, 0x1081: 102},
            {0x67F2: 0x200024, 0x67F3: 0, 0x0037: "Password reset", 0x0042: "IT Helpdesk", 0x0E04: "You",
             0x0E06: ft(2019, 11, 11), 0x0E08: 900, 0x0E07: 0},
            {0x67F2: 0x2000C4, 0x67F3: 0}]
    w.node(0x804E, INBOX, None, tc=(cols, rows))
    data = w.write()
    open(path, "wb").write(data)
    return data


def tiny_png(w=160, h=100) -> bytes:
    import zlib
    rows = b"".join(b"\0" + bytes(v for x in range(w) for v in (40 + x, 120 + y, 200 - x // 2)) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def sample_messages():
    a = Message(subject="Team lunch Friday", sender=Address("Marcus Lee", "marcus@example.com"),
                to=[Address("You", "you@example.com")], date=dt.datetime(2020, 3, 5, 12, 30, tzinfo=dt.timezone.utc),
                body_text="Pizza at noon. From the office we walk over.", message_id="<lunch@example.com>",
                source_format="test")
    b = Message(subject="Photos from the trip", sender=Address("Priya N", "priya@example.com"),
                to=[Address("You", "you@example.com")], cc=[Address("Dana", "dana@example.com")],
                date=dt.datetime(2020, 4, 1, 8, 0, tzinfo=dt.timezone.utc),
                body_html="<p>Here are the <i>photos</i>.</p>", source_format="test")
    b.attachments.append(Attachment(filename="beach.jpg", mime_type="image/jpeg", _data=b"\xff\xd8\xff" + b"x" * 500, size=503))
    png = tiny_png()
    b.attachments.append(Attachment(filename="sunset.png", mime_type="image/png", _data=png, size=len(png)))
    b.attachments.append(Attachment(filename="packing list.txt", mime_type="text/plain", _data=b"towels\nsunscreen\nbooks\n", size=24))
    return [a, b]


def main(out="tests/fixtures"):
    os.makedirs(out, exist_ok=True)
    make_pst(f"{out}/archive2019.pst")
    a, b = sample_messages()
    open(f"{out}/lunch.eml", "wb").write(build_eml(a))
    open(f"{out}/photos.msg", "wb").write(build_msg(b))
    mbox = io.BytesIO()
    from mailvault.core.mbox import write_mbox
    write_mbox(mbox, [a, b, a])
    open(f"{out}/gmail.mbox", "wb").write(mbox.getvalue())
    gm = b"".join(
        b"From x@y Mon Jan  6 10:00:00 2020\nX-Gmail-Labels: " + lab + b"\nFrom: " + fr +
        b"\nTo: you@gmail.com\nSubject: " + sub + b"\nDate: Mon, 6 Jan 2020 10:00:00 +0000\n\nbody\n\n"
        for lab, fr, sub in [(b"Inbox,Unread,Important", b"news@shop.com", b"Sale"),
                             (b"Sent", b"you@gmail.com", b"Hello"),
                             (b"Trash,Opened", b"spam@x.com", b"Old"),
                             (b'Opened,"Receipts, 2019"', b"store@x.com", b"Your receipt")])
    open(f"{out}/takeout.mbox", "wb").write(gm)
    # damaged files
    _m = build_msg(b)
    open(f"{out}/truncated.msg", "wb").write(_m[:len(_m) - 3072])  # directory and FAT lost
    open(f"{out}/broken.mbox", "wb").write(b"Subject: no separator\n\nbody\n\n" + mbox.getvalue()[:300])
    with zipfile.ZipFile(f"{out}/backup.zip", "w") as z:
        z.writestr("msgs/lunch.eml", build_eml(a))
        z.writestr("msgs/photos.msg", build_msg(b))
        z.writestr("mail/gmail.mbox", mbox.getvalue())
        inner = io.BytesIO()
        with tarfile.open(fileobj=inner, mode="w:gz") as t:
            ti = tarfile.TarInfo("old/lunch.eml")
            d = build_eml(a)
            ti.size = len(d)
            t.addfile(ti, io.BytesIO(d))
        z.writestr("nested/old.tar.gz", inner.getvalue())
    # meeting invitations (two requests and a cancellation) for the calendar tests
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_invites import eml as invite_eml
    with open(f"{out}/invites.mbox", "wb") as fh:
        for raw in (invite_eml(4), invite_eml(8), invite_eml(9, "CANCEL", "STATUS:CANCELLED\r\n")):
            fh.write(b"From - Mon May 4 10:00:00 2026\r\n" + raw + b"\r\n")
    print("fixtures written to", out)


if __name__ == "__main__":
    main()
