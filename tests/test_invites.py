"""Calendar invitations inside mail: .ics parsing, invite fields and the per-mailbox invites calendar."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mailvault.core.invite import parse_ics  # noqa: E402
from mailvault.core.library import open_path  # noqa: E402
from mailvault.core.mime import parse_eml  # noqa: E402

ICS = """BEGIN:VCALENDAR\r
METHOD:{method}\r
PRODID:test\r
BEGIN:VTIMEZONE\r
TZID:W. Europe Standard Time\r
BEGIN:STANDARD\r
DTSTART:16010101T030000\r
TZOFFSETFROM:+0200\r
TZOFFSETTO:+0100\r
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10\r
END:STANDARD\r
BEGIN:DAYLIGHT\r
DTSTART:16010101T020000\r
TZOFFSETFROM:+0100\r
TZOFFSETTO:+0200\r
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3\r
END:DAYLIGHT\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:abc-{n}\r
SUMMARY:Quarterly planning\r
DTSTART;TZID=W. Europe Standard Time:2026051{n}T090000\r
DTEND;TZID=W. Europe Standard Time:2026051{n}T103000\r
LOCATION:Room 2\\, North wing\r
ORGANIZER;CN="Dana Whitfield":mailto:dana@example.com\r
ATTENDEE;CN=Sam;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:mailto:sam@example.com\r
ATTENDEE;CN=Ana;ROLE=OPT-PARTICIPANT:mailto:ana@example.com\r
{status}END:VEVENT\r
END:VCALENDAR\r
"""


def eml(n, method="REQUEST", status=""):
    ics = ICS.format(method=method, n=n, status=status)
    return (f"From: Dana Whitfield <dana@example.com>\r\nTo: sam@example.com\r\nSubject: Invitation: Quarterly planning {n}\r\n"
            f"Date: Mon, 4 May 2026 10:00:00 +0000\r\nMessage-ID: <inv{n}@example.com>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: multipart/alternative; boundary=b1\r\n\r\n--b1\r\nContent-Type: text/plain\r\n\r\nYou are invited.\r\n"
            f"--b1\r\nContent-Type: text/calendar; charset=utf-8; method={method}\r\n\r\n{ics}--b1--\r\n").encode()


def test_parse():
    ev = parse_ics(ICS.format(method="REQUEST", n=4, status=""))
    assert ev["start"].isoformat() == "2026-05-14T07:00:00+00:00", ev["start"]      # CEST = UTC+2
    assert ev["end"].isoformat() == "2026-05-14T08:30:00+00:00"
    assert ev["location"] == "Room 2, North wing"
    assert ev["organizer"] == "Dana Whitfield <dana@example.com>"
    assert [a["status"] for a in ev["attendees"]] == ["ACCEPTED", "NEEDS-ACTION"]
    allday = parse_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20261224\nSUMMARY:Holiday\nEND:VEVENT\nEND:VCALENDAR")
    assert allday["all_day"] and (allday["end"] - allday["start"]).days == 1
    assert parse_ics("BEGIN:VCALENDAR\nEND:VCALENDAR") is None
    print("ok parse")


def test_mail():
    m = parse_eml(eml(4))
    inv = m.item["invite"]
    assert m.kind == "mail" and inv["label"] == "Meeting invitation" and inv["organizer"].startswith("Dana")
    s = m.summary()
    assert s["start"].startswith("2026-05-14T07:00") and s["invite"]["method"] == "REQUEST"
    c = parse_eml(eml(5, "CANCEL", "STATUS:CANCELLED\r\n"))
    assert c.item["invite"]["label"] == "Meeting cancelled"
    plain = parse_eml(b"From: a@b.c\r\nSubject: hi\r\n\r\nno invite here\r\n")
    assert "invite" not in plain.item and "start" not in plain.item
    print("ok mail")


def test_invites_folder():
    from mailvault.server.app import Source
    d = tempfile.mkdtemp()
    p = os.path.join(d, "box.mbox")
    with open(p, "wb") as fh:
        for i, raw in enumerate([eml(4), b"From: x@y.z\r\nSubject: plain\r\n\r\nhello\r\n", eml(6, "CANCEL", "STATUS:CANCELLED\r\n")]):
            fh.write(b"From - Mon May 4 10:00:00 2026\r\n" + raw + b"\r\n")
    src = Source("s1", "box.mbox", open_path(p), p)
    inv = [f for f in src.folders.values() if f.kind == "invites"]
    assert len(inv) == 1 and inv[0].view == "calendar"
    rows = src.invite_rows()
    assert len(rows) == 2, rows
    assert all("fid" in r and "start" in r for r in rows)
    m = src.message(rows[0]["fid"], rows[0]["idx"])
    assert m.item.get("invite")
    print("ok invites folder")


def test_spam():
    from mailvault.core.state import is_spam
    assert is_spam("X-MS-Exchange-Organization-SCL: 6\r\n")
    assert is_spam("X-Forefront-Antispam-Report: CIP:1.2.3.4;SFV:SPM;SCL:9;\r\n")
    assert not is_spam("X-Forefront-Antispam-Report: CIP:1.2.3.4;SCL:1;\r\nX-Microsoft-Antispam: BCL:0;\r\n")
    assert is_spam("X-Spam-Flag: YES\r\n") and not is_spam("X-Spam-Status: No, score=0.1\r\n")
    assert is_spam("", "[SPAM] cheap watches") and not is_spam("", "Re: spam filter settings")
    print("ok spam")


if __name__ == "__main__":
    test_spam()
    test_parse()
    test_mail()
    test_invites_folder()
