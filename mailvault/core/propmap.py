"""Turn a Message into MAPI properties for the PST and MSG writers.

Properties come from two places:
  * the model (subject, people, dates, bodies, item fields, categories), so
    EML/MBOX sources produce complete Outlook items, and
  * for PST/MSG sources, the original MAPI properties, carried over verbatim
    (named properties are remapped by name), so calendar recurrence, reminders,
    contact details, task data and other Outlook-specific fields survive.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import struct

from .mapi import (PS_PUBLIC_STRINGS, PSETID_ADDRESS, PSETID_APPOINTMENT, PSETID_COMMON, PSETID_NOTE,
                   PSETID_TASK, to_filetime)
from .mime import html_to_text
from .model import Message

# property types
I2, LONG, R4, DOUBLE, CURRENCY, APPTIME, ERROR, BOOL, OBJECT, I8 = 0x2, 0x3, 0x4, 0x5, 0x6, 0x7, 0xA, 0xB, 0xD, 0x14
STR8, UNI, TIME, GUID, BIN = 0x1E, 0x1F, 0x40, 0x48, 0x102
MV = 0x1000

# Not copied from source items: store-specific, recomputed, or written separately
SKIP = {0x0E08, 0x0E20, 0x0E21, 0x0FF9, 0x0FFF, 0x0FFE, 0x3700, 0x3701, 0x3702, 0x3703,
        0x3705, 0x3707, 0x370E, 0x3712, 0x0E1D}
SKIP_RANGES = [(0x0E30, 0x0E3F), (0x67F0, 0x67FF), (0x6600, 0x66FF), (0x3FF0, 0x3FF9)]


def _skip(pid: int) -> bool:
    return pid in SKIP or any(a <= pid <= b for a, b in SKIP_RANGES)


class Namer:
    """Assigns file-local ids (0x8000+) to named properties."""

    def __init__(self):
        self.keys: list[tuple] = []      # ('lid'|'name', guid, lid|name)
        self.index: dict[tuple, int] = {}

    def pid(self, kind: str, guid: str, ident) -> int:
        key = (kind, guid.lower(), ident.lower() if kind == "name" else ident)
        if key not in self.index:
            self.index[key] = 0x8000 + len(self.keys)
            self.keys.append((kind, guid.lower(), ident))
        return self.index[key]


def _search_key(m: Message) -> bytes:
    return hashlib.md5(f"{m.message_id}|{m.subject}|{m.date}|{m.sender}".encode()).digest()


def model_props(m: Message, namer: Namer) -> dict:
    """Properties derived from the format-independent model: pid -> (ptype, value)."""
    p: dict = {}
    def put(pid, t, v):
        if v not in (None, "", b""):
            p[pid] = (t, v)
    cls = {"appointment": "IPM.Appointment", "contact": "IPM.Contact", "task": "IPM.Task",
           "note": "IPM.StickyNote"}.get(m.kind, "IPM.Note")
    put(0x001A, UNI, m.extra.get("message_class") or cls)
    put(0x0037, UNI, m.subject)
    put(0x0070, UNI, m.subject)
    sname, semail = m.sender.name or m.sender.email, m.sender.email
    for pid, val in ((0x0C1A, sname), (0x0042, sname), (0x0C1F, semail), (0x0065, semail),
                     (0x5D01, semail), (0x5D02, semail)):
        put(pid, UNI, val)
    if semail:
        for pid in (0x0C1E, 0x0064):
            put(pid, UNI, "SMTP")
    put(0x0E04, UNI, "; ".join(a.name or a.email for a in m.to))
    put(0x0E03, UNI, "; ".join(a.name or a.email for a in m.cc))
    put(0x0E02, UNI, "; ".join(a.name or a.email for a in m.bcc))
    if m.date:
        for pid in (0x0039, 0x0E06, 0x3007, 0x3008):
            put(pid, TIME, m.date)
    else:
        now = dt.datetime.now(dt.timezone.utc)
        put(0x3007, TIME, now)
        put(0x3008, TIME, now)
    body = m.body_text or (html_to_text(m.body_html) if m.body_html else "")
    put(0x1000, UNI, body)
    if m.body_html:
        put(0x1013, BIN, m.body_html.encode("utf-8"))
        put(0x3FDE, LONG, 65001)
    put(0x1035, UNI, m.message_id)
    put(0x1042, UNI, m.in_reply_to)
    put(0x007D, UNI, m.headers if m.source_format in ("eml", "mbox") or m.headers else "")
    imp = {"low": 0, "high": 2}.get(m.importance, 1)
    p[0x0017] = (LONG, imp)
    p[0x0026] = (LONG, {0: -1, 1: 0, 2: 1}[imp])     # PidTagPriority
    p[0x0036] = (LONG, 0)
    flags = 0
    st = set(m.states or [])
    if "Read" in st or ("Unread" not in st and m.source_format not in ("pst", "msg")):
        flags |= 0x1
    if "Draft" in st or "draft" in m.flags:
        flags |= 0x8
    if "Sent" in st:
        flags |= 0x20
    if any(not a.inline for a in m.attachments):
        flags |= 0x10
    p[0x0E07] = (LONG, flags)
    p[0x0E17] = (LONG, 0)
    if "Flagged" in st or "flagged" in m.flags:
        p[0x1090] = (LONG, 2)
    elif "Completed" in st:
        p[0x1090] = (LONG, 1)
    if "Replied" in st:
        p[0x1081] = (LONG, 102)
    elif "Replied all" in st:
        p[0x1081] = (LONG, 103)
    elif "Forwarded" in st:
        p[0x1081] = (LONG, 104)
    p[0x300B] = (BIN, _search_key(m))
    if m.categories:
        p[namer.pid("name", PS_PUBLIC_STRINGS, "Keywords")] = (MV | UNI, list(m.categories))
    it = m.item
    n = lambda g, lid: namer.pid("lid", g, lid)  # noqa: E731
    if m.kind == "appointment":
        s, e = it.get("start") or m.date, it.get("end") or it.get("start") or m.date
        if s:
            p[n(PSETID_APPOINTMENT, 0x820D)] = (TIME, s)
            p[0x0060] = (TIME, s)
            p[n(PSETID_COMMON, 0x8516)] = (TIME, s)       # PidLidCommonStart
        if e:
            p[n(PSETID_APPOINTMENT, 0x820E)] = (TIME, e)
            p[0x0061] = (TIME, e)
            p[n(PSETID_COMMON, 0x8517)] = (TIME, e)
        if s and e:
            p[n(PSETID_APPOINTMENT, 0x8213)] = (LONG, int((e - s).total_seconds() // 60))  # duration
        if it.get("location"):
            p[n(PSETID_APPOINTMENT, 0x8208)] = (UNI, it["location"])
        p[n(PSETID_APPOINTMENT, 0x8215)] = (BOOL, bool(it.get("all_day")))
        p[n(PSETID_APPOINTMENT, 0x8205)] = (LONG, {"free": 0, "tentative": 1, "busy": 2, "out of office": 3,
                                                   "working elsewhere": 4}.get(it.get("busy", "busy"), 2))
        p[n(PSETID_APPOINTMENT, 0x8223)] = (BOOL, bool(it.get("recurring")))
    elif m.kind == "contact":
        for pid, key in ((0x3001, "display_name"), (0x3A06, "given"), (0x3A11, "surname"), (0x3A44, "middle"),
                         (0x3A16, "company"), (0x3A17, "job_title"), (0x3A18, "department"), (0x3A1C, "mobile"),
                         (0x3A08, "business_phone"), (0x3A09, "home_phone"), (0x3A24, "business_fax"),
                         (0x3A51, "web")):
            if it.get(key):
                p[pid] = (UNI, it[key])
        if isinstance(it.get("birthday"), dt.datetime):
            p[0x3A42] = (TIME, it["birthday"])
        for lid, key in ((0x8083, "email"), (0x8093, "email2"), (0x80A3, "email3")):
            if it.get(key):
                p[n(PSETID_ADDRESS, lid)] = (UNI, it[key])
                p[n(PSETID_ADDRESS, lid - 1)] = (UNI, "SMTP")                 # address type
                p[n(PSETID_ADDRESS, lid - 3)] = (UNI, f"{it.get('display_name', '')} ({it[key]})")  # display
        p[0x3001] = p.get(0x3001) or (UNI, m.subject)
    elif m.kind == "task":
        if isinstance(it.get("due"), dt.datetime):
            p[n(PSETID_TASK, 0x8105)] = (TIME, it["due"])
        if isinstance(it.get("start"), dt.datetime):
            p[n(PSETID_TASK, 0x8104)] = (TIME, it["start"])
        status = {"Not started": 0, "In progress": 1, "Completed": 2, "Waiting": 3, "Deferred": 4}.get(
            it.get("task_status", "Not started"), 0)
        p[n(PSETID_TASK, 0x8101)] = (LONG, status)
        p[n(PSETID_TASK, 0x8102)] = (DOUBLE, (it.get("percent") or 0) / 100.0)
        p[n(PSETID_TASK, 0x811C)] = (BOOL, bool(it.get("complete")))
    elif m.kind == "note":
        p[n(PSETID_NOTE, 0x8B00)] = (LONG, {"blue": 0, "green": 1, "pink": 2, "yellow": 3, "white": 4}.get(
            it.get("color", "yellow"), 3))
    return p


def source_props(m: Message, namer: Namer) -> dict:
    """Original MAPI properties of a PST/MSG item, named ones remapped."""
    raw = m.extra.get("props") or {}
    nm = m.extra.get("namemap")
    out: dict = {}
    for k, v in raw.items():
        if not isinstance(k, int) or v is None:
            continue
        t = raw.get(("t", k))
        if t is None or t in (ERROR, OBJECT) or t & 0xF000 not in (0, MV):
            continue
        pid = k
        if k >= 0x8000:
            key = nm.key_of(k) if nm is not None else None
            if not key:
                continue
            pid = namer.pid(*key)
        elif _skip(k):
            continue
        out[pid] = (t, v)
    return out


def message_props(m: Message, namer: Namer) -> dict:
    p = model_props(m, namer)
    if m.source_format in ("pst", "msg"):
        p.update(source_props(m, namer))
    return p


def recipient_rows(m: Message) -> list[dict]:
    rows = []
    for rtype, lst in ((1, m.to), (2, m.cc), (3, m.bcc)):
        for a in lst:
            email = a.email or ""
            name = a.name or email
            rows.append({0x0C15: (LONG, rtype), 0x3001: (UNI, name), 0x3002: (UNI, "SMTP" if email else ""),
                         0x3003: (UNI, email), 0x39FE: (UNI, email), 0x39FF: (UNI, name), 0x0FFE: (LONG, 6),
                         0x3900: (LONG, 0), 0x0E0F: (BOOL, False), 0x3A40: (BOOL, False),
                         0x300B: (BIN, ("SMTP:" + email.upper()).encode() + b"\0"),
                         0x0FF9: (BIN, hashlib.md5(email.lower().encode()).digest())})
    return rows


ATT_SKIP = {0x3701, 0x3705, 0x0E20, 0x0E21, 0x0FF9, 0x0FFF, 0x370B}


def attachment_props(a, index: int) -> dict:
    p = {0x3704: (UNI, a.filename[:255]), 0x3707: (UNI, a.filename), 0x3001: (UNI, a.filename),
         0x370E: (UNI, a.mime_type or "application/octet-stream"), 0x370B: (LONG, -1), 0x0E21: (LONG, index),
         0x0FFE: (LONG, 7)}
    if a.content_id:
        p[0x3712] = (UNI, a.content_id)
    if a.inline:
        p[0x3714] = (LONG, 0x4)
        p[0x7FFE] = (BOOL, True)
    ext = "." + a.filename.rsplit(".", 1)[-1] if "." in a.filename else ""
    if ext:
        p[0x3703] = (UNI, ext)
    if a.exception:
        p[0x7FFE] = (BOOL, True)
        p[0x3714] = (LONG, 0x2)
    raw = getattr(a, "props", None) or {}
    for k, v in raw.items():           # original attachment properties (exception times, flags, ...)
        t = raw.get(("t", k))
        if isinstance(k, int) and k < 0x8000 and v is not None and t is not None and k not in ATT_SKIP \
                and not _skip(k) and t not in (ERROR, OBJECT):
            p[k] = (t, v)
    return p


def filetime_bytes(d) -> bytes:
    return struct.pack("<Q", to_filetime(d))
