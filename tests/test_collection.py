"""The mail collection: import mapped files, auto-sort them, serve rows and messages through the API."""
import os
import shutil
import sys
import tempfile
import time

tmp = tempfile.mkdtemp()
os.environ["MAILVAULT_CACHE"] = os.path.join(tmp, "cache")
os.environ["MAILVAULT_COLLECTIONS"] = os.path.join(tmp, "collections")
os.environ["MAILVAULT_TOKEN"] = "t0ken"
os.environ["MAILVAULT_UPLOADS"] = os.path.join(tmp, "uploads")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.testclient import TestClient  # noqa: E402

from mailvault.server import app as srv  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
H = {"X-MailVault-Token": "t0ken", "X-MailVault-User": "alice"}


def wait_ready(c, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = c.get("/api/collection", headers=H).json()["collection"]
        if st and st["state"] == "ready" and st["version"] > 0:
            return st
        time.sleep(0.3)
    raise AssertionError(f"collection not ready: {st}")


def find(tree, name):
    if tree["name"] == name:
        return tree
    for ch in tree["children"]:
        r = find(ch, name)
        if r:
            return r
    return None


def test_placement():
    from mailvault.server.collection import Collection as C
    assert C._place(["me@x.com", "sent"]) == ("Sent", ())
    assert C._place(["me@x.com", "Projects"]) == (None, ("Projects",))
    assert C._place(["Top of Personal Folders", "Inbox", "Projects", "A"]) == ("Inbox", ("Projects", "A"))
    assert C._place(["Robin"]) == (None, ("Robin",)) and C._place([]) == (None, ())
    me = {"me@x.com"}
    mine = {"sender_email": "me@x.com", "to": "a@b.c", "states": ["Sent"]}
    other = {"sender_email": "a@b.c", "to": "me@x.com", "states": ["Received"]}
    assert C._file_as(mine, "Inbox", (), me) == ("Sent", ())
    assert C._file_as(mine, None, ("Projects",), me) == (None, ("Projects",))
    assert C._file_as(other, None, (), me) == ("Inbox", ())
    assert C._file_as({**other, "states": ["Received", "Junk"]}, "Inbox", (), me) == ("Junk", ())
    assert C._file_as({"sender_email": "me@x.com", "to": "", "states": []}, None, (), me) == ("Drafts", ())
    print("ok placement")


def main():
    test_placement()
    c = TestClient(srv.app)
    names = ["archive2019.pst", "invites.mbox", "lunch.eml", "photos.msg", "gmail.mbox"]
    files = [{"id": str(i + 1), "name": n, "path": os.path.join(FIX, n), "size": os.path.getsize(os.path.join(FIX, n)),
              "mtime": 1, "folder": "Mail archive"} for i, n in enumerate(names)]
    # one file the engine cannot read in place: it must be uploaded
    files.append({"id": "99", "name": "takeout.mbox", "path": "/nonexistent/takeout.mbox", "size": 1, "mtime": 1, "folder": "x"})
    r = c.post("/api/collection/sync", json={"files": files}, headers=H).json()
    assert r["need_upload"] == ["99"], r
    with open(os.path.join(FIX, "takeout.mbox"), "rb") as fh:
        assert c.post("/api/collection/upload?id=99", content=fh.read(), headers=H).json()["ok"]
    st = wait_ready(c)
    assert st["files"] == 6 and st["imported"] == 6 and not st["errors"], st
    srcs = c.get("/api/sources", headers=H).json()
    allmail = srcs[0]
    assert allmail["id"] == "all" and allmail["name"] == "All mail"
    tree = allmail["tree"]
    groups = [g["name"] for g in tree["children"]]
    assert groups[-1] == "More views", groups
    accts = [a for a in tree["children"] if a["kind"] == "account"]
    print("accounts:", [a["name"] for a in accts])
    assert accts and all("@" in a["name"] or a["name"] == "Unknown account" for a in accts), accts
    std = ["Inbox", "Drafts", "Sent Items", "Deleted Items", "Archive", "Junk Email"]
    for a in accts:
        if a["name"] != "Unknown account":
            assert [x["name"] for x in a["children"]][:6] == std, a["children"]
    acct = accts[0]
    hits = c.get(f"/api/sources/all/folders/{acct['id']}?q=lunch", headers=H).json()
    print("search 'lunch' in", acct["name"], "->", hits["total"])
    # listing your own addresses re-sorts without re-importing
    c.post("/api/collection/sync", json={"files": files, "accounts": ["priya@example.com"]}, headers=H)
    st = wait_ready(c)
    print("groups:", groups, "messages:", st["messages"])
    # the 5-minute rescan sends the same list again: nothing is rebuilt
    c.post("/api/collection/sync", json={"files": files, "accounts": ["priya@example.com"]}, headers=H)
    time.sleep(2)
    assert c.get("/api/collection", headers=H).json()["collection"]["version"] == st["version"]
    for g in tree["children"]:
        print(" ", g["name"], "->", ", ".join(f"{ch['name']} ({ch['count']})" for ch in g["children"][:8]))
    cals = [x for a in accts for x in a["children"] if x["name"] == "Calendar"]
    assert cals and all(x["view"] == "calendar" for x in cals) and sum(x["count"] for x in cals) >= 3, cals
    cal = max(cals, key=lambda x: x["count"])
    assert sum(find(a, "Inbox")["count"] for a in accts) >= 1
    by_file = [x["name"] for x in find(tree, "Files")["children"]]
    assert len(by_file) == 5 and "Mail archive" in by_file, by_file      # lunch.eml + photos.msg grouped by folder
    rows = c.get(f"/api/sources/all/folders/{cal['id']}?limit=50", headers=H).json()
    assert rows["total"] == cal["count"] and all("sid" in x and "fid" in x for x in rows["items"])
    x = rows["items"][0]
    m = c.get(f"/api/sources/{x['sid']}/folders/{x['fid']}/{x['idx']}", headers=H).json()
    assert m["subject"] == x["subject"], (m["subject"], x["subject"])
    # another user sees nothing of alice's collection
    bob = {"X-MailVault-Token": "t0ken", "X-MailVault-User": "bob"}
    assert c.get(f"/api/sources/{x['sid']}/folders/{x['fid']}/{x['idx']}", headers=bob).status_code == 404
    assert c.get("/api/sources", headers=bob).json() == []
    # removing files from the scan folders removes them from the collection
    c.post("/api/collection/sync", json={"files": files[:2]}, headers=H)
    deadline = time.time() + 60
    while time.time() < deadline:
        st = c.get("/api/collection", headers=H).json()["collection"]
        if st["state"] == "ready" and st["imported"] == 2:
            break
        time.sleep(0.3)
    assert st["imported"] == 2 and len(find(c.get("/api/sources", headers=H).json()[0]["tree"], "Files")["children"]) == 2, st
    print("ok collection")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
