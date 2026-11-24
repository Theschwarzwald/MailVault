"""Pure-Python PST/OST reader (MS-PST) with recovery of damaged, orphaned and
deleted items.

Layers implemented:
  NDB  - header, NBT/BBT B-trees, blocks, XBLOCK/XXBLOCK, SLBLOCK/SIBLOCK
  LTP  - heap-on-node, BTH, property context (PC), table context (TC)
  Messaging - stores, folders, messages, recipients, attachments, embedded msgs

Recovery strategy:
  * Every block CRC is checked; mismatches are reported but parsing continues.
  * Folders are rebuilt from the parent links stored in the node B-tree, so a
    broken hierarchy table does not hide mail.
  * Messages whose parent folder is missing, or that the folder's contents
    table does not list, are placed in "Orphaned items".
  * The whole file is scanned for stale node/block B-tree pages; message nodes
    found there that are no longer live are offered under "Recovered items".
"""
from __future__ import annotations

import binascii
import struct
import zlib
from dataclasses import dataclass

from .mapi import (FIXED_SIZES, MV_FLAG, P, PT_BINARY, PT_BOOLEAN, PT_CLSID, PT_ERROR, PT_I2,
                   PT_LONG, PT_OBJECT, PT_R4, PT_STRING8, PT_UNICODE, NameMap, codepage_name,
                   decode_fixed, decode_mv, decode_var, filetime, kind_of, make_attachment, props_to_message)
from .model import VIEWS, normalize_topic
from .state import from_mapi
from .model import DAMAGED, ORPHANED, RECOVERED, Attachment, Folder, Message

# Decoding table for NDB_CRYPT_PERMUTE ("compressible encryption")
_PERMUTE_DEC = bytes.fromhex(
    "47f1b4e60b6a7248854e9eebe2f89453e0bba002e85a09abdbe3bac67cc310dd"
    "39059630f53760828cc9134a6b1df3fb8f2697ca911701c4322d6e3195ffd923"
    "d1005e79dc443b1a28c561572090 3d83b943be67d2464276c06d5b7eb20f1629"
    "3ca903540dda5ddff6b7c762cd8d06d3695c86d614f7a56675acb1e94521700c"
    "879f74a4224c6fbf1f56aa2eb3783350b0a392bccf191ca763cb1e4d3e4b1b9b"
    "4fe7f0eead3ab55904ea40552551e57a893868527bfc27aed7bdfa07f4cc8e5f"
    "ef359c842b15d5773449b6120a7f7188fd9d18417d93d8582ccefe24afdeb836"
    "c8a180a69998a82f0e816573e4c2a28ad4e111d0088b2af2ed9a643fc16cf9ec".replace(" ", ""))
_PERMUTE_ENC = bytes(_PERMUTE_DEC.index(i) for i in range(256))
_DEC_TABLE = bytes.maketrans(bytes(range(256)), _PERMUTE_DEC)
_ENC_TABLE = bytes.maketrans(bytes(range(256)), _PERMUTE_ENC)

NID_MESSAGE_STORE, NID_ROOT_FOLDER, NID_NAME_TO_ID_MAP = 0x21, 0x122, 0x61
NID_TYPE_FOLDER, NID_TYPE_SEARCH_FOLDER, NID_TYPE_MESSAGE = 0x02, 0x03, 0x04
NID_TYPE_ATTACHMENT, NID_TYPE_ASSOC_MESSAGE, NID_TYPE_CONTENTS_TABLE = 0x05, 0x08, 0x0E
NID_RECIPIENT_TABLE, NID_ATTACHMENT_TABLE = 0x692, 0x671
PROP_ROW_ID = 0x67F2


class PSTError(Exception):
    pass


def _block_sig(ib: int, bid: int) -> int:
    """MS-PST ComputeSig for block/page trailers."""
    ib ^= bid
    return ((ib >> 16) ^ ib) & 0xFFFF


def pst_crc(data: bytes) -> int:
    """MS-PST CRC: CRC-32 with zero init and no final xor."""
    return (~binascii.crc32(data, 0xFFFFFFFF)) & 0xFFFFFFFF


def encrypt_permute(data: bytes) -> bytes:
    return data.translate(_ENC_TABLE)


@dataclass
class NodeRef:
    nid: int
    bid_data: int
    bid_sub: int
    parent: int
    source: str = "live"     # live | carved


class PST:
    def __init__(self, data: bytes, name: str = "file.pst"):
        self.data, self.name = data, name
        self.problems: list[str] = []
        if data[:4] != b"!BDN":
            raise PSTError("Not a PST/OST file (bad magic)")
        self.ver = struct.unpack_from("<H", data, 10)[0]
        self.client = data[8:10]
        self.unicode = self.ver >= 23
        self.page = 4096 if self.ver >= 36 else 512
        if self.unicode:
            self.crypt = data[513]
            self.nbt_bid, self.nbt_ib = struct.unpack_from("<QQ", data, 216)
            self.bbt_bid, self.bbt_ib = struct.unpack_from("<QQ", data, 232)
            self.trailer, self.max_block = 16, (65536 if self.ver >= 36 else 8192) - 16
        else:
            self.crypt = data[461]
            self.nbt_bid, self.nbt_ib = struct.unpack_from("<II", data, 184)
            self.bbt_bid, self.bbt_ib = struct.unpack_from("<II", data, 192)
            self.trailer, self.max_block = 12, 8192 - 12
        if self.crypt not in (0, 1):
            self.problems.append(f"Encryption method {self.crypt} (high/cyclic) is not supported; "
                                 "data may be unreadable")
        self.bbt: dict[int, tuple[int, int]] = {}
        self.nbt: dict[int, NodeRef] = {}
        self.page_ibs: set[int] = set()
        self._walk_btree(self.bbt_ib, leaf_type=0x80, seen=set())
        self._walk_btree(self.nbt_ib, leaf_type=0x81, seen=set())
        self.carved_nodes: dict[int, NodeRef] = {}
        self.carved_blocks: dict[int, tuple[int, int]] = {}
        self._namemap: NameMap | None = None

    @property
    def namemap(self) -> NameMap:
        if self._namemap is None:
            try:
                p = self.props_of(NID_NAME_TO_ID_MAP, [])
                g, e, st = p.get(0x0002), p.get(0x0003), p.get(0x0004)
                b = lambda v: v if isinstance(v, bytes) else b""  # noqa: E731
                self._namemap = NameMap(b(g), b(e), b(st))
            except Exception as ex:  # noqa: BLE001
                self.problems.append(f"Named property map unreadable: {ex}")
                self._namemap = NameMap()
        return self._namemap

    # ------------------------------------------------------------ NDB layer
    def _page(self, ib: int, expect: int | None) -> tuple[bytes, int] | None:
        pg = self.data[ib: ib + self.page]
        if len(pg) < self.page:
            self.problems.append(f"Page at {ib:#x} beyond end of file")
            return None
        t = pg[-self.trailer:]
        ptype, prep = t[0], t[1]
        if ptype != prep or (expect is not None and ptype != expect and ptype not in (0x80, 0x81)):
            self.problems.append(f"Bad page header at {ib:#x}")
            return None
        crc = struct.unpack_from("<I", t, 4 if self.unicode else 8)[0]
        if self.page == 512 and pst_crc(pg[:-self.trailer]) != crc:
            self.problems.append(f"Page CRC mismatch at {ib:#x} (using it anyway)")
        return pg, ptype

    def _btpage_meta(self, pg: bytes):
        if self.page == 4096:
            c_ent, _max = struct.unpack_from("<HH", pg, 4056)
            cb_ent, level = pg[4060], pg[4061]
        elif self.unicode:
            c_ent, _max, cb_ent, level = pg[488], pg[489], pg[490], pg[491]
        else:
            c_ent, _max, cb_ent, level = pg[496], pg[497], pg[498], pg[499]
        return c_ent, cb_ent, level

    def _parse_leaf(self, pg: bytes, ptype: int, c_ent: int, cb_ent: int, carved=False):
        out = []
        for i in range(c_ent):
            off = i * cb_ent
            if self.unicode:
                if ptype == 0x80:
                    bid, ib, cb, _ref = struct.unpack_from("<QQHH", pg, off)
                    out.append(("b", bid & ~1, ib, cb))
                else:
                    nid, bd, bs, par = struct.unpack_from("<QQQI", pg, off)
                    out.append(("n", NodeRef(nid & 0xFFFFFFFF, bd, bs, par, "carved" if carved else "live")))
            else:
                if ptype == 0x80:
                    bid, ib, cb, _ref = struct.unpack_from("<IIHH", pg, off)
                    out.append(("b", bid & ~1, ib, cb))
                else:
                    nid, bd, bs, par = struct.unpack_from("<IIII", pg, off)
                    out.append(("n", NodeRef(nid, bd, bs, par, "carved" if carved else "live")))
        return out

    def _walk_btree(self, ib: int, leaf_type: int, seen: set, depth: int = 0):
        if ib in seen or depth > 16:
            return
        seen.add(ib)
        self.page_ibs.add(ib)
        r = self._page(ib, leaf_type)
        if not r:
            return
        pg, ptype = r
        c_ent, cb_ent, level = self._btpage_meta(pg)
        if cb_ent == 0 or c_ent * cb_ent > len(pg):
            self.problems.append(f"Corrupt B-tree page at {ib:#x}")
            return
        if level == 0:
            for e in self._parse_leaf(pg, ptype, c_ent, cb_ent):
                if e[0] == "b":
                    self.bbt[e[1]] = (e[2], e[3])
                else:
                    self.nbt[e[1].nid] = e[1]
        else:
            for i in range(c_ent):
                off = i * cb_ent
                if self.unicode:
                    _key, _bid, child = struct.unpack_from("<QQQ", pg, off)
                else:
                    _key, _bid, child = struct.unpack_from("<III", pg, off)
                self._walk_btree(child, leaf_type, seen, depth + 1)

    def carve(self):
        """Scan every page slot for stale B-tree leaf pages (deleted data)."""
        step = self.page
        for ib in range(step * 2, len(self.data) - step + 1, step):
            pg = self.data[ib: ib + step]
            t = pg[-self.trailer:]
            if t[0] not in (0x80, 0x81) or t[0] != t[1]:
                continue
            crc = struct.unpack_from("<I", t, 4 if self.unicode else 8)[0]
            if step == 512 and pst_crc(pg[:-self.trailer]) != crc:
                continue
            c_ent, cb_ent, level = self._btpage_meta(pg)
            if level != 0 or not cb_ent or c_ent * cb_ent > len(pg) - self.trailer:
                continue
            for e in self._parse_leaf(pg, t[0], c_ent, cb_ent, carved=True):
                if e[0] == "b":
                    if e[1] not in self.bbt:
                        self.carved_blocks[e[1]] = (e[2], e[3])
                elif e[1].nid not in self.nbt:
                    self.carved_nodes.setdefault(e[1].nid, e[1])

    def _locate(self, bid: int) -> tuple[int, int]:
        b = bid & ~1
        loc = self.bbt.get(b) or self.carved_blocks.get(b)
        if not loc:
            raise PSTError(f"Block {bid:#x} not found in block B-tree")
        return loc

    def read_block(self, bid: int, problems: list | None = None) -> bytes:
        ib, cb = self._locate(bid)
        raw = self.data[ib: ib + cb]
        if len(raw) < cb:
            raise PSTError(f"Block {bid:#x} truncated by end of file")
        size = (cb + self.trailer + 63) & ~63
        t = self.data[ib + size - self.trailer: ib + size]
        if len(t) == self.trailer:
            crc = struct.unpack_from("<I", t, 4 if self.unicode else 8)[0]
            if pst_crc(raw) != crc and problems is not None:
                problems.append(f"CRC mismatch in block {bid:#x}")
        if self.ver >= 36 and raw[:2] in (b"\x78\x9c", b"\x78\x01", b"\x78\xda"):
            try:  # OST 2013+ may zlib-compress blocks
                raw = zlib.decompress(raw)
            except zlib.error:
                pass
        if not (bid & 2) and self.crypt == 1:
            raw = raw.translate(_DEC_TABLE)
        return raw

    def node_blocks(self, bid: int, problems: list) -> list[bytes]:
        """Expand XBLOCK/XXBLOCK trees into a list of data blocks."""
        raw = self.read_block(bid, problems)
        if not (bid & 2):
            return [raw]
        btype, level, c_ent = raw[0], raw[1], struct.unpack_from("<H", raw, 2)[0]
        if btype != 1:
            raise PSTError(f"Unexpected internal block type {btype}")
        w = 8 if self.unicode else 4
        bids = struct.unpack_from(f"<{c_ent}{'Q' if self.unicode else 'I'}", raw, 8)
        out = []
        for b in bids:
            try:
                if level == 2:
                    out += self.node_blocks(b, problems)
                else:
                    out.append(self.read_block(b, problems))
            except Exception as e:  # noqa: BLE001
                problems.append(f"Missing data block: {e}")
                out.append(b"")
        del w
        return out

    def subnodes(self, bid: int, problems: list) -> dict[int, tuple[int, int]]:
        if not bid:
            return {}
        raw = self.read_block(bid, problems)
        btype, level, c_ent = raw[0], raw[1], struct.unpack_from("<H", raw, 2)[0]
        if btype != 2:
            raise PSTError("Bad subnode block")
        out: dict[int, tuple[int, int]] = {}
        if self.unicode:
            base = 8
            if level == 0:
                for i in range(c_ent):
                    nid, bd, bs = struct.unpack_from("<QQQ", raw, base + i * 24)
                    out[nid & 0xFFFFFFFF] = (bd, bs)
            else:
                for i in range(c_ent):
                    _nid, b = struct.unpack_from("<QQ", raw, base + i * 16)
                    out.update(self.subnodes(b, problems))
        else:
            base = 4
            if level == 0:
                for i in range(c_ent):
                    nid, bd, bs = struct.unpack_from("<III", raw, base + i * 12)
                    out[nid] = (bd, bs)
            else:
                for i in range(c_ent):
                    _nid, b = struct.unpack_from("<II", raw, base + i * 8)
                    out.update(self.subnodes(b, problems))
        return out

    # ----------------------------------------------------------- LTP layer
    def node(self, bid_data: int, bid_sub: int, problems: list) -> "LTPNode":
        return LTPNode(self, bid_data, bid_sub, problems)

    # ------------------------------------------------------ messaging layer
    def node_of(self, nid: int) -> NodeRef | None:
        return self.nbt.get(nid) or self.carved_nodes.get(nid)

    def props_of(self, nid: int, problems: list) -> dict:
        ref = self.node_of(nid)
        if not ref:
            raise PSTError(f"Node {nid:#x} missing")
        return self.node(ref.bid_data, ref.bid_sub, problems).pc()

    def message(self, nid: int, status: str = "ok") -> Message:
        try:
            return self._message(nid, status)
        except Exception as e:  # noqa: BLE001
            m = Message(subject=f"(unreadable item {nid:#x})", status=DAMAGED, source_format="pst")
            m.problems.append(f"{type(e).__name__}: {e}")
            return m

    def _message(self, nid: int, status: str) -> Message:
        problems: list[str] = []
        ref = self.node_of(nid)
        if not ref:
            raise PSTError(f"Message node {nid:#x} missing")
        node = self.node(ref.bid_data, ref.bid_sub, problems)
        return self._message_from_node(node, problems, status)

    def _message_from_node(self, node: "LTPNode", problems: list, status: str) -> Message:
        props = node.pc()
        recips: list[dict] = []
        try:
            if NID_RECIPIENT_TABLE in node.subs:
                recips = node.sub(NID_RECIPIENT_TABLE).tc()
        except Exception as e:  # noqa: BLE001
            problems.append(f"Recipient table unreadable: {e}")
        atts: list[Attachment] = []
        for snid in sorted(node.subs):
            if snid & 0x1F != NID_TYPE_ATTACHMENT:
                continue
            try:
                atts.append(self._attachment(node.sub(snid), problems))
            except Exception as e:  # noqa: BLE001
                problems.append(f"Attachment {snid:#x} unreadable: {e}")
        m = props_to_message(props, recips, atts, "pst", self.namemap)
        m.problems = problems + m.problems
        if status != "ok":
            m.status = status
        elif m.problems:
            m.status = DAMAGED
        return m

    def _attachment(self, an: "LTPNode", problems: list) -> Attachment:
        props = an.pc(defer_binary={P["ATTACH_DATA"]})
        method = props.get(P["ATTACH_METHOD"])
        obj = props.get(("object", P["ATTACH_DATA"]))
        if method == 5 and obj:
            sub_nid = obj
            emb = self._message_from_node(an.sub(sub_nid), [], "ok")
            a = make_attachment(props, None, emb)
            a.mime_type = "application/vnd.ms-outlook"
            return a
        lazy = props.get(("lazy", P["ATTACH_DATA"]))
        a = make_attachment(props, lazy)
        if not a.size and lazy is None:
            a._data = b""
        return a

    # Folder tree -----------------------------------------------------
    def build_tree(self) -> Folder:
        self.carve()
        store_name = self.name
        ipm_nid = None
        try:
            sp = self.props_of(NID_MESSAGE_STORE, [])
            store_name = sp.get(0x3001) or store_name
            eid = sp.get(0x35E0)
            if isinstance(eid, bytes) and len(eid) >= 24:
                ipm_nid = struct.unpack_from("<I", eid, 20)[0]
        except Exception as e:  # noqa: BLE001
            self.problems.append(f"Message store properties unreadable: {e}")

        folders: dict[int, Folder] = {}
        fparent: dict[int, int] = {}
        for nid, ref in self.nbt.items():
            if nid & 0x1F in (NID_TYPE_FOLDER, NID_TYPE_SEARCH_FOLDER):
                name, err, cls = f"Folder {nid:#x}", "", ""
                try:
                    fp = self.props_of(nid, [])
                    name = fp.get(0x3001) or name
                    cls = fp.get(0x3613) or ""
                except Exception as e:  # noqa: BLE001
                    err = str(e)
                f = Folder(name=str(name), kind="search" if nid & 0x1F == 3 else "folder", error=err)
                cls = str(cls)
                f.view = next((v for k, v in VIEWS.items() if cls.startswith(k)), "mail")
                f.extra_nid = nid  # type: ignore[attr-defined]
                folders[nid] = f
                fparent[nid] = ref.parent
        for nid, f in folders.items():
            p = fparent[nid]
            if p != nid and p in folders:
                folders[p].children.append(f)

        def reaches_root(nid: int) -> bool:
            seen = set()
            while nid not in seen:
                seen.add(nid)
                if nid == NID_ROOT_FOLDER:
                    return True
                if nid not in fparent:
                    return False
                nid = fparent[nid]
            return False

        listed: dict[int, dict | None] = {}
        orphan_folder = Folder("Orphaned items", kind="orphaned")
        recovered_folder = Folder("Recovered items", kind="recovered")
        for nid, ref in self.nbt.items():
            if nid & 0x1F != NID_TYPE_MESSAGE:
                continue
            par = ref.parent
            if par in folders and reaches_root(par):
                rows = listed.get(par)
                if rows is None:
                    rows = listed[par] = self._contents_rows(par)
                if rows is not None and nid not in rows:
                    orphan_folder.items.append(lambda n=nid: self.message(n, ORPHANED))
                    orphan_folder.quick.append(None)
                    continue
                folders[par].items.append(lambda n=nid: self.message(n))
                row = rows.get(nid) if rows else None
                folders[par].quick.append(
                    (lambda r=row: _quick_summary(r)) if row and folders[par].view == "mail" and 0x0037 in row else None)
            else:
                orphan_folder.items.append(lambda n=nid: self.message(n, ORPHANED))
                orphan_folder.quick.append(None)
        for nid, ref in self.carved_nodes.items():
            if nid & 0x1F == NID_TYPE_MESSAGE:
                recovered_folder.items.append(lambda n=nid: self.message(n, RECOVERED))
                recovered_folder.quick.append(None)
        for nid, f in folders.items():
            if f.kind == "folder" and not reaches_root(nid) and nid != NID_ROOT_FOLDER:
                f.kind = "orphaned"
                orphan_folder.children.append(f)

        root = Folder(str(store_name), kind="store")
        top = folders.get(ipm_nid) if ipm_nid else None
        if top is None:
            top = folders.get(NID_ROOT_FOLDER)
        if top is not None:
            root.children = [c for c in top.children]
            root.items, root.quick = top.items, top.quick
        else:
            self.problems.append("Root folder missing; showing all folders flat")
            root.children = [f for f in folders.values() if f.kind != "orphaned"]
        _sort(root)
        if orphan_folder.items or orphan_folder.children:
            root.children.append(orphan_folder)
        if recovered_folder.items:
            root.children.append(recovered_folder)
        if self.problems:
            root.error = "; ".join(self.problems[:5])
        return root

    def _contents_rows(self, folder_nid: int) -> dict | None:
        """Rows of a folder's contents table, keyed by message NID. These carry
        subject/sender/date/size, so folder lists need no per-message parsing."""
        cnid = (folder_nid & ~0x1F) | NID_TYPE_CONTENTS_TABLE
        ref = self.nbt.get(cnid)
        if not ref:
            return None
        try:
            rows = self.node(ref.bid_data, ref.bid_sub, []).tc(columns=QUICK_COLUMNS)
            return {r.get(PROP_ROW_ID): r for r in rows if r.get(PROP_ROW_ID) is not None}
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ deep scan
    def deep_scan(self, progress=None, cancelled=None) -> list[Message]:
        """Rebuild items from blocks no index points to any more.

        1. Find every valid block (trailer + signature + CRC) in space that no
           live block or B-tree page occupies.
        2. Classify them: heap blocks holding a message property context,
           XBLOCKs (multi-block heaps / big values) and SLBLOCKs (subnodes:
           attachments, recipient tables, embedded messages).
        3. Link: an XBLOCK whose first child is a message heap is that
           message's data; the nearest unclaimed SLBLOCK (by block ID, which
           Outlook allocates sequentially) holding a recipient table or
           attachments becomes its subnode tree.
        4. Rebuild each message with the normal reader, so attachments,
           recipients and embedded messages come back too.
        """
        orphans = self._scan_free_space(progress, cancelled)
        if cancelled and cancelled():
            return []
        for bid, loc in orphans.items():
            self.carved_blocks.setdefault(bid, loc)
        heaps: dict[int, bytes] = {}
        xblocks: dict[int, list[int]] = {}
        slblocks: dict[int, dict] = {}
        for bid in orphans:
            try:
                raw = self.read_block(bid)
            except Exception:  # noqa: BLE001
                continue
            if bid & 2:
                if raw[:1] == b"\x01":
                    try:
                        c = struct.unpack_from("<H", raw, 2)[0]
                        xblocks[bid] = list(struct.unpack_from(f"<{c}{'Q' if self.unicode else 'I'}", raw, 8))
                    except struct.error:
                        pass
                elif raw[:1] == b"\x02" and raw[1] == 0:
                    try:
                        slblocks[bid] = self.subnodes(bid, [])
                    except Exception:  # noqa: BLE001
                        pass
            elif len(raw) >= 12 and raw[2] == 0xEC and raw[3] == 0xBC:
                heaps[bid] = raw
        used_by_live = {r.bid_data & ~1 for r in self.carved_nodes.values()}
        # Data roots: XBLOCKs whose first child is a heap, otherwise the heap block itself
        roots: dict[int, int] = {}      # first heap bid -> data root bid
        for xb, kids in xblocks.items():
            if kids and (kids[0] & ~1) in heaps:
                roots[kids[0] & ~1] = xb
        for hb in heaps:
            roots.setdefault(hb, hb)
        # SLBLOCKs referenced from other SLBLOCKs belong to attachments, not messages
        nested = {bs & ~1 for sl in slblocks.values() for _, bs in sl.values() if bs}
        candidates_sl = {b: sl for b, sl in slblocks.items() if b not in nested}
        messages: list[tuple[int, int]] = []
        for hb, root in roots.items():
            if hb in used_by_live:
                continue
            try:
                node = LTPNode(self, root, 0, [])
                props = node.pc()
            except Exception:  # noqa: BLE001
                continue
            cls = props.get(0x001A)
            if isinstance(cls, str) and cls.upper().startswith("IPM.") and \
                    (props.get(0x0037) or props.get(0x1000) or props.get(0x0C1A)):
                messages.append((hb, root))
        # Nearest-bid pairing of messages with subnode trees
        pairs = sorted((abs(sb - hb), hb, sb) for hb, _ in messages for sb, sl in candidates_sl.items()
                       if abs(sb - hb) <= 4096 and any(n == NID_RECIPIENT_TABLE or n & 0x1F == NID_TYPE_ATTACHMENT
                                                       for n in sl))
        link: dict[int, int] = {}
        taken: set[int] = set()
        for _, hb, sb in pairs:
            if hb not in link and sb not in taken:
                link[hb] = sb
                taken.add(sb)
        found: list[Message] = []
        for hb, root in messages:
            problems = [f"Rebuilt from unreferenced block {hb:#x}"]
            sb = link.get(hb, 0)
            m = None
            if sb:
                try:
                    node = LTPNode(self, root, sb, list(problems))
                    m = self._message_from_node(node, node.problems, RECOVERED)
                    m.problems.append("Attachments and recipients were matched to this item by block position; "
                                      "check they belong to it")
                except Exception:  # noqa: BLE001
                    m = None
            if m is None:
                try:
                    node = LTPNode(self, root, 0, list(problems))
                    m = self._message_from_node(node, node.problems, RECOVERED)
                    m.problems.append("No attachments or recipient list could be linked to this item")
                except Exception:  # noqa: BLE001
                    continue
            found.append(m)
        return found

    def _scan_free_space(self, progress=None, cancelled=None) -> dict[int, tuple[int, int]]:
        """Find valid blocks outside every live block and B-tree page."""
        extents = sorted([(ib, ib + ((cb + self.trailer + 63) & ~63)) for ib, cb in self.bbt.values()] +
                         [(ib, ib + self.page) for ib in self.page_ibs])
        start0 = 0x4400 if self.unicode else 0x4400
        gaps, pos = [], start0
        for a, b in extents:
            if a > pos:
                gaps.append((pos, a))
            pos = max(pos, b)
        if pos < len(self.data):
            gaps.append((pos, len(self.data)))
        total = sum(b - a for a, b in gaps) or 1
        done = 0
        out: dict[int, tuple[int, int]] = {}
        data, tl, uni = self.data, self.trailer, self.unicode
        unpack = struct.unpack_from
        for ga, gb in gaps:
            ga = (ga + 63) & ~63
            for end in range(ga + 64, gb + 1, 64):
                if uni:
                    cb, sig, crc, bid = unpack("<HHIQ", data, end - 16)
                else:
                    cb, sig, bid, crc = unpack("<HHII", data, end - 12)
                if not cb or cb > self.max_block or not bid or bid & 1:
                    continue
                start = end - ((cb + tl + 63) & ~63)
                if start < ga:
                    continue
                if sig and sig != _block_sig(start, bid):
                    continue
                if pst_crc(bytes(data[start:start + cb])) != crc:
                    continue
                if bid not in self.bbt:
                    out[bid] = (start, cb)
                if cancelled and (end & 0xFFFFF) == 0 and cancelled():
                    return out
            done += gb - ga
            if progress:
                progress(done, total)
        return out

QUICK_COLUMNS = {PROP_ROW_ID, 0x0037, 0x0042, 0x0C1A, 0x0E04, 0x0E06, 0x0039, 0x0E08, 0x0E07,
                 0x0017, 0x1090, 0x001A, 0x0070, 0x0065, 0x0C1F, 0x1080, 0x1081, 0x301F, 0x3018}


def _quick_summary(r: dict) -> dict:
    subj = r.get(0x0037) or ""
    if isinstance(subj, str) and subj[:1] == "\x01":
        subj = subj[2:]
    name = r.get(0x0042) or r.get(0x0C1A) or ""
    email = r.get(0x0065) or r.get(0x0C1F) or ""
    date = r.get(0x0E06) or r.get(0x0039)
    flags = r.get(0x0E07) or 0
    return {"subject": subj or "(no subject)", "from": (f"{name} <{email}>" if name and email and name != email
                                                       else name or email) or "(no sender)",
            "from_name": name or email or "(no sender)", "to": (r.get(0x0E04) or "").replace(";", ","),
            "date": date.isoformat() if date else None, "size": r.get(0x0E08) or 0,
            "attachments": bool(flags & 0x10), "importance": {0: "low", 2: "high"}.get(r.get(0x0017), "normal"),
            "status": "ok", "unread": not (flags & 0x1), "flagged": r.get(0x1090) == 2,
            "kind": kind_of(r.get(0x001A) or ""), "topic": normalize_topic(subj),
            "states": from_mapi(r)[0], "categories": [], "sender_email": email}


def _sort(f: Folder):
    order = ["inbox", "drafts", "sent items", "deleted items", "outbox", "junk email"]
    f.children.sort(key=lambda c: (order.index(c.name.lower()) if c.name.lower() in order else 99, c.name.lower()))
    for c in f.children:
        _sort(c)


class LTPNode:
    """A node's data blocks interpreted as a heap-on-node."""

    def __init__(self, pst: PST, bid_data: int, bid_sub: int, problems: list):
        self.pst, self.problems = pst, problems
        self.blocks = pst.node_blocks(bid_data, problems) if bid_data else []
        try:
            self.subs = pst.subnodes(bid_sub, problems)
        except Exception as e:  # noqa: BLE001
            problems.append(f"Subnodes unreadable: {e}")
            self.subs = {}

    def sub(self, nid: int) -> "LTPNode":
        if nid not in self.subs:
            raise PSTError(f"Subnode {nid:#x} missing")
        bd, bs = self.subs[nid]
        return LTPNode(self.pst, bd, bs, self.problems)

    def sub_bytes(self, nid: int) -> bytes:
        bd, _ = self.subs[nid]
        return b"".join(self.pst.node_blocks(bd, self.problems))

    # heap ------------------------------------------------------------
    def hn_header(self):
        b = self.blocks[0]
        if len(b) < 12 or b[2] != 0xEC:
            raise PSTError("Not a heap-on-node (bad signature)")
        return b[3], struct.unpack_from("<I", b, 4)[0]

    def heap(self, hid: int) -> bytes:
        if hid == 0:
            return b""
        idx, blk = (hid >> 5) & 0x7FF, hid >> 16
        if blk >= len(self.blocks):
            raise PSTError(f"HID {hid:#x} block out of range")
        b = self.blocks[blk]
        ibm = struct.unpack_from("<H", b, 0)[0]
        c_alloc = struct.unpack_from("<H", b, ibm)[0]
        if idx < 1 or idx > c_alloc:
            raise PSTError(f"HID {hid:#x} index out of range")
        s, e = struct.unpack_from("<HH", b, ibm + 4 + (idx - 1) * 2)
        return b[s:e]

    def hnid(self, v: int) -> bytes:
        if v & 0x1F == 0:
            return self.heap(v)
        return self.sub_bytes(v)

    def bth(self, hid: int) -> tuple[int, int, list[tuple[bytes, bytes]]]:
        h = self.heap(hid)
        if h[0] != 0xB5:
            raise PSTError("Bad BTH header")
        cb_key, cb_ent, levels, root = h[1], h[2], h[3], struct.unpack_from("<I", h, 4)[0]
        out: list = []

        def walk(hid_, lvl):
            if not hid_:
                return
            d = self.heap(hid_)
            step = cb_key + (cb_ent if lvl == 0 else 4)
            for o in range(0, len(d) - step + 1, step):
                if lvl == 0:
                    out.append((d[o:o + cb_key], d[o + cb_key:o + step]))
                else:
                    walk(struct.unpack_from("<I", d, o + cb_key)[0], lvl - 1)
        walk(root, levels)
        return cb_key, cb_ent, out

    def _value(self, ptype: int, raw4: bytes, cp: str, pid: int, defer: set, props: dict):
        if ptype in (PT_I2, PT_LONG, PT_R4, PT_ERROR, PT_BOOLEAN):
            return decode_fixed(ptype, raw4)
        v = struct.unpack_from("<I", raw4)[0]
        if ptype == PT_OBJECT:
            d = self.heap(v) if v & 0x1F == 0 else b""
            if len(d) >= 4:
                props[("object", pid)] = struct.unpack_from("<I", d)[0]
            return None
        if pid in defer and v:
            props[("lazy", pid)] = lambda v=v: self.hnid(v)
            return None
        data = self.hnid(v) if v else b""
        if ptype in FIXED_SIZES:
            return decode_fixed(ptype, data)
        if ptype & MV_FLAG:
            return decode_mv(ptype, data, cp)
        return decode_var(ptype, data, cp)

    def pc(self, defer_binary: set | None = None) -> dict:
        client, root = self.hn_header()
        if client != 0xBC:
            raise PSTError("Node is not a property context")
        _k, _e, recs = self.bth(root)
        props: dict = {}
        raw = []
        for k, d in recs:
            pid = struct.unpack_from("<H", k)[0]
            ptype = struct.unpack_from("<H", d)[0]
            raw.append((pid, ptype, d[2:6]))
        cp = "cp1252"
        for pid, ptype, v in raw:
            if pid in (P["MESSAGE_CPID"], P["INTERNET_CPID"]) and ptype == PT_LONG:
                cp = codepage_name(decode_fixed(PT_LONG, v))
        for pid, ptype, v in raw:
            props[("t", pid)] = ptype
            try:
                val = self._value(ptype, v, cp, pid, defer_binary or set(), props)
                if val is not None or (ptype not in (PT_OBJECT,) and pid not in (defer_binary or ())):
                    props[pid] = val
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"Property {pid:#06x} unreadable: {e}")
        return props

    def tc(self, columns: set | None = None) -> list[dict]:
        client, root = self.hn_header()
        if client != 0x7C:
            raise PSTError("Node is not a table context")
        info = self.heap(root)
        c_cols = info[1]
        ib4, ib2, ib1, ibm = struct.unpack_from("<HHHH", info, 2)
        hid_rows_idx, hnid_rows = struct.unpack_from("<II", info, 10)
        cols = []
        for i in range(c_cols):
            tag, ibd, cbd, ibit = struct.unpack_from("<IHBB", info, 22 + i * 8)
            cols.append((tag & 0xFFFF, tag >> 16, ibd, cbd, ibit))
        if not hnid_rows:
            return []
        rowsize = ibm
        if hnid_rows & 0x1F == 0:
            chunks = [self.heap(hnid_rows)]
        else:
            bd, _ = self.subs[hnid_rows]
            chunks = self.pst.node_blocks(bd, self.problems)
        rows_data = []
        for ch in chunks:
            per = len(ch) // rowsize if rowsize else 0
            rows_data += [ch[i * rowsize:(i + 1) * rowsize] for i in range(per)]
        try:
            _ck, _ce, idx = self.bth(hid_rows_idx)
            n_rows = len(idx)
            rows_data = rows_data[:n_rows] if n_rows else rows_data
        except Exception:  # noqa: BLE001
            pass
        out = []
        for r in rows_data:
            row: dict = {}
            for ptype, pid, ibd, cbd, ibit in cols:
                if columns and pid not in columns:
                    continue
                if ib1 + ibit // 8 >= len(r) or not (r[ib1 + ibit // 8] >> (7 - ibit % 8)) & 1:
                    continue
                cell = r[ibd: ibd + cbd]
                try:
                    if pid == PROP_ROW_ID:
                        row[pid] = struct.unpack_from("<I", cell)[0]
                    elif ptype in FIXED_SIZES and cbd >= FIXED_SIZES[ptype]:
                        row[pid] = decode_fixed(ptype, cell)
                    elif ptype in (PT_UNICODE, PT_STRING8, PT_BINARY, PT_CLSID):
                        v = struct.unpack_from("<I", cell)[0]
                        row[pid] = decode_var(ptype, self.hnid(v) if v else b"")
                except Exception:  # noqa: BLE001
                    continue
            out.append(row)
        del ib4, ib2
        return out


def open_pst(name: str, data: bytes) -> Folder:
    pst = PST(data, name)
    root = pst.build_tree()
    root.kind = "store"
    root.pst = pst  # type: ignore[attr-defined]  # kept for deep scan
    return root
