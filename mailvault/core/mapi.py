"""MAPI property handling shared by the MSG and PST readers."""
from __future__ import annotations

import datetime as _dt
import struct
import uuid

from .model import DAMAGED, Address, Attachment, Message
from .rtf import decompress_rtf, rtf_to_html, rtf_to_text

# Property types
PT_I2, PT_LONG, PT_R4, PT_DOUBLE, PT_CURRENCY, PT_APPTIME = 0x2, 0x3, 0x4, 0x5, 0x6, 0x7
PT_ERROR, PT_BOOLEAN, PT_OBJECT, PT_I8 = 0xA, 0xB, 0xD, 0x14
PT_STRING8, PT_UNICODE, PT_SYSTIME, PT_CLSID, PT_BINARY = 0x1E, 0x1F, 0x40, 0x48, 0x102
MV_FLAG = 0x1000

FIXED_SIZES = {PT_I2: 2, PT_LONG: 4, PT_R4: 4, PT_DOUBLE: 8, PT_CURRENCY: 8, PT_APPTIME: 8,
               PT_ERROR: 4, PT_BOOLEAN: 1, PT_I8: 8, PT_SYSTIME: 8}

# Property ids
P = dict(
    MESSAGE_CLASS=0x001A, SUBJECT=0x0037, CLIENT_SUBMIT_TIME=0x0039, SENT_REP_NAME=0x0042,
    SENT_REP_EMAIL=0x0065, IMPORTANCE=0x0017, TRANSPORT_HEADERS=0x007D,
    SENDER_NAME=0x0C1A, SENDER_EMAIL=0x0C1F, SENDER_SMTP=0x5D01, SENT_REP_SMTP=0x5D02,
    DISPLAY_BCC=0x0E02, DISPLAY_CC=0x0E03, DISPLAY_TO=0x0E04, DELIVERY_TIME=0x0E06,
    MESSAGE_FLAGS=0x0E07, MESSAGE_SIZE=0x0E08, BODY=0x1000, RTF_COMPRESSED=0x1009,
    HTML=0x1013, INTERNET_MESSAGE_ID=0x1035, IN_REPLY_TO=0x1042, FLAG_STATUS=0x1090,
    DISPLAY_NAME=0x3001, EMAIL_ADDRESS=0x3003, SMTP_ADDRESS=0x39FE, RECIPIENT_TYPE=0x0C15,
    ATTACH_DATA=0x3701, ATTACH_FILENAME=0x3704, ATTACH_METHOD=0x3705, ATTACH_LONG_FILENAME=0x3707,
    ATTACH_SIZE=0x0E20, ATTACH_MIME=0x370E, ATTACH_CID=0x3712, ATTACH_FLAGS=0x3714,
    ATTACHMENT_HIDDEN=0x7FFE, INTERNET_CPID=0x3FDE, MESSAGE_CPID=0x3FFD, CREATION_TIME=0x3007,
    LAST_MOD_TIME=0x3008, DISPLAY_NAME_FOLDER=0x3001, CONTENT_COUNT=0x3602,
)


def filetime(v: int) -> _dt.datetime | None:
    if not v or v >= 0x7FFFFFFFFFFFFFFF:
        return None
    try:
        return _dt.datetime(1601, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(microseconds=v // 10)
    except OverflowError:
        return None


def to_filetime(d: _dt.datetime | None) -> int:
    if not d:
        return 0
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return int((d - _dt.datetime(1601, 1, 1, tzinfo=_dt.timezone.utc)).total_seconds() * 10_000_000)


def decode_fixed(ptype: int, raw: bytes):
    try:
        if ptype == PT_I2:
            return struct.unpack_from("<h", raw)[0]
        if ptype in (PT_LONG, PT_ERROR):
            return struct.unpack_from("<i", raw)[0]
        if ptype == PT_R4:
            return struct.unpack_from("<f", raw)[0]
        if ptype in (PT_DOUBLE, PT_APPTIME):
            return struct.unpack_from("<d", raw)[0]
        if ptype in (PT_I8, PT_CURRENCY):
            return struct.unpack_from("<q", raw)[0]
        if ptype == PT_BOOLEAN:
            return bool(raw[0])
        if ptype == PT_SYSTIME:
            return filetime(struct.unpack_from("<Q", raw)[0])
    except struct.error:
        return None
    return raw


def decode_var(ptype: int, raw: bytes, codepage: str = "cp1252"):
    if ptype == PT_UNICODE:
        return raw.decode("utf-16-le", "replace").rstrip("\0")
    if ptype == PT_STRING8:
        try:
            return raw.decode(codepage).rstrip("\0")
        except (LookupError, UnicodeDecodeError):
            return raw.decode("cp1252", "replace").rstrip("\0")
    if ptype == PT_CLSID:
        return str(uuid.UUID(bytes_le=raw[:16])) if len(raw) >= 16 else raw
    return raw


def decode_mv(ptype: int, data: bytes, codepage: str = "cp1252") -> list:
    """Multi-valued property as stored in a PST heap/subnode."""
    base = ptype & ~MV_FLAG
    try:
        if base in FIXED_SIZES:
            sz = FIXED_SIZES[base] if base != PT_BOOLEAN else 2
            return [decode_fixed(base, data[i:i + sz]) for i in range(0, len(data) - sz + 1, sz)]
        if len(data) < 4:
            return []
        n = struct.unpack_from("<I", data)[0]
        if n > len(data) // 4:
            return []
        offs = list(struct.unpack_from(f"<{n}I", data, 4)) + [len(data)]
        return [decode_var(base, data[offs[i]:offs[i + 1]], codepage) for i in range(n)]
    except struct.error:
        return []


def codepage_name(cpid) -> str:
    if not isinstance(cpid, int) or cpid <= 0:
        return "cp1252"
    return {65001: "utf-8", 20127: "ascii", 28591: "latin-1", 1200: "utf-16-le",
            50220: "iso2022_jp", 51932: "euc_jp", 936: "gbk", 950: "big5"}.get(cpid, f"cp{cpid}")


def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        for enc in ("utf-8", "cp1252"):
            try:
                return v.decode(enc)
            except UnicodeDecodeError:
                pass
        return v.decode("latin-1", "replace")
    return str(v)


def _split_display(s: str) -> list[Address]:
    return [Address(name=p.strip()) for p in (s or "").split(";") if p.strip()]


def props_to_message(props: dict, recipients: list[dict], attachments: list[Attachment],
                     source_format: str, namemap: "NameMap | None" = None) -> Message:
    g = props.get
    m = Message(source_format=source_format)
    m.extra["message_class"] = g(P["MESSAGE_CLASS"]) or ""
    m.subject = _as_text(g(P["SUBJECT"]))
    # Strip the 2-char subject prefix marker some writers leave (\x01\x01)
    if m.subject[:1] == "\x01" and len(m.subject) > 2:
        m.subject = m.subject[2:]
    name = g(P["SENT_REP_NAME"]) or g(P["SENDER_NAME"]) or ""
    addr = g(P["SENDER_SMTP"]) or g(P["SENT_REP_SMTP"]) or g(P["SENDER_EMAIL"]) or g(P["SENT_REP_EMAIL"]) or ""
    m.sender = Address(_as_text(name), _as_text(addr))
    m.date = g(P["CLIENT_SUBMIT_TIME"]) or g(P["DELIVERY_TIME"]) or g(P["CREATION_TIME"])
    m.headers = _as_text(g(P["TRANSPORT_HEADERS"]))
    m.message_id = _as_text(g(P["INTERNET_MESSAGE_ID"]))
    m.in_reply_to = _as_text(g(P["IN_REPLY_TO"]))
    imp = g(P["IMPORTANCE"])
    m.importance = {0: "low", 2: "high"}.get(imp, "normal")
    flags = g(P["MESSAGE_FLAGS"]) or 0
    if isinstance(flags, int):
        if flags & 0x1:
            m.flags.add("read")
        if flags & 0x8:
            m.flags.add("draft")
    if g(P["FLAG_STATUS"]) == 2:
        m.flags.add("flagged")

    for r in recipients:
        a = Address(_as_text(r.get(P["DISPLAY_NAME"])),
                    _as_text(r.get(P["SMTP_ADDRESS"]) or r.get(P["EMAIL_ADDRESS"])))
        {2: m.cc, 3: m.bcc}.get(r.get(P["RECIPIENT_TYPE"]), m.to).append(a)
    if not (m.to or m.cc or m.bcc):
        m.to = _split_display(_as_text(g(P["DISPLAY_TO"])))
        m.cc = _split_display(_as_text(g(P["DISPLAY_CC"])))
        m.bcc = _split_display(_as_text(g(P["DISPLAY_BCC"])))

    body = g(P["BODY"])
    m.body_text = _as_text(body)
    html = g(P["HTML"])
    if html is not None:
        if isinstance(html, bytes):
            cp = codepage_name(g(P["INTERNET_CPID"]))
            try:
                html = html.decode(cp)
            except (LookupError, UnicodeDecodeError):
                html = html.decode("utf-8", "replace")
        m.body_html = html
    rtfc = g(P["RTF_COMPRESSED"])
    if isinstance(rtfc, bytes) and rtfc:
        try:
            m.body_rtf = decompress_rtf(rtfc)
            if not m.body_html and b"\\fromhtml1" in m.body_rtf[:1024]:
                m.body_html = rtf_to_html(m.body_rtf)
            if not m.body_text:
                m.body_text = rtf_to_text(m.body_rtf)
            if not m.body_html and not m.body_text:
                m.body_html = rtf_to_html(m.body_rtf)
        except Exception as e:  # noqa: BLE001
            m.problems.append(f"RTF body unreadable: {e}")
    m.attachments = attachments
    from .state import from_mapi
    m.states, info = from_mapi(props)
    m.extra.update(info)
    if namemap is not None:
        cats = namemap.get_named(props, PS_PUBLIC_STRINGS, "Keywords")
        if isinstance(cats, list):
            m.categories = [str(c) for c in cats if c]
        elif isinstance(cats, str) and cats:
            m.categories = [cats]
    m.kind = kind_of(m.extra["message_class"])
    if m.kind != "mail":
        try:
            m.item = item_fields(m.kind, props, namemap)
        except Exception as e:  # noqa: BLE001
            m.problems.append(f"Item fields unreadable: {e}")
        if m.kind == "contact" and not m.subject:
            m.subject = m.item.get("display_name", "")
        if m.kind == "appointment":
            link_exceptions(m)
    if m.problems:
        m.status = DAMAGED
    m.extra["props"] = props
    m.extra["namemap"] = namemap
    return m


def make_attachment(props: dict, data_loader, embedded: Message | None = None) -> Attachment:
    g = props.get
    fn = _as_text(g(P["ATTACH_LONG_FILENAME"]) or g(P["ATTACH_FILENAME"])) or "attachment.bin"
    mime = _as_text(g(P["ATTACH_MIME"])) or ("message/rfc822" if embedded else "application/octet-stream")
    cid = _as_text(g(P["ATTACH_CID"]))
    inline = bool(g(P["ATTACHMENT_HIDDEN"])) or bool((g(P["ATTACH_FLAGS"]) or 0) & 0x4)
    a = Attachment(filename=fn, mime_type=mime, content_id=cid, inline=inline and bool(cid),
                   loader=data_loader, embedded=embedded)
    a.props = {k: v for k, v in props.items() if not (isinstance(k, tuple) and k[0] == "lazy")}
    cls = (embedded.extra.get("message_class") or "").lower() if embedded is not None else ""
    if embedded is not None and ((g(P["ATTACH_FLAGS"]) or 0) & 0x2 or cls.startswith("ipm.ole.class.{00061055")):
        a.exception = True
        a.inline = True
    sz = g(P["ATTACH_SIZE"])
    a.size = sz if isinstance(sz, int) else 0
    if embedded is not None and not fn.lower().endswith((".msg", ".eml")):
        a.filename = (embedded.subject or "message") + ".msg"
    return a


# ------------------------------------------------------------ named props
PS_MAPI = "00020328-0000-0000-c000-000000000046"
PS_PUBLIC_STRINGS = "00020329-0000-0000-c000-000000000046"
PSETID_APPOINTMENT = "00062002-0000-0000-c000-000000000046"
PSETID_TASK = "00062003-0000-0000-c000-000000000046"
PSETID_ADDRESS = "00062004-0000-0000-c000-000000000046"
PSETID_COMMON = "00062008-0000-0000-c000-000000000046"
PSETID_NOTE = "0006200e-0000-0000-c000-000000000046"


class NameMap:
    """Maps (property set GUID, LID) to the file-local property id (0x8000+)."""

    def __init__(self, guids: bytes = b"", entries: bytes = b"", strings: bytes = b""):
        self.by_lid: dict[tuple[str, int], int] = {}
        self.by_name: dict[tuple[str, str], int] = {}
        self.case: dict[tuple[str, str], str] = {}
        glist = [str(uuid.UUID(bytes_le=guids[i:i + 16])) for i in range(0, len(guids) - 15, 16)]
        for off in range(0, len(entries) - 7, 8):
            pid, wguid, widx = struct.unpack_from("<IHH", entries, off)
            g = wguid >> 1
            guid = PS_MAPI if g == 1 else PS_PUBLIC_STRINGS if g == 2 else \
                (glist[g - 3] if 0 <= g - 3 < len(glist) else None)
            if not guid:
                continue
            if wguid & 1:          # string-named: pid is an offset into the string stream
                if pid + 4 <= len(strings):
                    ln = struct.unpack_from("<I", strings, pid)[0]
                    name = strings[pid + 4: pid + 4 + ln].decode("utf-16-le", "replace")
                    self.by_name[(guid, name.lower())] = 0x8000 + widx
                    self.case[(guid, name.lower())] = name
            else:
                self.by_lid[(guid, pid)] = 0x8000 + widx

    def key_of(self, pid: int):
        """Reverse lookup: file-local id -> ('lid', guid, lid) or ('name', guid, name)."""
        if not hasattr(self, "_rev"):
            self._rev = {v: ("lid", g, k) for (g, k), v in self.by_lid.items()}
            self._rev.update({v: ("name", g, self.case.get((g, n), n)) for (g, n), v in self.by_name.items()})
            self._names = {}
        return self._rev.get(pid)

    def get_named(self, props: dict, guid: str, name: str):
        pid = self.by_name.get((guid, name.lower()))
        return props.get(pid) if pid is not None else None

    def get(self, props: dict, guid: str, lid: int):
        pid = self.by_lid.get((guid, lid))
        return props.get(pid) if pid is not None else None


EMPTY_NAMEMAP = NameMap()


def kind_of(message_class: str) -> str:
    mc = (message_class or "").lower()
    if mc.startswith(("ipm.appointment", "ipm.schedule.meeting", "ipm.ole.class.{00061055")):
        return "appointment"
    if mc.startswith(("ipm.contact", "ipm.distlist")):
        return "contact"
    if mc.startswith(("ipm.task",)):
        return "task"
    if mc.startswith("ipm.stickynote"):
        return "note"
    return "mail"


def item_fields(kind: str, props: dict, nm: NameMap | None) -> dict:
    nm = nm or EMPTY_NAMEMAP
    g = props.get
    n = lambda guid, lid: nm.get(props, guid, lid)  # noqa: E731
    out: dict = {}
    if kind == "appointment":
        out["start"] = n(PSETID_APPOINTMENT, 0x820D) or g(0x0060)
        out["end"] = n(PSETID_APPOINTMENT, 0x820E) or g(0x0061)
        out["location"] = _as_text(n(PSETID_APPOINTMENT, 0x8208))
        out["all_day"] = bool(n(PSETID_APPOINTMENT, 0x8215))
        out["recurring"] = bool(n(PSETID_APPOINTMENT, 0x8223))
        blob = n(PSETID_APPOINTMENT, 0x8216)
        if isinstance(blob, (bytes, bytearray)) and len(blob) > 20:
            from .recur import Recurrence, TZRule
            try:
                rec = Recurrence.parse(bytes(blob))
                tzb = n(PSETID_APPOINTMENT, 0x8233)
                tz = TZRule.parse(tzb)
                out["recurring"] = True
                out["recurrence"] = rec.describe()
                out["occurrences"] = [[o["start"].isoformat(), o["end"].isoformat(), o.get("subject"), o.get("location"),
                                       None, o["original"].isoformat()] for o in rec.occurrences(tz)]
                out["_recur"] = (rec, tz, _as_text(n(PSETID_APPOINTMENT, 0x8234)))
            except Exception as e:  # noqa: BLE001
                out["recurrence"] = f"Recurring (pattern unreadable: {e})"
        rt = n(PSETID_APPOINTMENT, 0x8228)                 # PidLidExceptionReplaceTime
        if rt is not None:
            out["_replace_time"] = rt
        busy = n(PSETID_APPOINTMENT, 0x8205)
        out["busy"] = {0: "free", 1: "tentative", 2: "busy", 3: "out of office", 4: "working elsewhere"}.get(busy, "")
    elif kind == "contact":
        out.update({
            "display_name": _as_text(g(0x3001)), "given": _as_text(g(0x3A06)), "surname": _as_text(g(0x3A11)),
            "middle": _as_text(g(0x3A44)), "company": _as_text(g(0x3A16)), "job_title": _as_text(g(0x3A17)),
            "department": _as_text(g(0x3A18)),
            "email": _as_text(n(PSETID_ADDRESS, 0x8083)), "email2": _as_text(n(PSETID_ADDRESS, 0x8093)),
            "email3": _as_text(n(PSETID_ADDRESS, 0x80A3)),
            "phone": _as_text(g(0x3A1C) or g(0x3A08) or g(0x3A09)), "mobile": _as_text(g(0x3A1C)),
            "business_phone": _as_text(g(0x3A08)), "home_phone": _as_text(g(0x3A09)),
            "business_fax": _as_text(g(0x3A24)), "web": _as_text(g(0x3A51)),
            "birthday": g(0x3A42),
            "work_address": ", ".join(x for x in (_as_text(n(PSETID_ADDRESS, k)) for k in (0x8045, 0x8046, 0x8047, 0x8048, 0x8049)) if x),
            "home_address": ", ".join(x for x in (_as_text(g(k)) for k in (0x3A5D, 0x3A59, 0x3A5C, 0x3A5B, 0x3A5A)) if x),
        })
        if not out["email"]:
            out["email"] = _as_text(g(0x39FE) or g(0x3003))
    elif kind == "task":
        out["due"] = n(PSETID_TASK, 0x8105)
        out["start"] = n(PSETID_TASK, 0x8104)
        st = n(PSETID_TASK, 0x8101)
        out["task_status"] = {0: "Not started", 1: "In progress", 2: "Completed", 3: "Waiting", 4: "Deferred"}.get(st, "Not started")
        pct = n(PSETID_TASK, 0x8102)
        out["percent"] = int(round((pct or 0) * 100)) if isinstance(pct, (int, float)) else 0
        out["complete"] = bool(n(PSETID_TASK, 0x811C)) or st == 2
    elif kind == "note":
        out["color"] = {0: "blue", 1: "green", 2: "pink", 3: "yellow", 4: "white"}.get(n(PSETID_NOTE, 0x8B00), "yellow")
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def link_exceptions(m: Message):
    """Attach each changed occurrence's full details (body, attendees, ...) to the series."""
    from .mime import html_to_text
    atts = [a for a in m.attachments if a.exception and a.embedded is not None]
    if not atts:
        return
    exc, msgs = [], []
    for a in atts:
        e = a.embedded
        orig = a.props.get(0x7FF9) or e.item.get("_replace_time")
        start = e.item.get("start") or a.props.get(0x7FFB)
        end = e.item.get("end") or a.props.get(0x7FFC)
        exc.append({"original": orig.isoformat() if hasattr(orig, "isoformat") else None,
                    "start": start.isoformat() if hasattr(start, "isoformat") else None,
                    "end": end.isoformat() if hasattr(end, "isoformat") else None,
                    "subject": e.subject or None, "location": e.item.get("location"),
                    "attendees": [str(x) for x in e.to + e.cc],
                    "body": (e.body_text or html_to_text(e.body_html))[:20000]})
        msgs.append(e)
    m.item["exceptions"] = exc
    m.extra["exception_msgs"] = msgs
    occ = m.item.get("occurrences") or []
    rec = m.item.get("_recur")
    if not rec:
        return
    import datetime as _dt
    def near(a, b):
        try:
            return abs((_dt.datetime.fromisoformat(a) - _dt.datetime.fromisoformat(b)).total_seconds()) < 120
        except (TypeError, ValueError):
            return False
    for o in occ:
        orig = o[5] if len(o) > 5 else None
        for i, x in enumerate(exc):
            if (orig and x["original"] and near(orig, x["original"])) or (x["start"] and near(o[0], x["start"])):
                while len(o) < 5:
                    o.append(None)
                o[4] = i
                if x["subject"]:
                    o[2] = x["subject"]
                if x["location"]:
                    o[3] = x["location"]
                break
