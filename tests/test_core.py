"""Core regression tests. Run: python tests/make_fixtures.py && python tests/test_core.py"""
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mailvault.core.export import FORMATS, export_many, iter_folder  # noqa: E402
from mailvault.core.library import open_path  # noqa: E402
from mailvault.core.mime import parse_eml  # noqa: E402
from mailvault.core.msg import parse_msg  # noqa: E402
from mailvault.core.rtf import decompress_rtf, rtf_to_text  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "fixtures")


def items(path):
    return list(iter_folder(open_path(os.path.join(FX, path))))


def test_pst():
    got = {m.subject: (p, m) for p, m in items("archive2019.pst")}
    p, m = got["Q3 invoice attached"]
    assert p.endswith("Inbox") and m.status == "ok"
    assert m.attachments[0].filename == "invoice_q3.pdf" and len(m.attachments[0].data) == 20480
    assert m.attachments[0].data == bytes(range(256)) * 80
    assert [a.email for a in m.to] == ["you@example.com"] and m.cc[0].email == "ap@example.com"
    assert "Click the portal link" in got["Password reset"][1].body_text
    assert got["Not in folder index"][1].status == "orphaned"
    assert got["Lost parent folder"][1].status == "orphaned"
    assert got["Deleted: budget draft"][1].status == "recovered"
    assert got["Kickoff notes"][1].status == "damaged"
    emb = got["Fwd: proposal"][1].attachments[0].embedded
    assert emb is not None and emb.subject == "Original proposal"
    assert any(s.startswith("(unreadable item") for s in got)


def test_items_and_states():
    got = {m.subject: (p, m) for p, m in items("archive2019.pst")}
    a = got["Team offsite"][1]
    assert a.kind == "appointment" and a.item["all_day"] and a.item["location"] == "Lake House"
    assert a.item["busy"] == "out of office"
    c = got["Dana Whitfield"][1]
    assert c.kind == "contact" and c.item["email"] == "dana@example.com" and c.item["company"] == "Acme Corp"
    t = got["Send Q3 numbers to Dana"][1]
    assert t.kind == "task" and t.item["percent"] == 50 and t.item["task_status"] == "In progress"
    assert got["Renew domain"][1].item["complete"]
    assert got["Wi-Fi password"][1].kind == "note" and got["Wi-Fi password"][1].item["color"] == "yellow"
    s = got["Re: shipping dates"][1]
    assert "Sent" in s.states and s.categories == ["Customers", "Follow up"]
    assert "Forwarded" in got["2018 tax documents"][1].states and "Archived" in got["2018 tax documents"][1].states
    assert "Draft" in got["Unfinished draft"][1].states
    assert "Replied" not in got["Password reset"][1].states and "Unread" in got["Password reset"][1].states
    from mailvault.core.items import to_ics, to_vcf
    ics = to_ics([a]).decode()
    assert "DTSTART;VALUE=DATE:20191121" in ics and "LOCATION:Lake House" in ics
    assert "EMAIL;TYPE=INTERNET,PREF:dana@example.com" in to_vcf([c]).decode()
    gm = {m.subject: m for _, m in items("takeout.mbox")}
    assert "Sent" in gm["Hello"].states and "Deleted" in gm["Old"].states
    assert "Unread" in gm["Sale"].states and "Important" in gm["Sale"].states
    assert "Archived" in gm["Your receipt"].states and gm["Your receipt"].categories == ["Receipts, 2019"]


def test_recurrence():
    got = {m.subject: m for _, m in items("archive2019.pst")}
    r = got["Team standup"]
    assert r.item["recurring"] and r.item["recurrence"].startswith("Every week on Monday, Wednesday, 8 times"), r.item["recurrence"]
    occ = r.item["occurrences"]
    starts = [o[0] for o in occ]
    assert len(occ) == 7, starts                       # 8 minus one deleted
    assert starts[0] == "2019-11-04T14:30:00+00:00"    # 9:30 EST
    assert "2019-11-13T14:30:00+00:00" not in starts   # deleted
    moved = [o for o in occ if o[2]][0]
    assert moved[2] == "Standup (moved)" and moved[0] == "2019-11-18T16:00:00+00:00"
    from mailvault.core.items import to_ics
    ics = to_ics([r]).decode()
    assert "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,WE;WKST=SU;COUNT=8" in ics
    assert "EXDATE;TZID=(UTC-05:00) Eastern Time (US & Canada):20191113T093000" in ics
    assert "RECURRENCE-ID;TZID=(UTC-05:00) Eastern Time (US & Canada):20191118T093000" in ics
    assert "BEGIN:VTIMEZONE" in ics and "BYMONTH=3;BYDAY=2SU" in ics
    # changed occurrence: its own body, attendees and location
    ex = r.item["exceptions"][0]
    assert ex["body"] == "Moved for the customer demo. Bring laptops." and ex["location"] == "Room 4B"
    assert ex["attendees"] == ["Dana Whitfield <dana@example.com>", "Marcus Lee <marcus@example.com>"]
    assert moved[4] == 0 and moved[3] == "Room 4B"
    assert not r.has_attachments                         # the exception attachment is hidden
    assert "DESCRIPTION:Moved for the customer demo. Bring laptops." in ics
    assert "ATTENDEE;CN=Marcus Lee:mailto:marcus@example.com" in ics


def test_non_gregorian_recurrence():
    import datetime as dt
    from mailvault.core.recur import Recurrence, build_blob
    D = dt.datetime

    def dates(**kw):
        start = kw.pop("start")
        r = Recurrence.parse(build_blob(
            kw.pop("freq"), kw.pop("pattern"), kw.pop("period"), kw.pop("mask", 0), start, start + dt.timedelta(hours=1),
            count=kw.pop("count"), dom=kw.pop("dom", 0), nth=kw.pop("nth", 0), cal_type=kw.pop("cal")))
        return r, [o["start"].date().isoformat() for o in r.occurrences(None)]
    # Hijri: 1st of every month from 1 Ramadan 1441
    r, d = dates(freq=0x200C, pattern=0xA, period=1, dom=1, start=D(2020, 4, 24), count=3, cal=0x6)
    assert d == ["2020-04-24", "2020-05-24", "2020-06-22"], d
    assert "Hijri calendar" in r.describe() and r.rrule().startswith("RRULE:RSCALE=ISLAMIC-CIVIL;FREQ=MONTHLY")
    # Hebrew: Purim (14 Adar) yearly; leap year 5784 uses Adar II
    r, d = dates(freq=0x200D, pattern=0x2, period=12, dom=14, start=D(2023, 3, 7), count=3, cal=0x8)
    assert d == ["2023-03-07", "2024-03-24", "2025-03-14"], d
    assert "RSCALE=HEBREW" in r.rrule() and "BYMONTH=6" in r.rrule()
    # Chinese lunar: Mid-Autumn (8/15) yearly
    r, d = dates(freq=0x200D, pattern=0x2, period=12, dom=15, start=D(2020, 10, 1), count=3, cal=0xF)
    assert d == ["2020-10-01", "2021-09-21", "2022-09-10"], d
    # Chinese lunar monthly: the leap 4th month of 2020 counts as a month
    r, d = dates(freq=0x200C, pattern=0x2, period=1, dom=1, start=D(2020, 4, 23), count=3, cal=0xF)
    assert d == ["2020-04-23", "2020-05-23", "2020-06-21"], d
    # Korean lunar, last day of every month
    r, d = dates(freq=0x200C, pattern=0x4, period=1, start=D(2021, 2, 12), count=2, cal=0x14)
    assert len(d) == 2 and d[0] > "2021-02-12"
    # Japanese era calendar recurs exactly like Gregorian
    r, d = dates(freq=0x200C, pattern=0x2, period=1, dom=31, start=D(2021, 1, 31), count=3, cal=0x3)
    assert d == ["2021-01-31", "2021-02-28", "2021-03-31"], d


def test_exceptions_survive_export():
    import tempfile
    from mailvault.core.msg import build_msg, parse_msg
    from mailvault.core.pstwrite import write_pst
    r = {m.subject: m for _, m in items("archive2019.pst")}["Team standup"]
    for kind in ("msg", "pst", "pst-ansi"):
        if kind == "msg":
            b = parse_msg(build_msg(r))
        else:
            o = os.path.join(tempfile.mkdtemp(), "e.pst")
            write_pst(o, [("x/Calendar", r)], ansi=kind == "pst-ansi")
            b = items_abs(o)[0][1]
        ex = b.item.get("exceptions") or []
        assert len(ex) == 1 and ex[0]["body"] == "Moved for the customer demo. Bring laptops.", (kind, ex)
        assert ex[0]["attendees"][1] == "Marcus Lee <marcus@example.com>", kind
        assert [o[4] for o in b.item["occurrences"] if o[4] is not None] == [0], kind


def test_deep_scan():
    from mailvault.core.pst import PST
    data = open(os.path.join(FX, "archive2019.pst"), "rb").read()
    pst = PST(data)
    pst.build_tree()
    found = {m.subject: m for m in pst.deep_scan()}
    c = found["Signed contract"]
    assert c.status == "recovered"
    assert [a.filename for a in c.attachments] == ["contract.pdf"] and len(c.attachments[0].data) == 12000
    assert [a.email for a in c.to] == ["legal@example.com"]
    assert c.body_text == ("Signed contract attached. " * 400)[:3000], c.problems
    assert "Unreachable data" in found


def test_pst_writer_roundtrip():
    import tempfile
    from mailvault.core.pstwrite import write_pst
    sys.path.insert(0, os.path.dirname(__file__))
    from validate_pst import validate
    src = items("archive2019.pst")
    out = os.path.join(tempfile.mkdtemp(), "rt.pst")
    assert write_pst(out, src, "Round trip") == len(src)
    assert validate(out) == []
    back = {m.subject: (p, m) for p, m in items_abs(out)}
    for p, m in src:
        if m.subject.startswith("(unreadable"):
            continue
        b = back[m.subject][1]
        assert b.kind == m.kind and str(b.sender) == str(m.sender), m.subject
        assert [a.filename for a in b.attachments] == [a.filename for a in m.attachments], m.subject
        assert b.categories == m.categories
    assert back["Team standup"][1].item["recurrence"] == "Every week on Monday, Wednesday, 8 times"
    assert len(back["Team standup"][1].item["occurrences"]) == 7
    assert back["Team standup"][0].endswith("Calendar")
    assert back["Dana Whitfield"][1].item["email"] == "dana@example.com"
    assert back["Q3 invoice attached"][1].attachments[0].data == bytes(range(256)) * 80
    emb = back["Fwd: proposal"][1].attachments[0].embedded
    assert emb is not None and emb.subject == "Original proposal"
    for name in ("photos.msg", "takeout.mbox", "backup.zip"):
        for ansi in (False, True):
            o = os.path.join(tempfile.mkdtemp(), "x.pst")
            write_pst(o, items(name), ansi=ansi)
            assert validate(o) == [], (name, ansi)
    a = os.path.join(tempfile.mkdtemp(), "ansi.pst")
    write_pst(a, src, "ANSI", ansi=True)
    assert validate(a) == []
    ab = {m.subject: m for _, m in items_abs(a)}
    assert ab["Team standup"].item["recurrence"] == back["Team standup"][1].item["recurrence"]
    assert ab["Q3 invoice attached"].attachments[0].data == bytes(range(256)) * 80


def items_abs(path):
    return list(iter_folder(open_path(path)))


def test_deep_scan_multiblock():
    """A writer-made PST where one message lost every index entry: deep scan rebuilds it."""
    import datetime as dt
    import tempfile
    from mailvault.core.model import Address, Attachment, Message
    from mailvault.core.pst import PST
    from mailvault.core.pstwrite import PSTWriter

    class Dropper(PSTWriter):
        def add(self, path, m):
            if m.subject != "Lost one":
                return super().add(path, m)
            before = len(self.ndb.bbt)
            self._message_node(m)
            del self.ndb.bbt[before:]           # its blocks stay in the file, unindexed
    out = os.path.join(tempfile.mkdtemp(), "d.pst")
    w = Dropper(out, "Damaged")
    w.add(["Inbox"], Message(subject="Kept", sender=Address("K", "k@x.com"), body_text="ok", source_format="eml"))
    lost = Message(subject="Lost one", sender=Address("Lee", "lee@x.com"), to=[Address("You", "you@x.com")],
                   body_text="b" * 3500, body_html="<p>" + "h" * 3400 + "</p>", headers="X: " + "y" * 3400,
                   date=dt.datetime(2021, 5, 1, tzinfo=dt.timezone.utc), source_format="eml")
    lost.attachments.append(Attachment(filename="plan.pdf", _data=b"%PDF" + b"q" * 30000, size=30004))
    w.add(["Inbox"], lost)
    w.close()
    pst = PST(open(out, "rb").read())
    pst.build_tree()
    found = {m.subject: m for m in pst.deep_scan()}
    m = found["Lost one"]
    assert m.body_text == "b" * 3500 and m.body_html.endswith("h" * 3400 + "</p>")
    assert m.attachments[0].filename == "plan.pdf" and len(m.attachments[0].data) == 30004
    assert [a.email for a in m.to] == ["you@x.com"]


def test_quick_rows():
    from mailvault.core.library import open_path as op
    root = op(os.path.join(FX, "archive2019.pst"))
    inbox = next(f for f in root.walk() if f.name == "Inbox")
    q = [x() for x in inbox.quick if x]
    subj = {r["subject"]: r for r in q}
    assert subj["Q3 invoice attached"]["attachments"] and "Replied" in subj["Q3 invoice attached"]["states"]
    assert subj["Password reset"]["unread"]


def test_simple_formats():
    assert [m.subject for _, m in items("gmail.mbox")] == ["Team lunch Friday", "Photos from the trip", "Team lunch Friday"]
    assert items("lunch.eml")[0][1].body_text.startswith("Pizza at noon. From the office")
    m = items("photos.msg")[0][1]
    assert m.attachments[0].filename == "beach.jpg" and m.cc[0].email == "dana@example.com"
    t = items("truncated.msg")[0][1]
    assert t.status == "damaged" and "Photos from the trip" in t.body_text
    b = [m for _, m in items("broken.mbox")]
    assert len(b) == 2 and all(m.status == "damaged" for m in b)


def test_archive():
    subjects = sorted(m.subject for _, m in items("backup.zip"))
    assert subjects.count("Team lunch Friday") == 4 and "Photos from the trip" in subjects


def test_exports_roundtrip():
    src = items("archive2019.pst")
    for fmt in FORMATS:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            n = export_many(src, fmt, z)
        assert n == len(src) or fmt in ("ics", "vcf"), fmt
        z = zipfile.ZipFile(buf)
        assert z.namelist(), fmt
        if fmt == "msg":
            ms = [parse_msg(z.read(x)) for x in z.namelist()]
            assert any(a.embedded for m in ms for a in m.attachments)
        if fmt == "eml":
            ms = [parse_eml(z.read(x)) for x in z.namelist()]
            inv = [m for m in ms if m.subject == "Q3 invoice attached"][0]
            assert inv.attachments[0].data == bytes(range(256)) * 80


def test_rtf():
    raw = b"{\\rtf1\\ansi Hello\\par World}"
    import struct
    mela = struct.pack("<IIII", len(raw) + 12, len(raw), 0x414C454D, 0) + raw
    assert rtf_to_text(decompress_rtf(mela)).split() == ["Hello", "World"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
