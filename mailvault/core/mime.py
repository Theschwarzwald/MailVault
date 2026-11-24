"""EML (RFC 822 / MIME) reading and writing."""
from __future__ import annotations

import email
import email.policy
import email.utils
import html as _html
import re
from email.message import EmailMessage
from email.header import decode_header, make_header

from .model import DAMAGED, Address, Attachment, Message

_POLICY = email.policy.default.clone(raise_on_defect=False)


def _hdr(v) -> str:
    if v is None:
        return ""
    try:
        return str(make_header(decode_header(str(v))))
    except Exception:  # noqa: BLE001
        return str(v)


def _addrs(v) -> list[Address]:
    if not v:
        return []
    out = []
    for name, addr in email.utils.getaddresses([_hdr(v)]):
        if name or addr:
            out.append(Address(name, addr))
    return out


def _payload(part) -> bytes:
    try:
        return part.get_payload(decode=True) or b""
    except Exception:  # noqa: BLE001
        p = part.get_payload()
        return p.encode("utf-8", "replace") if isinstance(p, str) else b""


def _text(part) -> str:
    data = _payload(part)
    cs = part.get_content_charset() or "utf-8"
    for enc in (cs, "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1", "replace")


def parse_eml(raw: bytes, source_format: str = "eml") -> Message:
    m = Message(source_format=source_format, raw=raw)
    try:
        em = email.message_from_bytes(raw, policy=_POLICY)
    except Exception as e:  # noqa: BLE001
        m.status = DAMAGED
        m.problems.append(f"MIME parse failed: {e}")
        m.body_text = raw.decode("utf-8", "replace")
        return m

    head_end = raw.find(b"\r\n\r\n")
    if head_end < 0:
        head_end = raw.find(b"\n\n")
    m.headers = raw[: head_end if head_end >= 0 else len(raw)].decode("utf-8", "replace")
    m.subject = _hdr(em.get("Subject"))
    fr = _addrs(em.get("From"))
    m.sender = fr[0] if fr else Address()
    m.to, m.cc, m.bcc = _addrs(em.get("To")), _addrs(em.get("Cc")), _addrs(em.get("Bcc"))
    m.reply_to = _addrs(em.get("Reply-To"))
    m.message_id = _hdr(em.get("Message-ID")).strip()
    m.in_reply_to = _hdr(em.get("In-Reply-To")).strip()
    try:
        if em.get("Date"):
            m.date = email.utils.parsedate_to_datetime(str(em.get("Date")))
    except Exception:  # noqa: BLE001
        m.problems.append("Unparseable Date header")
    from .state import from_headers
    m.states, m.categories = from_headers(m.headers)
    m.extra["received_hdr"] = bool(re.search(r"(?im)^(received|delivered-to):", m.headers))
    imp = (_hdr(em.get("Importance")) or _hdr(em.get("X-Priority"))).lower()
    if imp.startswith(("high", "1", "2")):
        m.importance = "high"
    elif imp.startswith(("low", "4", "5")):
        m.importance = "low"

    structural = ("MissingHeaderBodySeparator", "StartBoundaryNotFound", "CloseBoundaryNotFound",
                  "InvalidBase64", "MultipartInvariantViolation")
    for part in em.walk():
        for d in getattr(part, "defects", []) or []:
            nm = type(d).__name__
            m.problems.append(f"MIME defect: {nm}")
            if nm.startswith(structural):
                m.status = DAMAGED

    for part in em.walk():
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
            continue
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        fname = part.get_filename()
        fname = _hdr(fname) if fname else None
        cid = (part.get("Content-ID") or "").strip("<> ")
        if ctype == "message/rfc822":
            inner = part.get_payload()
            inner_bytes = inner[0].as_bytes() if isinstance(inner, list) and inner else _payload(part)
            sub = parse_eml(inner_bytes)
            a = Attachment(filename=fname or (sub.subject or "message") + ".eml",
                           mime_type="message/rfc822", embedded=sub, _data=inner_bytes)
            a.size = len(inner_bytes)
            m.attachments.append(a)
            continue
        if part.get_content_maintype() == "multipart":
            continue
        is_att = "attachment" in disp or (fname and ctype not in ("text/plain", "text/html"))
        if not is_att and ctype == "text/plain" and not m.body_text:
            m.body_text = _text(part)
        elif not is_att and ctype == "text/html" and not m.body_html:
            m.body_html = _text(part)
        else:
            data = _payload(part)
            ext = {"text/calendar": "ics", "application/ics": "ics"}.get(ctype) or ctype.split("/")[-1].split("+")[0]
            m.attachments.append(Attachment(
                filename=fname or ("invite.ics" if ext == "ics" and not cid else f"{cid or 'part'}.{ext}"), mime_type=ctype,
                size=len(data), content_id=cid,
                inline=bool(cid) and "attachment" not in disp, _data=data))
    if problems_body(m):
        m.status = DAMAGED
    from .invite import apply_invite
    apply_invite(m)
    return m


def problems_body(m: Message) -> bool:
    return any("parse failed" in p for p in m.problems)


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|head).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", "", h)
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(h)).strip()


def build_eml(m: Message) -> bytes:
    """Serialise any Message to RFC 822. Re-uses raw bytes when lossless."""
    if m.raw and m.source_format in ("eml", "mbox"):
        return m.raw
    em = EmailMessage(policy=email.policy.SMTP)
    def put(k, v):
        if v:
            try:
                em[k] = v
            except Exception:  # noqa: BLE001 - malformed legacy values
                em[k] = v.encode("ascii", "replace").decode()
    put("Subject", m.subject)
    put("From", str(m.sender))
    put("To", ", ".join(map(str, m.to)))
    put("Cc", ", ".join(map(str, m.cc)))
    put("Bcc", ", ".join(map(str, m.bcc)))
    if m.date:
        put("Date", email.utils.format_datetime(m.date))
    put("Message-ID", m.message_id)
    put("In-Reply-To", m.in_reply_to)
    if m.importance != "normal":
        put("Importance", m.importance.capitalize())
    put("X-MailVault-Source", m.source_format)
    if m.status != "ok":
        put("X-MailVault-Status", m.status)
    text = m.body_text or (html_to_text(m.body_html) if m.body_html else "")
    em.set_content(text or "")
    if m.body_html:
        em.add_alternative(m.body_html, subtype="html")
    for a in m.attachments:
        if a.embedded is not None and a.mime_type in ("message/rfc822", "application/vnd.ms-outlook"):
            inner = build_eml(a.embedded)
            em.add_attachment(inner, maintype="message", subtype="rfc822",
                              filename=(a.filename.rsplit(".", 1)[0] + ".eml"))
            continue
        maintype, _, sub = (a.mime_type or "application/octet-stream").partition("/")
        if maintype == "message":
            maintype, sub = "application", "octet-stream"
        kw = {"maintype": maintype, "subtype": sub or "octet-stream", "filename": a.filename}
        if a.content_id:
            kw["cid"] = f"<{a.content_id}>"
        if a.inline:
            kw["disposition"] = "inline"
        em.add_attachment(a.data, **kw)
    return em.as_bytes()
