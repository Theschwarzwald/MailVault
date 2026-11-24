"""Outlook .msg reading and writing (MS-OXMSG)."""
from __future__ import annotations

import re
import struct

from .cfb import CFBReader, CFBWriter, Entry
from .mapi import (NameMap, FIXED_SIZES, MV_FLAG, P, PT_BINARY, PT_LONG, PT_OBJECT, PT_STRING8,
                   PT_SYSTIME, PT_UNICODE, PT_BOOLEAN, codepage_name, decode_fixed, decode_var,
                   make_attachment, props_to_message, to_filetime)
from .mime import html_to_text
from .model import DAMAGED, Attachment, Message

_SUBSTG = re.compile(r"__SUBSTG1\.0_([0-9A-F]{4})([0-9A-F]{4})(?:-([0-9A-F]{8}))?$")


def _read_props(cfb: CFBReader, store: Entry, header_size: int) -> dict:
    props: dict = {}
    pstream = store.children.get("__PROPERTIES_VERSION1.0")
    if pstream is not None:
        raw = cfb.read(pstream)
        for off in range(header_size, len(raw) - 15, 16):
            tag, _flags = struct.unpack_from("<II", raw, off)
            ptype, pid = tag & 0xFFFF, tag >> 16
            if ptype in FIXED_SIZES:
                props[pid] = decode_fixed(ptype, raw[off + 8: off + 16])
                props[("t", pid)] = ptype
    cp = codepage_name(props.get(P["MESSAGE_CPID"]) or props.get(P["INTERNET_CPID"]))
    for name, e in store.children.items():
        mt = _SUBSTG.match(name)
        if not mt or e.type != 2:
            continue
        pid, ptype = int(mt.group(1), 16), int(mt.group(2), 16)
        if mt.group(3):
            continue
        if ptype & MV_FLAG:
            base = ptype & ~MV_FLAG
            if base in (PT_UNICODE, PT_STRING8, PT_BINARY):
                vals = sorted((k, v) for k, v in store.children.items() if k.startswith(name + "-"))
                props[pid] = [decode_var(base, cfb.read(v), cp) for _, v in vals]
                props[("t", pid)] = ptype
            continue
        props[pid] = decode_var(ptype, cfb.read(e), cp)
        props[("t", pid)] = ptype
    return props


def _namemap(cfb: CFBReader) -> NameMap:
    nm = cfb.root.children.get("__NAMEID_VERSION1.0")
    if nm is None:
        return NameMap()
    g = nm.children.get("__SUBSTG1.0_00020102")
    e = nm.children.get("__SUBSTG1.0_00030102")
    st = nm.children.get("__SUBSTG1.0_00040102")
    return NameMap(cfb.read(g) if g else b"", cfb.read(e) if e else b"", cfb.read(st) if st else b"")


def _read_message(cfb: CFBReader, store: Entry, top: bool, nmap: NameMap | None = None) -> Message:
    props = _read_props(cfb, store, 32 if top else 24)
    recips = []
    atts: list[Attachment] = []
    for name, e in sorted(store.children.items()):
        if name.startswith("__RECIP_VERSION1.0_"):
            recips.append(_read_props(cfb, e, 8))
        elif name.startswith("__ATTACH_VERSION1.0_"):
            aprops = _read_props(cfb, e, 8)
            emb = None
            obj = e.children.get("__SUBSTG1.0_3701000D")
            if obj is not None and obj.type == 1:
                try:
                    emb = _read_message(cfb, obj, False, nmap)
                except Exception as ex:  # noqa: BLE001
                    emb = Message(subject="(damaged embedded message)", status=DAMAGED,
                                  problems=[str(ex)])
            data = aprops.get(P["ATTACH_DATA"])
            if emb is not None:
                a = make_attachment(aprops, None, emb)
                a.mime_type = "application/vnd.ms-outlook"
            else:
                d = data if isinstance(data, bytes) else b""
                a = make_attachment(aprops, None)
                a._data = d
                a.size = len(d)
            atts.append(a)
    return props_to_message(props, recips, atts, "msg", nmap)


def parse_msg(data: bytes) -> Message:
    cfb = CFBReader(data)
    m = _read_message(cfb, cfb.root, True, _namemap(cfb))
    if cfb.warnings:
        m.status = DAMAGED
        m.problems += cfb.warnings
    return m


# ------------------------------------------------------------------ writer
def _prop_streams(w: CFBWriter, base: str, props: dict, header: bytes):
    """props: pid -> (ptype, value). Writes __properties_version1.0 and value streams."""
    from .pstwrite import FIXED, encode_value
    from .propmap import MV, OBJECT, UNI, STR8, BIN
    fixed = bytearray(header)
    for pid, (ptype, val) in sorted(props.items()):
        if val is None:
            continue
        tag = (pid << 16) | ptype
        if ptype == OBJECT:
            fixed += struct.pack("<IIII", tag, 0x6, 0xFFFFFFFF, 0x4)
            continue
        if ptype in FIXED:
            raw = encode_value(ptype, val).ljust(8, b"\0")
            fixed += struct.pack("<II", tag, 0x6) + raw
            continue
        name = f"{base}/__substg1.0_{pid:04X}{ptype:04X}"
        if ptype & MV:
            b = ptype & ~MV
            vals = val if isinstance(val, (list, tuple)) else [val]
            if b in FIXED:
                data = b"".join(encode_value(b, v) for v in vals)
                w.add(name, data)
                fixed += struct.pack("<IIII", tag, 0x6, len(data), 0)
                continue
            items = []
            for i, v in enumerate(vals):
                d = encode_value(b, v) + (b"\0\0" if b == UNI else b"\0" if b == STR8 else b"")
                w.add(f"{name}-{i:08X}", d)
                items.append(d)
            lens = b"".join(struct.pack("<I", len(d)) if b != BIN else struct.pack("<II", len(d), 0) for d in items)
            w.add(name, lens)
            fixed += struct.pack("<IIII", tag, 0x6, len(lens), 0)
            continue
        data = encode_value(ptype, val)
        if ptype == UNI:
            data += b"\0\0"
        elif ptype == STR8:
            data += b"\0"
        w.add(name, data)
        fixed += struct.pack("<IIII", tag, 0x6, len(data), 0)
    w.add(f"{base}/__properties_version1.0", bytes(fixed))


def _write_message(w: CFBWriter, base: str, m: Message, top: bool, namer):
    from .propmap import LONG, OBJECT, UNI, attachment_props, message_props, recipient_rows
    props = message_props(m, namer)
    props[0x3FFD] = (LONG, 65001)
    props[0x3FDE] = (LONG, 65001)
    props[0x340D] = (LONG, 0x00040000)
    recips = recipient_rows(m)
    atts = m.attachments
    hdr = struct.pack("<QIIII", 0, len(recips), len(atts), len(recips), len(atts))
    if top:
        hdr += b"\0" * 8
    _prop_streams(w, base, props, hdr)
    for i, r in enumerate(recips):
        rb = f"{base}/__recip_version1.0_#{i:08X}"
        w.storage(rb)
        r = dict(r)
        r[0x3000] = (LONG, i)
        _prop_streams(w, rb, r, b"\0" * 8)
    for i, a in enumerate(atts):
        ab = f"{base}/__attach_version1.0_#{i:08X}"
        w.storage(ab)
        ap = attachment_props(a, i)
        if a.embedded is not None:
            ap[0x3705] = (LONG, 5)
            ap[0x3701] = (OBJECT, b"")
            _prop_streams(w, ab, ap, b"\0" * 8)
            sub = f"{ab}/__substg1.0_3701000D"
            w.storage(sub)
            _write_message(w, sub, a.embedded, False, namer)
        else:
            from .propmap import BIN
            ap[0x3705] = (LONG, 1)
            ap[0x3701] = (BIN, a.data or b"")
            ap[0x0E20] = (LONG, len(a.data or b""))
            _prop_streams(w, ab, ap, b"\0" * 8)


def _nameid_streams(w: CFBWriter, namer):
    import uuid as _uuid
    from .mapi import PS_MAPI, PS_PUBLIC_STRINGS
    from .pst import pst_crc
    nm = "__nameid_version1.0"
    w.storage(nm)
    guids, entries, strings = [], b"", b""
    buckets: dict[int, bytes] = {}
    for i, (kind, guid, ident) in enumerate(namer.keys):
        if guid == PS_MAPI:
            g = 1
        elif guid == PS_PUBLIC_STRINGS:
            g = 2
        else:
            gb = _uuid.UUID(guid).bytes_le
            if gb not in guids:
                guids.append(gb)
            g = guids.index(gb) + 3
        if kind == "name":
            raw = ident.encode("utf-16-le")
            off = len(strings)
            strings += struct.pack("<I", len(raw)) + raw
            strings += b"\0" * ((-len(strings)) % 4)
            wg = (g << 1) | 1
            entries += struct.pack("<IHH", off, wg, i)
            hval = pst_crc(raw)
        else:
            wg = g << 1
            entries += struct.pack("<IHH", ident, wg, i)
            hval = ident
        # MSG "property name to ID" mapping streams: one per stream ID (0x1000 + hash % 0x1F)
        sid = 0x1000 + ((hval ^ wg) % 0x1F)   # MS-OXMSG: ID xor (GUID index << 1 | N), mod 0x1F
        buckets[sid] = buckets.get(sid, b"") + struct.pack("<IHH", hval, wg, i)
    w.add(f"{nm}/__substg1.0_00020102", b"".join(guids))
    w.add(f"{nm}/__substg1.0_00030102", entries)
    w.add(f"{nm}/__substg1.0_00040102", strings)
    for sid, data in buckets.items():
        w.add(f"{nm}/__substg1.0_{sid:04X}0102", data)


def build_msg(m: Message) -> bytes:
    from .propmap import Namer
    w = CFBWriter()
    namer = Namer()
    _write_message(w, "", m, True, namer)
    _nameid_streams(w, namer)
    return w.tobytes()
