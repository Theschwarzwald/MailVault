"""Windows app: opened mailbox files and Library folders are laid out by account, and new mail is picked up."""
import os
import shutil
import sys
import tempfile
import time

tmp = tempfile.mkdtemp()
os.environ["MAILVAULT_CACHE"] = os.path.join(tmp, "cache")
os.environ["MAILVAULT_COLLECTIONS"] = os.path.join(tmp, "collections")
os.environ["MAILVAULT_LIBRARY_FILE"] = os.path.join(tmp, "library.json")
os.environ["MAILVAULT_UPLOADS"] = os.path.join(tmp, "uploads")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.testclient import TestClient  # noqa: E402

from mailvault.server import app as srv  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def find(tree, name):
    if tree["name"] == name:
        return tree
    for ch in tree["children"]:
        r = find(ch, name)
        if r:
            return r
    return None


def wait(c, cond, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get("/api/collection").json()
        if j.get("collection") and j["collection"]["state"] == "ready" and cond(j):
            return j
        time.sleep(0.3)
    raise AssertionError(f"timed out: {j.get('collection')}")


def main():
    srv.TOKEN, srv.LOCK = "desk", True
    srv.ROOTS[:] = [FIX, tmp]
    c = TestClient(srv.app)
    c.cookies.set("mv_auth", "desk")
    assert c.get("/api/config").json()["host"] == "desktop"

    # a single message opened on its own is just shown, not filed
    r = c.post("/api/open", json={"path": os.path.join(FIX, "lunch.eml")}).json()
    assert "tree" in r and not r.get("collection"), r

    # a mailbox file joins your mail, laid out by account
    r = c.post("/api/open", json={"path": os.path.join(FIX, "archive2019.pst")}).json()
    assert r == {"collection": True, "name": "archive2019.pst"}, r
    j = wait(c, lambda j: find(j["tree"], "Files") and len(find(j["tree"], "Files")["children"]) == 1)
    accts = [a for a in j["tree"]["children"] if a["kind"] == "account"]
    assert accts and find(accts[0], "Inbox") and find(accts[0], "Sent Items"), j["tree"]
    pst = find(j["tree"], "Files")["children"][0]
    assert pst["path"].endswith("archive2019.pst"), pst

    # a Library folder laid out like a mailbox: its folders become the account's folders
    lib = os.path.join(tmp, "mail")
    os.makedirs(os.path.join(lib, "me@work.com", "Inbox"))
    os.makedirs(os.path.join(lib, "me@work.com", "Projects"))
    shutil.copy(os.path.join(FIX, "lunch.eml"), os.path.join(lib, "me@work.com", "Inbox"))
    shutil.copy(os.path.join(FIX, "photos.msg"), os.path.join(lib, "me@work.com", "Projects"))
    assert c.post("/api/library", json={"kind": "server", "path": lib, "label": "Mail"}).status_code == 200
    j = wait(c, lambda j: find(j["tree"], "me@work.com") is not None)
    me = find(j["tree"], "me@work.com")
    assert find(me, "Inbox")["count"] == 1 and find(me, "Projects")["count"] == 1, me

    # new mail dropped into the folder shows up on the next check (every five minutes in the app)
    v = j["collection"]["version"]
    shutil.copy(os.path.join(FIX, "lunch.eml"), os.path.join(lib, "me@work.com", "Inbox", "lunch2.eml"))
    srv.desktop_sync()
    j = wait(c, lambda j: j["collection"]["version"] > v and find(find(j["tree"], "me@work.com"), "Inbox")["count"] == 2)
    # nothing new: nothing rebuilt
    v = j["collection"]["version"]
    srv.desktop_sync()
    time.sleep(1.5)
    assert c.get("/api/collection").json()["collection"]["version"] == v

    # library files can't be removed one by one; opened files can
    assert c.post("/api/collection/forget", json={"path": os.path.join(lib, "me@work.com", "Inbox", "lunch.eml")}).status_code == 400
    assert c.post("/api/collection/forget", json={"path": pst["path"]}).json()["ok"]
    j = wait(c, lambda j: all(x["name"] != "archive2019.pst" for x in find(j["tree"], "Files")["children"]))
    print("ok desktop mail:", [a["name"] for a in j["tree"]["children"]])


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
