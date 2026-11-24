"""Library folders end to end, in web/Docker, Nextcloud (token) and AppAPI modes.
Run: python tests/make_fixtures.py && python tests/test_library.py"""
import base64
import http.cookiejar
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FX = os.path.join(ROOT, "tests", "fixtures")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start(env_extra):
    port = free_port()
    data = tempfile.mkdtemp()
    env = {**os.environ, "MAILVAULT_CACHE": data, **env_extra}
    env.pop("MAILVAULT_ROOTS", None) if "MAILVAULT_ROOTS" not in env_extra else None
    p = subprocess.Popen([sys.executable, "-m", "mailvault", "serve", "--port", str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/heartbeat", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    return p, port


class Client:
    def __init__(self, port, headers=None):
        self.port, self.headers = port, headers or {}
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def __call__(self, path, data=None, method=None, ok=True):
        body = json.dumps(data).encode() if data is not None else None
        h = {**self.headers, **({"content-type": "application/json"} if data is not None else {})}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, method=method, headers=h)
        try:
            with self.op.open(req, timeout=60) as r:
                out = json.loads(r.read())
                assert ok, f"expected failure for {path}"
                return out
        except urllib.error.HTTPError as e:
            if ok:
                raise AssertionError(f"{path}: {e.code} {e.read()[:200]}")
            return e.code


# two "server" folders with nested mail files, plus a private folder
base = tempfile.mkdtemp()
a, b, secret = os.path.join(base, "archives"), os.path.join(base, "legal", "2019"), os.path.join(base, "hr")
for d in (a, os.path.join(a, "old"), b, secret):
    os.makedirs(d)
shutil.copy(os.path.join(FX, "archive2019.pst"), a)
shutil.copy(os.path.join(FX, "lunch.eml"), os.path.join(a, "old"))
shutil.copy(os.path.join(FX, "backup.zip"), b)
shutil.copy(os.path.join(FX, "photos.msg"), secret)
open(os.path.join(a, "notes.docx"), "w").write("not mail")
outside = tempfile.mkdtemp()      # a real folder outside the allowed base, on any OS (no /etc on Windows)

# ------------------------------------------------------------ web / Docker
p, port = start({"MAILVAULT_LIBRARY_ALLOW": base})
try:
    c = Client(port)
    cfg = c("/api/config")
    assert cfg["library"]["can_edit"] and cfg["host"] == "web"
    picks = c(f"/api/browse?pick=1&path={base}")
    assert {e["name"] for e in picks["entries"]} == {"archives", "legal", "hr"}
    assert c(f"/api/browse?pick=1&path={outside}", ok=False) == 403            # outside MAILVAULT_LIBRARY_ALLOW
    assert c("/api/library", {"path": outside}, "POST", ok=False) == 403
    f1 = c("/api/library", {"path": a, "label": "Archives"}, "POST")
    f2 = c("/api/library", {"path": b}, "POST")
    lib = c("/api/library")["folders"]
    assert [f["label"] for f in lib] == ["Archives", "2019"]
    sc = c(f"/api/library/{f1['id']}/scan")
    assert sc["count"] == 2 and [x["name"] for x in sc["files"]] == ["archive2019.pst"]
    assert sc["dirs"][0]["name"] == "old" and sc["dirs"][0]["files"][0]["name"] == "lunch.eml"
    src = c(f"/api/library/{f1['id']}/open", {"path": os.path.join(a, "archive2019.pst")}, "POST")
    assert src["tree"]["name"] == "Archive 2019"
    again = c(f"/api/library/{f1['id']}/open", {"path": os.path.join(a, "archive2019.pst")}, "POST")
    assert again["id"] == src["id"] and again.get("existing")                # no duplicate mailbox
    assert c(f"/api/library/{f1['id']}/open", {"path": os.path.join(secret, "photos.msg")}, "POST", ok=False) == 403
    assert c("/api/open", {"path": os.path.join(secret, "photos.msg")}, "POST", ok=False) == 403
    assert c("/api/open", {"path": os.path.join(b, "backup.zip")}, "POST")["name"] == "backup.zip"
    c(f"/api/library/{f2['id']}", {"label": "Legal 2019"}, "PATCH")
    assert c("/api/library")["folders"][1]["label"] == "Legal 2019"
    c(f"/api/library/{f2['id']}", method="DELETE")
    assert len(c("/api/library")["folders"]) == 1
finally:
    p.terminate()
print("ok web/Docker library")

# folders persist across restarts and MAILVAULT_ROOTS folders are always there
data_dir = tempfile.mkdtemp()
for i in range(2):
    p, port = start({"MAILVAULT_CACHE": data_dir, "MAILVAULT_ROOTS": secret})
    try:
        c = Client(port)
        if i == 0:
            c("/api/library", {"path": a}, "POST")
        labels = sorted(f["label"] for f in c("/api/library")["folders"])
        assert labels == ["archives", "hr"], labels
    finally:
        p.terminate()
print("ok library persists, mounted roots listed")

# ------------------------------------------------------------ Nextcloud (PHP app token mode)
p, port = start({"MAILVAULT_TOKEN": "t0k", "MAILVAULT_LIBRARY_ALLOW": base})
try:
    def nc(user, admin=False, groups=""):
        return Client(port, {"X-MailVault-Token": "t0k", "X-MailVault-User": user,
                             "X-MailVault-Admin": "1" if admin else "0", "X-MailVault-Groups": groups})
    admin, alice, bob, carol = nc("admin", True), nc("alice"), nc("bob", groups="finance"), nc("carol", groups="sales")
    assert alice("/api/library", {"path": a}, "POST", ok=False) == 403         # users can't add server folders
    assert alice("/api/browse?pick=1", ok=False) == 403
    fa = admin("/api/library", {"path": a, "label": "Shared archives"}, "POST")
    fh = admin("/api/library", {"path": secret, "label": "HR", "scope": {"mode": "list", "who": ["alice", "finance"]}}, "POST")
    fx = admin("/api/library", {"path": b, "label": "Admin only", "scope": {"mode": "admins"}}, "POST")
    see = lambda cl: sorted(f["label"] for f in cl("/api/library")["folders"])  # noqa: E731
    assert see(admin) == ["Admin only", "HR", "Shared archives"]
    assert see(alice) == ["HR", "Shared archives"]                            # named user
    assert see(bob) == ["HR", "Shared archives"]                              # via group
    assert see(carol) == ["Shared archives"]
    assert "scope" not in carol("/api/library")["folders"][0]                 # users don't see who else has access
    assert carol(f"/api/library/{fh['id']}/scan", ok=False) == 404
    assert carol("/api/open", {"path": os.path.join(secret, "photos.msg")}, "POST", ok=False) == 403
    assert bob("/api/open", {"path": os.path.join(secret, "photos.msg")}, "POST")["name"] == "photos.msg"
    assert alice(f"/api/library/{fx['id']}/open", {"path": os.path.join(b, "backup.zip")}, "POST", ok=False) == 404
    assert alice(f"/api/library/{fa['id']}", method="DELETE", ok=False) == 403
    admin(f"/api/library/{fh['id']}", {"scope": {"mode": "admins"}}, "PATCH")
    assert see(alice) == ["Shared archives"]
finally:
    p.terminate()
print("ok Nextcloud permissions (admins, users, groups)")

# ------------------------------------------------------------ AppAPI (ExApp) with a fake Nextcloud
NC_FILES = {"/Mail/2019/archive2019.pst": (101, os.path.join(FX, "archive2019.pst")),
            "/Mail/lunch.eml": (102, os.path.join(FX, "lunch.eml"))}


class FakeNC(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def user(self):
        u, _, s = base64.b64decode(self.headers["AUTHORIZATION-APP-API"]).decode().partition(":")
        assert s == "sek"
        return u

    def do_GET(self):
        u = self.user()
        if "/groups" in self.path:
            groups = ["admin"] if u == "boss" else ["staff"]
            out = json.dumps({"ocs": {"data": {"groups": groups}}}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(out); return
        rel = urllib.request.unquote(self.path.split(f"/files/{u}", 1)[1])
        data = open(NC_FILES[rel][1], "rb").read()
        self.send_response(200); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_SEARCH(self):
        u = self.user()
        body = self.rfile.read(int(self.headers["content-length"])).decode()
        self.send_response(207); self.end_headers()
        if "<oc:fileid/></d:prop></d:eq>" in body or "<d:eq>" in body:          # lookup by file id
            fid = int(body.split("<d:literal>")[1].split("<")[0])
            path = next(k for k, v in NC_FILES.items() if v[0] == fid)
            self.wfile.write(f"<d:multistatus xmlns:d='DAV:'><d:response><d:href>/remote.php/dav/files/{u}{path}</d:href></d:response></d:multistatus>".encode())
            return
        resp = "".join(f"<d:response><d:href>/remote.php/dav/files/{u}{k}</d:href><d:propstat><d:prop><oc:fileid>{v[0]}</oc:fileid>"
                       f"<d:getcontentlength>{os.path.getsize(v[1])}</d:getcontentlength></d:prop></d:propstat></d:response>"
                       for k, v in NC_FILES.items())
        self.wfile.write(f"<d:multistatus xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'>{resp}</d:multistatus>".encode())


ncport = free_port()
srv = http.server.ThreadingHTTPServer(("127.0.0.1", ncport), FakeNC)
threading.Thread(target=srv.serve_forever, daemon=True).start()
env = {"APP_ID": "mailvault", "APP_SECRET": "sek", "NEXTCLOUD_URL": f"http://127.0.0.1:{ncport}",
       "MAILVAULT_LIBRARY_ALLOW": base, "APP_HOST": "127.0.0.1"}
app_port = free_port()
env["APP_PORT"] = str(app_port)
p = subprocess.Popen([sys.executable, "-m", "mailvault", "serve"], cwd=ROOT,
                     env={**os.environ, "MAILVAULT_CACHE": tempfile.mkdtemp(), **env},
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{app_port}/heartbeat", timeout=1); break
    except Exception:
        time.sleep(0.1)
try:
    auth = lambda u: {"AUTHORIZATION-APP-API": base64.b64encode(f"{u}:sek".encode()).decode()}  # noqa: E731
    boss, staff = Client(app_port, auth("boss")), Client(app_port, auth("sam"))
    assert boss("/api/config")["library"]["admin"] and not staff("/api/config")["library"]["can_edit"]
    assert staff("/api/library", {"path": a}, "POST", ok=False) == 403
    boss("/api/library", {"path": a, "label": "Server archives"}, "POST")
    mine = staff("/api/library", {"kind": "nextcloud", "path": "/Mail", "label": "My mail"}, "POST")
    assert sorted(f["label"] for f in staff("/api/library")["folders"]) == ["My mail", "Server archives"]
    assert [f["label"] for f in boss("/api/library")["folders"]] == ["Server archives"]   # personal folders stay private
    sc = staff(f"/api/library/{mine['id']}/scan")
    assert sc["count"] == 2 and sc["dirs"][0]["name"] == "2019" and sc["files"][0]["fileId"] == 102
    src = staff(f"/api/library/{mine['id']}/open", {"fileId": 101}, "POST")
    assert src["tree"]["name"] == "Archive 2019"
finally:
    p.terminate()
    srv.shutdown()
print("ok AppAPI library (server folders + personal Nextcloud folders)")
