"""Calendar (ICS), contact (VCF) and task export, plus HTML views for non-mail items."""
from __future__ import annotations

import datetime as _dt
import html as _html
import uuid

from .mime import html_to_text
from .model import Message


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    out, b = [], line.encode("utf-8")
    while len(b) > 75:
        cut = 75
        while cut > 0 and (b[cut] & 0xC0) == 0x80:  # don't split UTF-8 sequences
            cut -= 1
        out.append(b[:cut].decode())
        b = b" " + b[cut:]
    out.append(b.decode())
    return "\r\n".join(out)


def _utc(d) -> str:
    if not isinstance(d, _dt.datetime):
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _uid(m: Message) -> str:
    base = m.message_id or f"{m.subject}|{m.item.get('start')}|{m.sender}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, base)) + "@mailvault"


def _body(m: Message) -> str:
    return m.body_text or html_to_text(m.body_html)


def to_ics(messages: list[Message]) -> bytes:
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MailVault//EN", "CALSCALE:GREGORIAN"]
    now = _utc(_dt.datetime.now(_dt.timezone.utc))
    for m in messages:
        it = m.item
        if m.kind == "task":
            L += ["BEGIN:VTODO", f"UID:{_uid(m)}", f"DTSTAMP:{now}", f"SUMMARY:{_esc(m.subject)}"]
            if it.get("due"):
                L.append(f"DUE:{_utc(it['due'])}")
            if it.get("start"):
                L.append(f"DTSTART:{_utc(it['start'])}")
            L.append(f"PERCENT-COMPLETE:{it.get('percent', 0)}")
            L.append("STATUS:" + ("COMPLETED" if it.get("complete") else "IN-PROCESS" if it.get("percent") else "NEEDS-ACTION"))
            if _body(m):
                L.append(f"DESCRIPTION:{_esc(_body(m))}")
            L.append("END:VTODO")
            continue
        start, end = it.get("start") or m.date, it.get("end")
        rec = it.get("_recur")
        if rec:
            L += _recurring_event(m, rec, now)
            continue
        L += ["BEGIN:VEVENT", f"UID:{_uid(m)}", f"DTSTAMP:{now}", f"SUMMARY:{_esc(m.subject)}"]
        if it.get("all_day") and isinstance(start, _dt.datetime):
            L.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
            if isinstance(end, _dt.datetime):
                L.append(f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}")
        else:
            if start:
                L.append(f"DTSTART:{_utc(start)}")
            if end:
                L.append(f"DTEND:{_utc(end)}")
        if it.get("location"):
            L.append(f"LOCATION:{_esc(it['location'])}")
        if m.sender.email:
            L.append(f"ORGANIZER;CN={_esc(m.sender.name)}:mailto:{m.sender.email}")
        for a in m.to + m.cc:
            if a.email:
                L.append(f"ATTENDEE;CN={_esc(a.name)};ROLE={'REQ' if a in m.to else 'OPT'}-PARTICIPANT:mailto:{a.email}")
        if it.get("busy") in ("free", "tentative"):
            L.append("TRANSP:TRANSPARENT" if it["busy"] == "free" else "STATUS:TENTATIVE")
        if _body(m):
            L.append(f"DESCRIPTION:{_esc(_body(m))}")
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    return ("\r\n".join(_fold(x) for x in L) + "\r\n").encode("utf-8")


def _recurring_event(m: Message, rec_tz, now: str) -> list[str]:
    """VTIMEZONE + master VEVENT with RRULE/EXDATE + one VEVENT per modified occurrence."""
    rec, tz, tzname = rec_tz
    tzid = (tzname or "Outlook time zone").replace(",", " ").replace(";", " ")
    L = tz.vtimezone(tzid) if tz else []
    loc = lambda d: d.strftime("%Y%m%dT%H%M%S")  # noqa: E731
    dts = (f"DTSTART;TZID={tzid}:" if tz else "DTSTART:") + loc(rec.start_date + _dt.timedelta(minutes=rec.start_offset))
    dte = (f"DTEND;TZID={tzid}:" if tz else "DTEND:") + loc(rec.start_date + _dt.timedelta(minutes=rec.end_offset))
    uid = _uid(m)
    L += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now}", f"SUMMARY:{_esc(m.subject)}", dts, dte, rec.rrule()]
    mod_days = {e.original.date() for e in rec.exceptions}
    for d in rec.deleted:
        if d.date() not in mod_days:
            when = d + _dt.timedelta(minutes=rec.start_offset)
            L.append((f"EXDATE;TZID={tzid}:" if tz else "EXDATE:") + loc(when))
    if m.item.get("location"):
        L.append(f"LOCATION:{_esc(m.item['location'])}")
    if m.sender.email:
        L.append(f"ORGANIZER;CN={_esc(m.sender.name)}:mailto:{m.sender.email}")
    for a in m.to + m.cc:
        if a.email:
            L.append(f"ATTENDEE;CN={_esc(a.name)}:mailto:{a.email}")
    if _body(m):
        L.append(f"DESCRIPTION:{_esc(_body(m))}")
    L.append("END:VEVENT")
    details = m.item.get("exceptions") or []
    for e in rec.exceptions:
        orig = e.original.replace(hour=0, minute=0) + _dt.timedelta(minutes=rec.start_offset)
        orig_utc = tz.to_utc(orig) if tz else orig.replace(tzinfo=_dt.timezone.utc)
        det = next((x for x in details if x.get("original") and
                    abs((_dt.datetime.fromisoformat(x["original"]) - orig_utc).total_seconds()) < 120), None)
        L += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now}",
              (f"RECURRENCE-ID;TZID={tzid}:" if tz else "RECURRENCE-ID:") + loc(orig),
              f"SUMMARY:{_esc((det or {}).get('subject') or e.subject or m.subject)}",
              (f"DTSTART;TZID={tzid}:" if tz else "DTSTART:") + loc(e.start),
              (f"DTEND;TZID={tzid}:" if tz else "DTEND:") + loc(e.end)]
        loc = (det or {}).get("location") or e.location or m.item.get("location")
        if loc:
            L.append(f"LOCATION:{_esc(loc)}")
        if det:
            if det.get("body"):
                L.append(f"DESCRIPTION:{_esc(det['body'])}")
            for att in det.get("attendees") or []:
                name, _, mail = att.rpartition(" <")
                mail = mail.rstrip(">") if "@" in mail else ""
                if mail:
                    L.append(f"ATTENDEE;CN={_esc(name or mail)}:mailto:{mail}")
        L.append("END:VEVENT")
    return L


def to_vcf(messages: list[Message]) -> bytes:
    out = []
    for m in messages:
        it = m.item
        fn = it.get("display_name") or m.subject or it.get("email") or "Contact"
        L = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{_esc(fn)}",
             f"N:{_esc(it.get('surname', ''))};{_esc(it.get('given', ''))};{_esc(it.get('middle', ''))};;"]
        if it.get("company") or it.get("department"):
            L.append(f"ORG:{_esc(it.get('company', ''))}" + (f";{_esc(it['department'])}" if it.get("department") else ""))
        if it.get("job_title"):
            L.append(f"TITLE:{_esc(it['job_title'])}")
        for k, t in (("email", "INTERNET,PREF"), ("email2", "INTERNET"), ("email3", "INTERNET")):
            if it.get(k):
                L.append(f"EMAIL;TYPE={t}:{it[k]}")
        for k, t in (("mobile", "CELL"), ("business_phone", "WORK,VOICE"), ("home_phone", "HOME,VOICE"),
                     ("business_fax", "WORK,FAX")):
            if it.get(k):
                L.append(f"TEL;TYPE={t}:{it[k]}")
        for k, t in (("work_address", "WORK"), ("home_address", "HOME")):
            if it.get(k):
                L.append(f"ADR;TYPE={t}:;;{_esc(it[k])};;;;")
        if isinstance(it.get("birthday"), _dt.datetime):
            L.append(f"BDAY:{it['birthday'].strftime('%Y-%m-%d')}")
        if it.get("web"):
            L.append(f"URL:{it['web']}")
        if _body(m):
            L.append(f"NOTE:{_esc(_body(m))}")
        L.append("END:VCARD")
        out.append("\r\n".join(_fold(x) for x in L))
    return ("\r\n".join(out) + "\r\n").encode("utf-8")


def _fmt(d, all_day=False) -> str:
    if not isinstance(d, _dt.datetime):
        return ""
    return d.strftime("%A, %B %d, %Y") if all_day else d.strftime("%A, %B %d, %Y %I:%M %p")


def item_rows(m: Message) -> list[tuple[str, str]]:
    """Label/value pairs describing a calendar, contact, task or note item."""
    it = m.item
    if m.kind == "appointment":
        rows = [("Start", _fmt(it.get("start"), it.get("all_day"))), ("End", _fmt(it.get("end"), it.get("all_day"))),
                ("Location", it.get("location", "")), ("Organizer", str(m.sender)),
                ("Required", "; ".join(map(str, m.to))), ("Optional", "; ".join(map(str, m.cc))),
                ("Show as", it.get("busy", "")),
                ("Recurrence", it.get("recurrence") or ("Yes" if it.get("recurring") else ""))]
    elif m.kind == "contact":
        rows = [("Full name", it.get("display_name", "")), ("Company", it.get("company", "")),
                ("Job title", it.get("job_title", "")), ("Department", it.get("department", "")),
                ("Email", it.get("email", "")), ("Email 2", it.get("email2", "")), ("Email 3", it.get("email3", "")),
                ("Mobile", it.get("mobile", "")), ("Business", it.get("business_phone", "")),
                ("Home", it.get("home_phone", "")), ("Business fax", it.get("business_fax", "")),
                ("Work address", it.get("work_address", "")), ("Home address", it.get("home_address", "")),
                ("Birthday", it["birthday"].strftime("%B %d, %Y") if isinstance(it.get("birthday"), _dt.datetime) else ""),
                ("Web page", it.get("web", ""))]
    elif m.kind == "task":
        rows = [("Status", it.get("task_status", "")), ("% complete", f"{it.get('percent', 0)}%"),
                ("Start", _fmt(it.get("start"), True)), ("Due", _fmt(it.get("due"), True))]
    else:
        rows = []
    return [(k, v) for k, v in rows if v]


def item_html(m: Message) -> str:
    rows = "".join(f"<tr><th>{_html.escape(k)}:</th><td>{_html.escape(v)}</td></tr>" for k, v in item_rows(m))
    return f"<table class='mv-head'>{rows}</table>" if rows else ""
