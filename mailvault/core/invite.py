"""Calendar invitations carried inside mail (text/calendar parts and .ics attachments).

Reads the first VEVENT of an iCalendar object and turns it into the same item
fields that Outlook appointments use (start, end, all_day, location), plus an
``invite`` dict with the method, organizer, attendees and status, so the mail
can be shown on a calendar and the reading pane can show the meeting time.
"""
from __future__ import annotations

import datetime as dt
import re

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

UTC = dt.timezone.utc

# Outlook writes Windows time-zone names in TZID; the common ones mapped to IANA.
WINDOWS_TZ = {
    "pacific standard time": "America/Los_Angeles", "mountain standard time": "America/Denver",
    "us mountain standard time": "America/Phoenix", "central standard time": "America/Chicago",
    "eastern standard time": "America/New_York", "us eastern standard time": "America/Indianapolis",
    "atlantic standard time": "America/Halifax", "alaskan standard time": "America/Anchorage",
    "hawaiian standard time": "Pacific/Honolulu", "newfoundland standard time": "America/St_Johns",
    "canada central standard time": "America/Regina", "mexico standard time": "America/Mexico_City",
    "central standard time (mexico)": "America/Mexico_City", "sa pacific standard time": "America/Bogota",
    "e. south america standard time": "America/Sao_Paulo", "argentina standard time": "America/Buenos_Aires",
    "gmt standard time": "Europe/London", "greenwich standard time": "Atlantic/Reykjavik",
    "utc": "UTC", "coordinated universal time": "UTC",
    "w. europe standard time": "Europe/Berlin", "romance standard time": "Europe/Paris",
    "central europe standard time": "Europe/Budapest", "central european standard time": "Europe/Warsaw",
    "e. europe standard time": "Europe/Chisinau", "gtb standard time": "Europe/Bucharest",
    "fle standard time": "Europe/Kiev", "russian standard time": "Europe/Moscow",
    "turkey standard time": "Europe/Istanbul", "israel standard time": "Asia/Jerusalem",
    "egypt standard time": "Africa/Cairo", "south africa standard time": "Africa/Johannesburg",
    "arabian standard time": "Asia/Dubai", "arab standard time": "Asia/Riyadh", "iran standard time": "Asia/Tehran",
    "pakistan standard time": "Asia/Karachi", "india standard time": "Asia/Kolkata",
    "bangladesh standard time": "Asia/Dhaka", "se asia standard time": "Asia/Bangkok",
    "china standard time": "Asia/Shanghai", "singapore standard time": "Asia/Singapore",
    "taipei standard time": "Asia/Taipei", "tokyo standard time": "Asia/Tokyo", "korea standard time": "Asia/Seoul",
    "aus eastern standard time": "Australia/Sydney", "e. australia standard time": "Australia/Brisbane",
    "cen. australia standard time": "Australia/Adelaide", "w. australia standard time": "Australia/Perth",
    "new zealand standard time": "Pacific/Auckland",
}

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in re.split(r"\r\n|\n|\r", text):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _split(line: str) -> tuple[str, dict, str]:
    """NAME;P1=a;P2="b:c":value -> (NAME, {P1: a, P2: b:c}, value)."""
    i, q = 0, False
    while i < len(line):
        c = line[i]
        if c == '"':
            q = not q
        elif c == ":" and not q:
            break
        i += 1
    head, value = line[:i], line[i + 1:]
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', head)
    name = parts[0].upper() if parts else ""
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return name, params, value


def _text(v: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v).strip()


def _parse_blocks(lines: list[str]) -> list[dict]:
    """Nested BEGIN/END components as dicts: {'type', 'props': [(name, params, value)], 'subs': [...]}."""
    root: dict = {"type": "ROOT", "props": [], "subs": []}
    stack = [root]
    for line in lines:
        name, params, value = _split(line)
        if name == "BEGIN":
            comp = {"type": value.strip().upper(), "props": [], "subs": []}
            stack[-1]["subs"].append(comp)
            stack.append(comp)
        elif name == "END":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1]["props"].append((name, params, value))
    return root["subs"]


def _first(comp: dict, name: str):
    for n, p, v in comp["props"]:
        if n == name:
            return p, v
    return None, None


def _all(comp: dict, name: str):
    return [(p, v) for n, p, v in comp["props"] if n == name]


def _naive(v: str) -> tuple[dt.datetime | None, bool]:
    v = v.strip()
    try:
        if re.fullmatch(r"\d{8}", v):
            return dt.datetime.strptime(v, "%Y%m%d"), True
        m = re.fullmatch(r"(\d{8}T\d{4,6})(Z?)", v)
        if m:
            s = m.group(1)
            d = dt.datetime.strptime(s, "%Y%m%dT%H%M%S" if len(s) == 15 else "%Y%m%dT%H%M")
            return (d.replace(tzinfo=UTC) if m.group(2) else d), False
    except ValueError:
        pass
    return None, False


def _offset(v: str) -> dt.timedelta:
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})(\d{2})?", (v or "").strip())
    if not m:
        return dt.timedelta(0)
    t = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)), seconds=int(m.group(4) or 0))
    return -t if m.group(1) == "-" else t


def _nth_weekday(year: int, month: int, byday: str) -> dt.date | None:
    m = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", byday.strip().upper())
    if not m:
        return None
    n, wd = int(m.group(1) or 1), _WEEKDAYS[m.group(2)]
    if n > 0:
        d = dt.date(year, month, 1)
        d += dt.timedelta(days=(wd - d.weekday()) % 7 + 7 * (n - 1))
        return d if d.month == month else None
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - dt.timedelta(days=1)
    d -= dt.timedelta(days=(d.weekday() - wd) % 7 + 7 * (-n - 1))
    return d


class _VTimezone(dt.tzinfo):
    """A VTIMEZONE's STANDARD/DAYLIGHT rules (yearly BYMONTH/BYDAY, as Outlook writes them)."""

    def __init__(self, comp: dict):
        self.rules = []
        for sub in comp["subs"]:
            if sub["type"] not in ("STANDARD", "DAYLIGHT"):
                continue
            _, start = _first(sub, "DTSTART")
            _, to = _first(sub, "TZOFFSETTO")
            _, frm = _first(sub, "TZOFFSETFROM")
            _, rrule = _first(sub, "RRULE")
            st, _ = _naive(start or "16010101T000000")
            r = dict(x.split("=", 1) for x in (rrule or "").split(";") if "=" in x)
            self.rules.append({"start": st or dt.datetime(1601, 1, 1), "to": _offset(to), "from": _offset(frm or to),
                               "month": int(r["BYMONTH"]) if r.get("BYMONTH", "").isdigit() else None,
                               "byday": r.get("BYDAY")})
        self.fixed = self.rules[0]["to"] if self.rules else dt.timedelta(0)

    def _transitions(self, year: int):
        out = []
        for r in self.rules:
            if r["month"] and r["byday"]:
                day = _nth_weekday(year, r["month"], r["byday"])
                if day:
                    out.append((dt.datetime.combine(day, r["start"].time()), r))
            elif r["start"].year <= year:
                out.append((r["start"].replace(year=year) if r["start"].year < year and r["month"] else r["start"], r))
        return sorted(out, key=lambda x: x[0])

    def utcoffset(self, d):
        if d is None or len(self.rules) < 2:
            return self.fixed
        naive = d.replace(tzinfo=None)
        cur = None
        for when, r in self._transitions(naive.year - 1) + self._transitions(naive.year):
            if naive >= when:
                cur = r
        return cur["to"] if cur else self.rules[-1]["to"]

    def dst(self, d):
        return dt.timedelta(0)

    def tzname(self, d):
        return None


def _zone(tzid: str | None, vtimezones: dict) -> dt.tzinfo | None:
    if not tzid:
        return None
    key = tzid.strip().strip("/")
    if ZoneInfo is not None:
        for name in (key, WINDOWS_TZ.get(key.lower())):
            if name:
                try:
                    return ZoneInfo(name)
                except Exception:  # noqa: BLE001 - unknown zone name or no tz database
                    pass
    return vtimezones.get(tzid)


def _when(params: dict, value: str, vtimezones: dict) -> tuple[dt.datetime | None, bool]:
    d, all_day = _naive(value)
    if d is None:
        return None, False
    if all_day or params.get("VALUE", "").upper() == "DATE":
        return d.replace(tzinfo=UTC), True       # all-day items are stored as UTC midnight
    if d.tzinfo is None:
        z = _zone(params.get("TZID"), vtimezones)
        d = d.replace(tzinfo=z) if z else d.replace(tzinfo=UTC)
    return d.astimezone(UTC), False


def _person(params: dict, value: str) -> str:
    addr = re.sub(r"(?i)^mailto:", "", value.strip())
    cn = params.get("CN", "").strip()
    return f"{cn} <{addr}>" if cn and addr and cn != addr else (cn or addr)


def parse_ics(text: str) -> dict | None:
    """The first event in an iCalendar object, or None when there isn't one."""
    blocks = _parse_blocks(_unfold(text))
    cal = next((b for b in blocks if b["type"] == "VCALENDAR"), None)
    if cal is None:
        return None
    vtz = {}
    for sub in cal["subs"]:
        if sub["type"] == "VTIMEZONE":
            _, tzid = _first(sub, "TZID")
            if tzid:
                vtz[tzid.strip()] = _VTimezone(sub)
    ev = next((s for s in cal["subs"] if s["type"] == "VEVENT"), None)
    if ev is None:
        return None
    _, method = _first(cal, "METHOD")
    p, v = _first(ev, "DTSTART")
    if v is None:
        return None
    start, all_day = _when(p, v, vtz)
    if start is None:
        return None
    p, v = _first(ev, "DTEND")
    end = _when(p, v, vtz)[0] if v else None
    if end is None:
        _, dur = _first(ev, "DURATION")
        m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", (dur or "").strip())
        if m and any(m.groups()):
            w, d_, h, mi, s = (int(x or 0) for x in m.groups())
            end = start + dt.timedelta(weeks=w, days=d_, hours=h, minutes=mi, seconds=s)
        else:
            end = start + (dt.timedelta(days=1) if all_day else dt.timedelta(0))
    org_p, org_v = _first(ev, "ORGANIZER")
    attendees = []
    for ap, av in _all(ev, "ATTENDEE"):
        attendees.append({"who": _person(ap, av), "role": ap.get("ROLE", "REQ-PARTICIPANT").upper(),
                          "status": ap.get("PARTSTAT", "NEEDS-ACTION").upper()})
    _, rrule = _first(ev, "RRULE")
    method = (method or "").strip().upper()
    _, status = _first(ev, "STATUS")
    return {
        "start": start, "end": end, "all_day": all_day,
        "summary": _text(_first(ev, "SUMMARY")[1] or ""),
        "location": _text(_first(ev, "LOCATION")[1] or ""),
        "description": _text(_first(ev, "DESCRIPTION")[1] or "")[:2000],
        "organizer": _person(org_p, org_v) if org_v else "",
        "attendees": attendees,
        "method": method,
        "status": (status or "").strip().upper(),
        "rrule": (rrule or "").strip(),
        "uid": (_first(ev, "UID")[1] or "").strip(),
    }


METHOD_LABEL = {"REQUEST": "Meeting invitation", "CANCEL": "Meeting cancelled", "REPLY": "Meeting response",
                "PUBLISH": "Calendar event", "COUNTER": "New time proposed", "ADD": "Meeting update",
                "REFRESH": "Meeting update request", "DECLINECOUNTER": "Proposal declined"}


def apply_invite(m) -> bool:
    """Attach invite details to a mail that carries a calendar object. Returns True if found."""
    if m.kind != "mail" or m.item.get("invite"):
        return bool(m.item.get("invite"))
    for a in m.attachments:
        mt = (a.mime_type or "").lower()
        if not (mt in ("text/calendar", "application/ics", "text/x-vcalendar")
                or (a.filename or "").lower().endswith((".ics", ".vcs"))):
            continue
        try:
            raw = a.data or b""
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except UnicodeDecodeError:
            text = raw.decode("latin-1", "replace")
        try:
            ev = parse_ics(text)
        except Exception:  # noqa: BLE001 - a broken calendar part must not break the mail
            ev = None
        if not ev:
            continue
        m.item.update(start=ev["start"], end=ev["end"], all_day=ev["all_day"])
        if ev["location"]:
            m.item.setdefault("location", ev["location"])
        if ev["rrule"]:
            m.item.setdefault("recurring", True)
        att = [x["who"] for x in ev["attendees"]]
        m.item["invite"] = {
            "label": "Meeting cancelled" if ev["status"] == "CANCELLED" else METHOD_LABEL.get(ev["method"], "Calendar event"),
            "method": ev["method"], "status": ev["status"], "summary": ev["summary"], "organizer": ev["organizer"],
            "attendees": att[:50], "more_attendees": max(0, len(att) - 50),
            "responses": {x["who"]: x["status"] for x in ev["attendees"][:50] if x["status"] != "NEEDS-ACTION"},
            "rrule": ev["rrule"], "attachment": m.attachments.index(a),
        }
        return True
    return False
