"""HTTP API + static web UI. Used by Docker, the desktop app and the Nextcloud app."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..core import export as X
from ..core.index import Index, fingerprint
from ..core.library import open_bytes, open_path, sniff
from ..core.mime import build_eml, html_to_text
from ..core.model import RECOVERED, Folder, Message, safe_load
from ..core.state import _SENT, finalize, guess_owner, is_spam
from ..core.items import item_rows
from ..core.invite import apply_invite
from .jobs import JOBS, Job, start_job
from .preview import preview, preview_kind
from . import exapp
from .. import __version__
from . import collection as C
from . import library as L

WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
ROOTS: list[str] = []
PASSWORD = os.environ.get("MAILVAULT_PASSWORD", "")
TOKEN = os.environ.get("MAILVAULT_TOKEN", "")          # shared secret for the Nextcloud app
LOCK = os.environ.get("MAILVAULT_LOCK", "") == "1"      # desktop: every request must carry the token
UPLOAD_DIR = os.environ.get("MAILVAULT_UPLOADS") or os.path.join(tempfile.gettempdir(), "mailvault")
MAX_UPLOAD = int(os.environ.get("MAILVAULT_MAX_UPLOAD_MB", "20480")) << 20
os.makedirs(UPLOAD_DIR, exist_ok=True)


class Source:
    def __init__(self, sid: str, name: str, root: Folder, path: str, key: str | None = None):
        self.id, self.name, self.root, self.path = sid, name, root, path
        self.summaries: dict[int, list] = {}
        self.cache: OrderedDict = OrderedDict()
        self.lock = threading.Lock()
        self._owner: str | None = None
        self.deep_scanned = False
        self._number()
        self.index: Index | None = None
        if key:
            try:
                self.index = Index(key)
                if self.index.state != "ready" or int(self.index.get_meta("items") or -1) != self.item_count:
                    self.index.set_meta(items=self.item_count)
                    self.index.state = "empty"
                    threading.Thread(target=self.index.build, args=(self,), daemon=True).start()
            except Exception:  # noqa: BLE001 - search falls back to in-memory scanning
                self.index = None

    def _number(self):
        self.folders: dict[int, Folder] = {}
        self.paths: dict[int, list[str]] = {}

        def walk(f, path):
            fid = len(self.folders)
            self.folders[fid] = f
            f.fid = fid  # type: ignore[attr-defined]
            self.paths[fid] = path
            for c in f.children:
                walk(c, path + [c.name])
        if not any(c.kind == "invites" for c in self.root.children) and \
                any(f.items for f in self.root.walk() if f.view == "mail"):
            self.root.children.append(Folder("Calendar invites", kind="invites", view="calendar"))
        walk(self.root, [])
        self.item_count = sum(len(f.items) for f in self.folders.values())

    def tree(self, f: Folder | None = None) -> dict:
        f = f or self.root
        return {"id": f.fid, "name": f.name, "kind": f.kind, "view": f.view,  # type: ignore[attr-defined]
                "count": len(f.items), "error": f.error, "children": [self.tree(c) for c in f.children]}

    @property
    def owner(self) -> str:
        """Mailbox owner's address, used to tell sent from received mail."""
        if self._owner is None:
            sent, recv = [], []
            for fid, f in self.folders.items():
                is_sent = bool(_SENT.search(" / ".join(self.paths[fid])))
                for i in range(min(len(f.items), 200)):
                    q = f.quick[i]() if i < len(f.quick) and f.quick[i] else None
                    if q is None:
                        m = self._raw(fid, i)
                        email, to = m.sender.email, [a.email for a in m.to]
                    else:
                        email, to = q.get("sender_email", ""), []
                    (sent.append(email) if is_sent else recv.extend(to))
            self._owner = guess_owner(sent, recv)
        return self._owner

    def _raw(self, fid: int, idx: int) -> Message:
        m = safe_load(self.folders[fid].items[idx])
        try:
            apply_invite(m)          # .ics invites inside PST/MSG mail; EML/MBOX do this while parsing
            if m.kind == "mail" and "Junk" not in m.states and is_spam(m.headers, m.subject):
                m.states = list(m.states) + ["Junk"]
        except Exception:  # noqa: BLE001
            pass
        return m

    def invite_rows(self) -> list[dict]:
        """Every calendar invitation in the mailbox: .ics invites in mail and Outlook meeting requests."""
        rows = []
        for fid, f in self.folders.items():
            if f.kind == "invites" or f.view != "mail":
                continue
            for r in self.summaries_for(fid):
                if r.get("invite") or (r.get("kind") == "appointment" and r.get("start")):
                    rows.append({**r, "fid": fid, "folder": " / ".join(self.paths[fid])})
        return rows

    def _final_states(self, fid, intrinsic, status, sender_email, received_hdr, kind="mail") -> list[str]:
        own = self._owner if self._owner is not None else ""
        if kind == "mail" and not intrinsic and self._owner is None:
            own = self.owner
        return finalize(intrinsic, self.paths.get(fid, []), status, sender_email, own, received_hdr, kind)

    def message(self, fid: int, idx: int, cache: bool = True) -> Message:
        key = (fid, idx)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
        m = self._raw(fid, idx)
        if not m.extra.get("state_done"):
            m.states = self._final_states(fid, m.states, m.status, m.sender.email, m.extra.get("received_hdr", False), m.kind)
            m.extra["state_done"] = True
        if cache:
            with self.lock:
                self.cache[key] = m
                while len(self.cache) > 300:
                    self.cache.popitem(last=False)
        return m

    def summary_of(self, fid: int, idx: int, m: Message | None = None) -> dict:
        f = self.folders[fid]
        q = f.quick[idx] if m is None and idx < len(f.quick) else None
        if q is not None:
            s = q()
            s["states"] = self._final_states(fid, s.get("states", []), s["status"], s.get("sender_email", ""), True,
                                             s.get("kind", "mail"))
        else:
            m = m or self.message(fid, idx)
            s = m.summary()
        s["idx"] = idx
        return s

    def summaries_for(self, fid: int) -> list[dict]:
        if fid in self.summaries:
            return self.summaries[fid]
        n = len(self.folders[fid].items)
        if self.index and self.index.state == "ready":
            rows = self.index.summaries(fid)
            if rows is not None and len(rows) == n:
                self.summaries[fid] = rows
                return rows
        rows = [self.summary_of(fid, i) for i in range(n)]
        self.summaries[fid] = rows
        return rows

    def search(self, q: str, fid: int | None = None) -> list[tuple[int, int]]:
        if self.index and self.index.state == "ready":
            hits = self.index.search(q)
            return [h for h in hits if fid is None or h[0] == fid]
        terms = q.lower().split()
        out = []
        for f_id in ([fid] if fid is not None else list(self.folders)):
            for r in self.summaries_for(f_id):
                hay = " ".join(str(r.get(k, "")) for k in ("subject", "from", "to", "location", "company",
                                                            "email")).lower()
                if all(t.split(":")[-1] in hay for t in terms):
                    out.append((f_id, r["idx"]))
        return out

    def add_folder(self, f: Folder):
        self.root.children.append(f)
        self._number()

    def close(self):
        if self.index:
            self.index.close()


class Session:
    def __init__(self):
        self.sources: dict[str, Source] = {}
        self.seen = time.time()


SESSIONS: dict[str, Session] = {}


def session(req: Request) -> Session:
    key = req.state.user
    s = SESSIONS.get(key)
    if not s:
        s = SESSIONS[key] = Session()
    s.seen = time.time()
    return s


def _make_source(sid, name, root, path, key):
    return Source(sid, name, root, path, key)


def _coll(req: Request, create: bool = False):
    return C.get(req.state.user, _make_source, create)


def _src(req: Request) -> Source:
    sid = req.path_params["sid"]
    s = session(req).sources.get(sid)
    if not s and sid.startswith("c"):            # a file imported into the user's collection
        c = _coll(req)
        s = c.source(sid) if c else None
    if not s:
        raise KeyError("Mailbox not open")
    return s


def _coll_entry(c) -> dict:
    st = c.status()
    tree = c.tree() or {"id": 0, "name": "All mail", "kind": "store", "view": "mail", "count": 0, "error": "",
                        "children": []}
    return {"id": "all", "name": "All mail", "tree": tree, "index": None, "collection": st}


class Auth(BaseHTTPMiddleware):
    """Single-user cookie session, optional password, or Nextcloud token + user header."""

    async def dispatch(self, req: Request, call_next):
        if req.url.path == "/heartbeat":
            req.state.user = "public"
            return await call_next(req)
        if exapp.ENABLED:   # deployed by Nextcloud AppAPI: every call comes through its proxy
            path = req.url.path
            if path == "/heartbeat" or path.startswith(("/static/", "/js/", "/css/", "/img/")):
                req.state.user = "public"
                return await call_next(req)
            user = exapp.auth_user(req.headers)
            if user is None:
                return Response("Unauthorized", 401)
            req.state.user = "nc:" + (user or "system")
            req.state.nc_user = user
            return await call_next(req)
        tok = req.headers.get("x-mailvault-token")
        if TOKEN and tok and hmac.compare_digest(tok, TOKEN):
            req.state.user = "nc:" + req.headers.get("x-mailvault-user", "anon")
            return await call_next(req)
        if LOCK:
            q, c = req.query_params.get("t", ""), req.cookies.get("mv_auth", "")
            if TOKEN and q and hmac.compare_digest(q, TOKEN):
                req.state.user = "desktop"
                resp = await call_next(req)
                resp.set_cookie("mv_auth", TOKEN, httponly=True, samesite="strict")
                return resp
            if not (TOKEN and c and hmac.compare_digest(c, TOKEN)) and not req.url.path.startswith("/static"):
                return Response("Forbidden", 403)
            req.state.user = "desktop"
            desktop_watch()
            return await call_next(req)
        if PASSWORD and not req.url.path.startswith("/static"):
            h = req.headers.get("authorization", "")
            ok = False
            if h.lower().startswith("basic "):
                try:
                    _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                    ok = hmac.compare_digest(pw, PASSWORD)
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="MailVault"'})
        sid = req.cookies.get("mv_session") or secrets.token_hex(16)
        req.state.user = "web:" + hashlib.sha256(sid.encode()).hexdigest()[:16]
        resp = await call_next(req)
        if "mv_session" not in req.cookies:
            resp.set_cookie("mv_session", sid, httponly=True, samesite="strict")
        return resp


def err(e: Exception, code=400):
    return JSONResponse({"error": f"{e}"}, code)


# ------------------------------------------------------------- handlers
async def favicon(req):
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><rect x='2' y='4' width='20' height='16' "
           "rx='2' fill='#0f6cbd'/><path d='M3 6l9 6 9-6' stroke='#fff' stroke-width='2' fill='none'/></svg>")
    return Response(svg, media_type="image/svg+xml")


async def index(req):
    return FileResponse(os.path.join(WEB, "index.html"))


def host_kind(req) -> str:
    u = req.state.user
    if u == "desktop":
        return "desktop"
    if u.startswith("nc:"):
        return "exapp" if exapp.ENABLED else "nextcloud"
    return "web"


def who(req) -> L.Who:
    """Who is asking, for library permissions."""
    kind = host_kind(req)
    if kind == "desktop":
        return L.Who("desktop", True, [], True)
    if kind == "web":
        return L.Who("web", True, [], os.environ.get("MAILVAULT_LIBRARY_EDIT", "1") != "0")
    if kind == "nextcloud":     # the PHP app vouches for admin rights and groups (token-authenticated)
        admin = req.headers.get("x-mailvault-admin") == "1"
        from urllib.parse import unquote
        groups = [unquote(g) for g in req.headers.get("x-mailvault-groups", "").split(",") if g]
        # server folders only when the operator allowed some that actually exist
        return L.Who(req.state.user, admin, groups, admin and any(os.path.isdir(x) for x in L.ALLOW))
    groups = exapp.user_groups(getattr(req.state, "nc_user", ""))
    admin = "admin" in groups
    return L.Who(req.state.user, admin, groups, admin)


def roots_for(req) -> list[str]:
    return list(dict.fromkeys(ROOTS + L.visible_roots(who(req))))


async def config(req):
    w = who(req)
    return JSONResponse({"roots": roots_for(req), "formats": X.FORMATS, "version": __version__, "host": host_kind(req),
                         "library": {"can_edit": w.can_edit, "admin": w.admin,
                                     "allow": L.ALLOW if w.can_edit else []}})


def _within(path: str, roots: list[str]) -> bool:
    rp = os.path.realpath(path)
    return any(rp == os.path.realpath(r) or rp.startswith(os.path.realpath(r).rstrip(os.sep) + os.sep) for r in roots)


def _allowed(path: str, req=None) -> bool:
    return _within(path, roots_for(req) if req is not None else ROOTS)


async def browse(req):
    """Server-side file browser. pick=1 lists folders for adding to the library (editors only)."""
    pick = req.query_params.get("pick") == "1"
    w = who(req)
    if pick:
        if not w.can_edit:
            return err(PermissionError("Only an administrator can add server folders"), 403)
        bases = [a for a in L.ALLOW] or [os.sep]
        start = os.path.expanduser("~") if host_kind(req) == "desktop" and bases == [os.sep] else bases[0]
    else:
        bases = roots_for(req)
        start = bases[0] if bases else ""
    path = req.query_params.get("path") or start
    if not bases:
        return JSONResponse({"path": "", "entries": [], "roots": []})
    if not _within(path, bases):
        return err(PermissionError("Outside the allowed folders"), 403)
    entries = []
    try:
        for e in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            if e.name.startswith(".") or (pick and not e.is_dir()):
                continue
            try:
                size = e.stat().st_size if e.is_file() else 0
            except OSError:
                size = 0
            entries.append({"name": e.name, "dir": e.is_dir(), "path": e.path, "size": size})
    except OSError as e:
        return err(e)
    parent = os.path.dirname(path)
    parent = parent if parent != path and _within(parent, bases) else None
    return JSONResponse({"path": path, "parent": parent, "entries": entries, "roots": bases})


# ---------------------------------------------------------------- library
async def library_list(req):
    w = who(req)
    return JSONResponse({"folders": L.list_folders(w), "can_edit": w.can_edit, "admin": w.admin})


async def library_add(req):
    b = await req.json()
    w = who(req)
    kind = b.get("kind", "server")
    if kind == "nextcloud" and host_kind(req) != "exapp":
        return err(ValueError("Nextcloud folders are handled by the Nextcloud app"))
    f = L.add_folder(w, b.get("path", ""), b.get("label", ""), b.get("scope"), bool(b.get("personal")), kind)
    if host_kind(req) == "desktop":
        _desktop_sync_soon()                           # its mail joins the accounts view
    return JSONResponse(f)


async def library_update(req):
    b = await req.json()
    return JSONResponse(L.update_folder(who(req), req.path_params["lid"], b.get("label"), b.get("scope")))


async def library_remove(req):
    L.remove_folder(who(req), req.path_params["lid"])
    if host_kind(req) == "desktop":
        _desktop_sync_soon()
    return JSONResponse({"ok": True})


async def library_scan(req):
    f = L.find(who(req), req.path_params["lid"])
    refresh = req.query_params.get("refresh") == "1"
    if f["kind"] == "nextcloud":
        return JSONResponse(exapp.dav_scan(getattr(req.state, "nc_user", ""), f["path"], refresh))
    if not os.path.isdir(f["path"]):
        return err(FileNotFoundError(f"{f['path']} is not available right now"), 404)
    return JSONResponse(L.scan(f["path"], refresh))


async def library_open(req):
    f = L.find(who(req), req.path_params["lid"])
    b = await req.json()
    if f["kind"] == "nextcloud":
        user = getattr(req.state, "nc_user", "")
        path, name = exapp.fetch_user_file(user, str(b.get("fileId", "")), UPLOAD_DIR)
        return JSONResponse(_add(req, name, open_path(path, name), path))
    path = b.get("path", "")
    if not _within(path, [f["path"]]) or not os.path.isfile(path):
        return err(PermissionError("That file is not in this folder"), 403)
    return JSONResponse(_add(req, os.path.basename(path), open_path(path), path))


def _add(req, name, root, path) -> dict:
    if path and not path.startswith(UPLOAD_DIR):
        for s in session(req).sources.values():
            if s.path == path:
                return {"id": s.id, "name": s.name, "tree": s.tree(),
                        "index": s.index.status() if s.index else None, "existing": True}
    sid = secrets.token_hex(6)
    key = fingerprint(path, name) if path and os.path.isfile(path) else None
    src = Source(sid, name, root, path, key)
    session(req).sources[sid] = src
    return {"id": sid, "name": name, "tree": src.tree(), "index": src.index.status() if src.index else None}


async def open_file(req):
    body = await req.json()
    path = body.get("path", "")
    if not _allowed(path, req) or not os.path.isfile(path):
        return err(PermissionError("File not available"), 403)
    if host_kind(req) == "desktop" and not path.lower().endswith(SINGLE_EXT):
        try:
            open_path(path)                            # fails now, not silently in the background, if unreadable
        except Exception as e:  # noqa: BLE001
            return err(e)
        _save_opened(_opened() + [path])
        _desktop_sync_soon()
        return JSONResponse({"collection": True, "name": os.path.basename(path)})
    try:
        return JSONResponse(_add(req, os.path.basename(path), open_path(path), path))
    except Exception as e:  # noqa: BLE001
        return err(e)


async def upload(req):
    """Raw body upload (?name=file.pst). Streams to disk so huge PSTs work."""
    name = os.path.basename(req.query_params.get("name") or "upload.bin")
    dst = os.path.join(UPLOAD_DIR, secrets.token_hex(8) + "_" + X.safe_name(name))
    size = 0
    with open(dst, "wb") as fh:
        async for chunk in req.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                fh.close()
                os.remove(dst)
                return err(ValueError("File too large"), 413)
            fh.write(chunk)
    try:
        return JSONResponse(_add(req, name, open_path(dst, name), dst))
    except Exception as e:  # noqa: BLE001
        return err(e)


async def sources(req):
    out = [{"id": s.id, "name": s.name, "tree": s.tree(),
            "index": s.index.status() if s.index else None} for s in session(req).sources.values()]
    c = _coll(req)
    if c and c.files:
        out.insert(0, _coll_entry(c))
    return JSONResponse(out)


async def collection_get(req):
    c = _coll(req)
    return JSONResponse(_coll_entry(c) if c and c.files else {"id": "all", "collection": None})


async def collection_sync(req):
    """The Nextcloud app sends the mail files in the user's scan folders; returns ids it must upload."""
    b = await req.json()
    files = b.get("files") or []
    c = _coll(req, create=bool(files))
    if c is None:
        return JSONResponse({"need_upload": [], "files": 0})
    if isinstance(b.get("accounts"), list):
        c.set_accounts([str(a) for a in b["accounts"]])
    need = c.sync(files)
    return JSONResponse({"need_upload": need, "files": len(c.files), "accounts": c.account_list()})


async def collection_upload(req):
    c = _coll(req)
    if c is None:
        return err(KeyError("No collection"), 404)
    fid = req.query_params.get("id", "")
    tmp = os.path.join(UPLOAD_DIR, secrets.token_hex(8) + ".part")
    size = 0
    with open(tmp, "wb") as fh:
        async for chunk in req.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                fh.close()
                os.remove(tmp)
                return err(ValueError("File too large"), 413)
            fh.write(chunk)
    try:
        c.store_upload(fid, tmp)
    except Exception as e:  # noqa: BLE001
        return err(e, 404)
    return JSONResponse({"ok": True})


# ------------------------------------------------------------ the Windows app's mail
# On the desktop there is no Nextcloud to send a file list, so the engine builds it:
# every mailbox file you open (PST, OST, MBOX, archives) plus every mail file in your
# Library folders goes into one collection, laid out by account like a mail program.
# Library folders are checked again every five minutes for new mail.
DESKTOP_USER = "desktop"
SINGLE_EXT = (".msg", ".eml", ".emlx")        # a single message opened on its own is just read, not filed
_desk_lock = threading.Lock()
_desk_watch = {"started": False}


def _opened_file() -> str:
    os.makedirs(C.DATA, exist_ok=True)
    return os.path.join(C.DATA, "desktop-opened.json")


def _opened() -> list[str]:
    try:
        with open(_opened_file(), encoding="utf-8") as fh:
            return [p for p in json.load(fh) if isinstance(p, str)]
    except (OSError, ValueError):
        return []


def _save_opened(paths: list[str]):
    with open(_opened_file(), "w", encoding="utf-8") as fh:
        json.dump(sorted(set(paths)), fh)


def _fid(path: str) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8", "replace")).hexdigest()[:16]


def desktop_files() -> list[dict]:
    """Opened mailbox files plus the mail files in every Library folder."""
    files = {}
    for p in _opened():
        try:
            st = os.stat(p)
        except OSError:
            continue                                   # moved or on a drive that isn't connected
        files[_fid(p)] = {"id": _fid(p), "name": os.path.basename(p), "path": p, "size": st.st_size,
                          "mtime": int(st.st_mtime), "folder": "Opened files", "where": ""}
    for f in L.list_folders(L.Who(DESKTOP_USER, True, [], True)):
        if f.get("kind") != "server" or not os.path.isdir(f["path"]):
            continue
        tree = L.scan(f["path"], refresh=True)

        def walk(node, rel):
            for x in node["files"]:
                files.setdefault(_fid(x["path"]), {"id": _fid(x["path"]), "name": x["name"], "path": x["path"],
                                                   "size": x["size"], "mtime": x["mtime"],
                                                   "folder": f.get("label") or os.path.basename(f["path"]), "where": rel})
            for d in node["dirs"]:
                walk(d, f"{rel}/{d['name']}" if rel else d["name"])
        walk(tree, "")
    return list(files.values())


def desktop_sync():
    with _desk_lock:
        files = desktop_files()
        c = C.get(DESKTOP_USER, _make_source, create=bool(files))
        if c is not None:
            c.sync(files)


def _desktop_sync_soon():
    threading.Thread(target=lambda: _safe(desktop_sync), daemon=True).start()


def _safe(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 - a bad folder must not stop the watcher
        pass


def desktop_watch():
    """Start the five-minute Library check once, the first time the Windows app talks to the engine."""
    if _desk_watch["started"]:
        return
    _desk_watch["started"] = True

    def loop():
        while True:
            _safe(desktop_sync)
            time.sleep(300)
    threading.Thread(target=loop, daemon=True).start()


async def collection_forget(req):
    """Take an opened mailbox file out of the Windows app's mail."""
    if host_kind(req) != "desktop":
        return err(PermissionError("Only the Windows app keeps opened files"), 403)
    path = (await req.json()).get("path", "")
    paths = _opened()
    if path not in paths:
        return err(ValueError("That file comes from a Library folder: remove the folder from the Library instead"))
    _save_opened([p for p in paths if p != path])
    _desktop_sync_soon()
    return JSONResponse({"ok": True})


async def status(req):
    """Index progress for every open mailbox, plus this user's jobs."""
    ses = session(req)
    c = _coll(req)
    return JSONResponse({"collection": c.status() if c and c.files else None,
                         "index": {s.id: (s.index.status() if s.index else None) for s in ses.sources.values()},
                         "jobs": [j.public() for j in JOBS.values() if j.user == req.state.user][-20:]})


async def close(req):
    s = session(req).sources.pop(req.path_params["sid"], None)
    if s:
        s.close()
    if s and s.path.startswith(UPLOAD_DIR):
        try:
            os.remove(s.path)
        except OSError:
            pass
    return JSONResponse({"ok": True})


async def folder(req):
    coll = None
    try:
        fid = int(req.path_params["fid"])
        if req.path_params["sid"] == "all":
            coll = _coll(req)
            if coll is None:
                raise KeyError("No collection")
            src, virtual, rows = None, True, coll.rows(fid)
        else:
            src = _src(req)
            virtual = src.folders[fid].kind == "invites"
            rows = src.invite_rows() if virtual else src.summaries_for(fid)
    except Exception as e:  # noqa: BLE001
        return err(e, 404)
    qp = req.query_params
    q = (qp.get("q") or "").strip()
    if q and coll is not None:
        terms = q.lower().split()
        rows = [r for r in rows if all(t.split(":")[-1] in " ".join(
            str(r.get(k, "")) for k in ("subject", "from", "to", "location", "folder")).lower() for t in terms)]
    elif q:
        if virtual:
            hits2 = set(src.search(q))
            rows = [r for r in rows if (r["fid"], r["idx"]) in hits2]
        else:
            hits = {i for f_id, i in src.search(q, fid)}
            rows = [r for r in rows if r["idx"] in hits]
    if qp.get("status"):
        rows = [r for r in rows if r["status"] in qp["status"].split(",")]
    if qp.get("state"):
        want = set(qp["state"].split(","))
        rows = [r for r in rows if want & set(r.get("states") or [])]
    if qp.get("category"):
        rows = [r for r in rows if qp["category"] in (r.get("categories") or [])]
    if qp.get("unread") == "1":
        rows = [r for r in rows if "Unread" in (r.get("states") or []) or r.get("unread")]
    sort = qp.get("sort", "date")
    desc = qp.get("desc", "1") == "1"
    keyf = (lambda r: r.get("size") or 0) if sort == "size" else (lambda r: str(r.get(sort) or "").lower())
    have = [r for r in rows if r.get(sort) not in (None, "")]
    rows = sorted(have, key=keyf, reverse=desc) + [r for r in rows if r.get(sort) in (None, "")]
    off, lim = int(qp.get("offset", 0)), int(qp.get("limit", 500))
    cats = sorted({c for r in (rows if virtual else src.summaries_for(fid)) for c in (r.get("categories") or [])})
    if coll is not None and fid == 0 and qp.get("q") is None and len(rows) > 20000 and lim > 5000:
        lim = 5000
    return JSONResponse({"total": len(rows), "items": rows[off:off + lim], "categories": cats})


def _message(req) -> tuple[Source, Message]:
    src = _src(req)
    return src, src.message(int(req.path_params["fid"]), int(req.path_params["idx"]))


async def message(req):
    try:
        _, m = _message(req)
    except Exception as e:  # noqa: BLE001
        return err(e, 404)
    props = m.extra.get("props") or {}
    return JSONResponse({
        "subject": m.subject, "from": str(m.sender), "to": "; ".join(map(str, m.to)),
        "cc": "; ".join(map(str, m.cc)), "bcc": "; ".join(map(str, m.bcc)),
        "date": m.date.isoformat() if m.date else None, "status": m.status, "problems": m.problems,
        "importance": m.importance, "format": m.source_format, "headers": m.headers,
        "message_id": m.message_id, "has_html": bool(m.body_html),
        "kind": m.kind, "states": m.states, "categories": m.categories,
        "item": [list(r) for r in item_rows(m)], "item_raw": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                                                              for k, v in m.item.items() if not k.startswith("_")},
        "archive_date": m.extra.get("archive_date"), "last_action_time": m.extra.get("last_action_time"),
        "attachments": [{"i": i, "name": a.filename, "type": a.mime_type, "size": a.size or len(a.data or b""),
                         "inline": a.inline, "embedded": a.embedded is not None,
                         "preview": preview_kind(a)}
                        for i, a in enumerate(m.attachments)],
        "props": {f"0x{k:04X}": (v if isinstance(v, (int, float, str, bool)) or v is None else
                                 (v.isoformat() if hasattr(v, "isoformat") else f"<{len(v)} bytes>"
                                  if isinstance(v, bytes) else str(v)))
                  for k, v in props.items() if isinstance(k, int)},
    })


async def body(req):
    try:
        _, m = _message(req)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<p>{e}</p>", 404)
    remote = req.query_params.get("remote") == "1"
    exc = req.query_params.get("exc")
    if exc is not None:                      # one changed occurrence of a recurring meeting
        msgs = m.extra.get("exception_msgs") or []
        if exc.isdigit() and int(exc) < len(msgs):
            m = msgs[int(exc)]
    css = ("<style>body{font-family:Calibri,'Segoe UI',Arial,sans-serif;font-size:14px;margin:12px;"
           "color:#222;word-wrap:break-word}img{max-width:100%}pre{white-space:pre-wrap}</style>")
    h = "<!DOCTYPE html><html><head><meta charset='utf-8'><base target='_blank'>" + css + \
        "</head><body>" + X.body_html(m, block_remote=not remote) + "</body></html>"
    csp = "default-src 'none'; img-src data: " + ("https: http:" if remote else "") + "; style-src 'unsafe-inline'"
    return HTMLResponse(h, headers={"Content-Security-Policy": csp})


async def attachment(req):
    try:
        _, m = _message(req)
        a = m.attachments[int(req.path_params["aid"])]
    except Exception as e:  # noqa: BLE001
        return err(e, 404)
    data = a.data
    name = a.filename
    if a.embedded is not None and not data:
        data, name = build_eml(a.embedded), name.rsplit(".", 1)[0] + ".eml"
    disp = "inline" if req.query_params.get("view") == "1" else "attachment"
    return Response(data, media_type=a.mime_type or "application/octet-stream",
                    headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{_q(name)}",
                             "Content-Security-Policy": "sandbox"})


async def open_attachment(req):
    """Open an attached mail/archive (embedded .msg, .eml, .zip...) as a new mailbox."""
    try:
        _, m = _message(req)
        a = m.attachments[int(req.path_params["aid"])]
        if a.embedded is not None:
            root = Folder(a.filename, kind="file")
            root.items.append(lambda e=a.embedded: e)
            return JSONResponse(_add(req, a.filename, root, ""))
        data = a.data
        if sniff(a.filename, data) == "unknown":
            return err(ValueError("Attachment is not a mail file or archive"))
        return JSONResponse(_add(req, a.filename, open_bytes(a.filename, data), ""))
    except Exception as e:  # noqa: BLE001
        return err(e)


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


async def export_one(req):
    try:
        _, m = _message(req)
        name, data = X.one(m, req.query_params.get("fmt", "pdf"))
    except Exception as e:  # noqa: BLE001
        return err(e)
    return Response(data, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_q(name)}"})


def _select(src: Source, b: dict) -> list:
    """(folder path, lazy loader) pairs for an export request."""
    scope = b.get("scope", "folder")
    statuses = set(b["statuses"]) if b.get("statuses") else None
    states = set(b["states"]) if b.get("states") else None
    fid = int(b.get("fid") or 0)
    if scope in ("selected", "folder"):
        f = src.folders[fid]
        idxs = [int(i) for i in b.get("items", [])] if scope == "selected" else range(len(f.items))
        pairs = [(fid, i) for i in idxs]
    else:
        top = src.root if scope == "all" else src.folders[fid]
        pairs = [(f_id, i) for f_id, f in src.folders.items() if _under(f, top) for i in range(len(f.items))]
    out = []
    for f_id, i in pairs:
        if statuses or states:
            r = src.summary_of(f_id, i)
            if statuses and r["status"] not in statuses:
                continue
            if states and not states & set(r.get("states") or []):
                continue
        path = "/".join(X.safe_name(p) for p in [src.root.name] + src.paths[f_id]) if scope != "selected" \
            else X.safe_name(src.folders[f_id].name)
        out.append((path, lambda f_id=f_id, i=i: src.message(f_id, i, cache=False)))
    return out


def _under(f: Folder, top: Folder) -> bool:
    return any(x is f for x in top.walk())


async def export_batch(req):
    """POST {sid, fid, fmt, scope, items, statuses, states} -> background job."""
    b = await req.json()
    src = session(req).sources[b["sid"]]
    fmt = b.get("fmt", "eml")
    items = _select(src, b)
    if not items:
        return err(ValueError("Nothing to export with these settings"))
    title = f"{X.FORMATS.get(fmt, fmt)} from {src.name}"

    def work(job: Job):
        if fmt in ("pst", "pst-ansi"):
            from ..core.pstwrite import write_pst
            tmp = job.temp_path(".pst")
            write_pst(tmp, items, src.root.name or src.name, progress=job.progress, cancelled=lambda: job.cancelled,
                      ansi=fmt == "pst-ansi")
            suffix = " - export (97-2002).pst" if fmt == "pst-ansi" else " - export.pst"
            job.finish_file(tmp, X.safe_name(os.path.splitext(src.name)[0] + suffix))
            return
        if len(items) == 1 and fmt not in ("csv", "json", "attachments", "pdf-combined", "mbox"):
            name, data = X.one(items[0][1](), fmt)
            job.write(name, data)
            return
        tmp = job.temp_path(".zip")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            X.export_many(items, fmt, z, progress=job.progress, cancelled=lambda: job.cancelled)
        job.finish_file(tmp, X.safe_name(f"{src.name} - {fmt}.zip"))

    job = start_job(req.state.user, title, len(items), work)
    return JSONResponse(job.public())


async def job_get(req):
    j = JOBS.get(req.path_params["jid"])
    if not j or j.user != req.state.user:
        return err(KeyError("No such job"), 404)
    return JSONResponse(j.public())


async def job_download(req):
    j = JOBS.get(req.path_params["jid"])
    if not j or j.user != req.state.user or j.state != "done":
        return err(KeyError("Export not ready"), 404)
    return FileResponse(j.result, filename=j.result_name, media_type=mimetypes.guess_type(j.result_name)[0]
                        or "application/octet-stream")


async def job_cancel(req):
    j = JOBS.get(req.path_params["jid"])
    if j and j.user == req.state.user:
        j.cancel()
    return JSONResponse({"ok": True})


async def deep_scan(req):
    src = _src(req)
    if src.deep_scanned:
        return err(ValueError("This file was already deep scanned"))
    from ..core.pst import PST
    pst = getattr(src.root, "pst", None)
    if not isinstance(pst, PST):
        return err(ValueError("Deep scan works on PST and OST files"))

    def work(job: Job):
        found = pst.deep_scan(progress=job.progress, cancelled=lambda: job.cancelled)
        f = Folder(f"Deep scan results ({len(found)})", kind="recovered")
        for m in found:
            f.items.append(lambda m=m: m)
        src.deep_scanned = True
        if found:
            src.add_folder(f)
        job.message = f"Found {len(found)} more items" if found else "No additional items found"

    job = start_job(req.state.user, f"Deep scan of {src.name}", 100, work)
    return JSONResponse(job.public())


def _health(src: Source) -> dict:
    stats: dict = {}
    states: dict = {}
    problems = []
    for fid, f in src.folders.items():
        for r in src.summaries_for(fid):
            stats[r["status"]] = stats.get(r["status"], 0) + 1
            for st in r.get("states") or []:
                states[st] = states.get(st, 0) + 1
            if r["status"] != "ok":
                m = src.message(fid, r["idx"])
                problems.append({"folder": " / ".join(src.paths[fid]) or src.root.name, "fid": fid, "idx": r["idx"],
                                 "subject": r["subject"], "status": r["status"], "problems": m.problems[:5]})
        if f.error:
            problems.append({"folder": f.name, "fid": fid, "idx": None, "subject": "(folder)",
                             "status": "error", "problems": [f.error]})
    return {"stats": stats, "states": states, "problems": problems[:2000], "deep_scanned": src.deep_scanned,
            "is_pst": hasattr(src.root, "pst")}


async def health(req):
    return JSONResponse(_health(_src(req)))


async def report(req):
    """Recovery report as HTML or PDF."""
    import html as H
    src = _src(req)
    h = _health(src)
    total = sum(h["stats"].values())
    size = os.path.getsize(src.path) if src.path and os.path.isfile(src.path) else 0
    rows = "".join(f"<tr><td>{H.escape(p['status'])}</td><td>{H.escape(p['folder'])}</td><td>{H.escape(p['subject'])}"
                   f"</td><td>{'<br>'.join(H.escape(x) for x in p['problems'])}</td></tr>" for p in h["problems"])
    def tiles(d):
        return "".join(f"<div class='t'><b>{v}</b>{H.escape(k)}</div>" for k, v in sorted(d.items(), key=lambda x: -x[1]))
    folders = "".join(f"<tr><td>{H.escape(' / '.join(src.paths[fid]) or src.root.name)}</td><td>{len(f.items)}</td>"
                      f"<td>{H.escape(f.error)}</td></tr>" for fid, f in src.folders.items() if f.items or f.error)
    html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>Recovery report</title><style>
    body{{font-family:Segoe UI,Arial,sans-serif;font-size:10pt;margin:28px;color:#222}}h1{{font-weight:400;font-size:20pt;margin:0}}
    h2{{font-weight:600;font-size:12pt;margin:22px 0 6px;color:#0f6cbd}}.t{{display:inline-block;background:#f3f3f3;padding:8px 14px;margin:0 6px 6px 0;min-width:80px}}
    .t b{{display:block;font-size:16pt;font-weight:400}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}}
    th{{background:#f3f3f3}}.mu{{color:#666}}</style></head><body>
    <h1>Recovery report</h1><p class='mu'>{H.escape(src.name)} · {size / 1048576:.1f} MB · {time.strftime('%B %d, %Y %I:%M %p')}
    · deep scan {'done' if h['deep_scanned'] else 'not run'}</p>
    <h2>Items by health</h2><div class='t'><b>{total}</b>Total</div>{tiles(h['stats'])}
    <h2>Items by saved state</h2>{tiles(h['states'])}
    <h2>Folders</h2><table><tr><th>Folder</th><th>Items</th><th>Errors</th></tr>{folders}</table>
    <h2>Problem items ({len(h['problems'])})</h2><table><tr><th>Status</th><th>Folder</th><th>Item</th><th>Details</th></tr>
    {rows or "<tr><td colspan=4>No problems found.</td></tr>"}</table></body></html>"""
    if req.query_params.get("fmt") == "html":
        return HTMLResponse(html)
    data = X.html_to_pdf(html)
    name = X.safe_name(f"{src.name} - recovery report.pdf")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_q(name)}"})


async def attachment_preview(req):
    _, m = _message(req)
    a = m.attachments[int(req.path_params["aid"])]
    return preview(a)


async def search_all(req):
    q = (req.query_params.get("q") or "").strip()
    out = []
    if q:
        for src in session(req).sources.values():
            for fid, idx in src.search(q)[:1000]:
                rows = src.summaries_for(fid)
                r = next((x for x in rows if x["idx"] == idx), None)
                if r:
                    out.append({**r, "sid": src.id, "fid": fid, "folder": src.folders[fid].name})
    return JSONResponse({"total": len(out), "items": out[:1000]})


async def duplicates(req):
    try:
        src = _src(req)
    except Exception as e:  # noqa: BLE001
        return err(e, 404)
    seen: dict = {}
    for fid in src.folders:
        for r in src.summaries_for(fid):
            m = src.message(fid, r["idx"])
            key = m.message_id or f"{r['subject']}|{r['from']}|{r['date']}"
            seen.setdefault(key, []).append({"fid": fid, "idx": r["idx"], "subject": r["subject"],
                                             "folder": src.folders[fid].name})
    return JSONResponse([v for v in seen.values() if len(v) > 1])


async def nc_open(req):
    """Open a Nextcloud file by id (ExApp mode): fetched server-side as that user."""
    user = getattr(req.state, "nc_user", "")
    if not exapp.ENABLED or not user:
        return err(PermissionError("Only available inside Nextcloud"), 403)
    path, name = exapp.fetch_user_file(user, req.query_params.get("fileId", ""), UPLOAD_DIR)
    return JSONResponse(_add(req, name, open_path(path, name), path))


def _wrap(fn):
    async def h(req):
        try:
            return await fn(req)
        except PermissionError as e:
            return err(e, 403)
        except (KeyError, FileNotFoundError) as e:
            return err(e.args[0] if e.args else e, 404)
        except ValueError as e:
            return err(e, 400)
        except Exception as e:  # noqa: BLE001
            return err(e, 500)
    return h


routes = [
    Route("/", index),
    Route("/favicon.ico", favicon),
    Route("/api/config", _wrap(config)),
    Route("/heartbeat", exapp.heartbeat),
    Route("/enabled", _wrap(exapp.enabled), methods=["PUT"]),
    Route("/nc/file-action", _wrap(exapp.file_action), methods=["POST"]),
    Route("/nc/open", _wrap(nc_open), methods=["POST"]),
    Route("/api/browse", _wrap(browse)),
    Route("/api/library", _wrap(library_list)),
    Route("/api/library", _wrap(library_add), methods=["POST"]),
    Route("/api/library/{lid}", _wrap(library_update), methods=["PATCH"]),
    Route("/api/library/{lid}", _wrap(library_remove), methods=["DELETE"]),
    Route("/api/library/{lid}/scan", _wrap(library_scan)),
    Route("/api/library/{lid}/open", _wrap(library_open), methods=["POST"]),
    Route("/api/open", _wrap(open_file), methods=["POST"]),
    Route("/api/upload", _wrap(upload), methods=["POST"]),
    Route("/api/sources", _wrap(sources)),
    Route("/api/collection", _wrap(collection_get)),
    Route("/api/collection/sync", _wrap(collection_sync), methods=["POST"]),
    Route("/api/collection/upload", _wrap(collection_upload), methods=["POST"]),
    Route("/api/collection/forget", _wrap(collection_forget), methods=["POST"]),
    Route("/api/sources/{sid}", _wrap(close), methods=["DELETE"]),
    Route("/api/status", _wrap(status)),
    Route("/api/sources/{sid}/health", _wrap(health)),
    Route("/api/sources/{sid}/report", _wrap(report)),
    Route("/api/sources/{sid}/deepscan", _wrap(deep_scan), methods=["POST"]),
    Route("/api/jobs/{jid}", _wrap(job_get)),
    Route("/api/jobs/{jid}", _wrap(job_cancel), methods=["DELETE"]),
    Route("/api/jobs/{jid}/download", _wrap(job_download)),
    Route("/api/sources/{sid}/duplicates", _wrap(duplicates)),
    Route("/api/search", _wrap(search_all)),
    Route("/api/sources/{sid}/folders/{fid}", _wrap(folder)),
    Route("/api/sources/{sid}/folders/{fid}/{idx}", _wrap(message)),
    Route("/api/sources/{sid}/folders/{fid}/{idx}/body", _wrap(body)),
    Route("/api/sources/{sid}/folders/{fid}/{idx}/export", _wrap(export_one)),
    Route("/api/sources/{sid}/folders/{fid}/{idx}/attachments/{aid}", _wrap(attachment)),
    Route("/api/sources/{sid}/folders/{fid}/{idx}/attachments/{aid}/open", _wrap(open_attachment), methods=["POST"]),
    Route("/api/sources/{sid}/folders/{fid}/{idx}/attachments/{aid}/preview", _wrap(attachment_preview)),
    Route("/api/export", _wrap(export_batch), methods=["POST"]),
    Mount("/static", StaticFiles(directory=WEB), name="static"),
    Route("/{kind:str}/{name:str}", exapp.static_asset),   # js/css/img for the Nextcloud ExApp page
]

app = Starlette(routes=routes, middleware=[Middleware(Auth)])


def _reopen_collections():
    """Collections survive restarts: reopen them (imports run in the background)."""
    for user in C.existing_users():
        try:
            C.get(user, _make_source)
        except Exception:  # noqa: BLE001
            pass


if os.environ.get("MAILVAULT_TOKEN") and not LOCK:
    threading.Thread(target=_reopen_collections, daemon=True).start()


def run(host="127.0.0.1", port=8080, roots=None):
    import uvicorn
    if exapp.ENABLED:   # AppAPI decides where we listen and where data persists
        host = os.environ.get("APP_HOST", "0.0.0.0")
        port = int(os.environ.get("APP_PORT", port))
    ROOTS[:] = [os.path.abspath(r) for r in (roots or [])] or \
        [p for p in os.environ.get("MAILVAULT_ROOTS", "").split(os.pathsep) if p]
    uvicorn.run(app, host=host, port=port, log_level="warning")
