"""End-to-end API tests: runs the real server on a free port.
Run: python tests/make_fixtures.py && python tests/test_server.py"""
import http.cookiejar
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
import io

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["MAILVAULT_CACHE"] = tempfile.mkdtemp()
FX = os.path.join(ROOT, "tests", "fixtures")

import uvicorn  # noqa: E402
from mailvault.server import app as srv  # noqa: E402

s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
srv.ROOTS[:] = [FX]
server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.05)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(path, data=None, method=None, raw=False):
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=body, method=method,
                                 headers={"content-type": "application/json"} if isinstance(data, dict) else {})
    with opener.open(req, timeout=60) as r:
        b = r.read()
        return b if raw else json.loads(b)


def wait(pred, secs=30):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    raise AssertionError("timed out")


src = call("/api/open", {"path": os.path.join(FX, "archive2019.pst")})
sid = src["id"]
folders = {}


def walk(f):
    folders[f["name"]] = f
    for c in f["children"]:
        walk(c)


walk(src["tree"])
assert folders["Calendar"]["view"] == "calendar" and folders["Contacts"]["view"] == "contacts"
wait(lambda: call("/api/status")["index"][sid]["state"] == "ready")
print("ok index built")

hits = call("/api/search?q=portal")["items"]
assert [h["subject"] for h in hits] == ["Password reset"], hits
fd = {h["subject"] for h in call("/api/search?q=from:dana")["items"]}
assert "Q3 invoice attached" in fd and "Password reset" not in fd, fd
print("ok full-text search")

sent = call(f"/api/sources/{sid}/folders/{folders['Sent Items']['id']}?state=Sent")["items"]
assert {r["subject"] for r in sent} == {"Re: shipping dates", "Fwd: proposal"}
assert call(f"/api/sources/{sid}/folders/{folders['Sent Items']['id']}?category=Customers")["total"] == 1
cal = call(f"/api/sources/{sid}/folders/{folders['Calendar']['id']}")["items"]
assert all("Sent" not in r["states"] and "Received" not in r["states"] for r in cal)
print("ok states and filters")

job = call("/api/export", {"sid": sid, "fid": 0, "fmt": "vcf", "scope": "all"})
wait(lambda: call(f"/api/jobs/{job['id']}")["state"] == "done")
z = zipfile.ZipFile(io.BytesIO(call(f"/api/jobs/{job['id']}/download", raw=True)))
assert any(n.endswith("Contacts.vcf") for n in z.namelist())
print("ok export job")

job = call("/api/export", {"sid": sid, "fid": 0, "fmt": "pst", "scope": "all"})
wait(lambda: call(f"/api/jobs/{job['id']}")["state"] == "done", 60)
pst_bytes = call(f"/api/jobs/{job['id']}/download", raw=True)
assert pst_bytes[:4] == b"!BDN"
print("ok PST export job")

job = call(f"/api/sources/{sid}/deepscan", data=b"", method="POST")
wait(lambda: call(f"/api/jobs/{job['id']}")["state"] == "done")
tree = [s for s in call("/api/sources") if s["id"] == sid][0]["tree"]
assert any(c["name"].startswith("Deep scan results") for c in tree["children"])
print("ok deep scan")

pdf = call(f"/api/sources/{sid}/report", raw=True)
assert pdf[:4] == b"%PDF"
print("ok recovery report")

msg = call("/api/open", {"path": os.path.join(FX, "photos.msg")})
m = call(f"/api/sources/{msg['id']}/folders/0/0")
png = [a for a in m["attachments"] if a["name"] == "sunset.png"][0]
assert png["preview"] == "image"
assert call(f"/api/sources/{msg['id']}/folders/0/0/attachments/{png['i']}/preview", raw=True)[:4] == b"\x89PNG"
print("ok attachment preview")
server.should_exit = True
