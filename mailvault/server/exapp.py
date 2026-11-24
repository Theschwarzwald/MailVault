"""Nextcloud AppAPI ("ExApp") integration.

When AppAPI deploys the MailVault container it passes APP_ID, APP_SECRET,
APP_PORT, NEXTCLOUD_URL, AA_VERSION... This module then:
  * answers /heartbeat and /enabled (the AppAPI lifecycle),
  * registers a top-menu page, its script and style, and a Files action
    ("Open in MailVault") through AppAPI's OCS API,
  * authenticates every proxied request from the AUTHORIZATION-APP-API header,
  * fetches the user's file from Nextcloud over WebDAV (as that user) when the
    Files action is used, so huge PSTs never pass through the browser.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import urllib.parse
import urllib.request

from starlette.responses import FileResponse, JSONResponse, Response

from .. import __version__

APP_ID = os.environ.get("APP_ID", "mailvault")
APP_SECRET = os.environ.get("APP_SECRET", "")
APP_VERSION = os.environ.get("APP_VERSION", __version__)
AA_VERSION = os.environ.get("AA_VERSION", "3.0.0")
NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL", "").rstrip("/")
ENABLED = bool(APP_SECRET)
WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
MIME_TYPES = ",".join([
    "application/vnd.ms-outlook", "application/vnd.ms-outlook-pst", "application/vnd.ms-outlook-ost",
    "message/rfc822", "application/mbox", "application/x-mbox", "application/octet-stream",
])


def auth_user(headers) -> str | None:
    """Validate AppAPI auth; returns the Nextcloud user id ('' for system calls) or None."""
    h = headers.get("authorization-app-api", "")
    if not h or not APP_SECRET:
        return None
    try:
        user, _, secret = base64.b64decode(h).decode().partition(":")
    except Exception:  # noqa: BLE001
        return None
    return user if hmac.compare_digest(secret, APP_SECRET) else None


def _headers(user: str = "") -> dict:
    return {
        "AA-VERSION": AA_VERSION, "EX-APP-ID": APP_ID, "EX-APP-VERSION": APP_VERSION,
        "AUTHORIZATION-APP-API": base64.b64encode(f"{user}:{APP_SECRET}".encode()).decode(),
        "OCS-APIRequest": "true",
    }


def ocs(method: str, path: str, payload: dict | None = None, user: str = ""):
    url = f"{NEXTCLOUD_URL}/ocs/v1.php/apps/app_api/{path.lstrip('/')}?format=json"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**_headers(user), "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def register_ui():
    ocs("POST", "api/v1/ui/top-menu", {"name": "main", "displayName": "MailVault", "icon": "img/app.svg",
                                       "adminRequired": 0})
    ocs("POST", "api/v1/ui/script", {"type": "top_menu", "name": "main", "path": "js/exapp"})
    ocs("POST", "api/v1/ui/style", {"type": "top_menu", "name": "main", "path": "css/app"})
    ocs("POST", "api/v2/ui/files-actions-menu", {
        "name": "mailvault_open", "displayName": "Open in MailVault", "actionHandler": "nc/file-action",
        "mime": MIME_TYPES, "icon": "img/app.svg", "order": 0})


def unregister_ui():
    for path, payload in (("api/v1/ui/top-menu", {"name": "main"}),
                          ("api/v2/ui/files-actions-menu", {"name": "mailvault_open"})):
        try:
            ocs("DELETE", path, payload)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------ handlers
async def heartbeat(req):
    return JSONResponse({"status": "ok"})


async def enabled(req):
    if auth_user(req.headers) is None:
        return Response("Unauthorized", 401)
    on = req.query_params.get("enabled") in ("1", "true")
    try:
        register_ui() if on else unregister_ui()
        return JSONResponse({"error": ""})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"MailVault could not register its UI: {e}"})


async def file_action(req):
    """Files action handler: AppAPI forwards the selection; we send the user to our page."""
    return JSONResponse({"redirect_handler": "open"})


async def static_asset(req):
    kind, name = req.path_params["kind"], os.path.basename(req.path_params["name"])
    if kind not in ("js", "css", "img"):
        return Response("Not found", 404)
    if kind == "img" and name == "app.svg":
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><rect x='2' y='4.5' width='20' height='15' "
               "rx='2' fill='none' stroke='#fff' stroke-width='2'/><path d='M3 7l9 6 9-6' fill='none' stroke='#fff' "
               "stroke-width='2'/></svg>")
        return Response(svg, media_type="image/svg+xml")
    path = os.path.join(WEB, name if name.endswith((".js", ".css")) else name + (".js" if kind == "js" else ".css"))
    if not os.path.isfile(path):
        return Response("Not found", 404)
    return FileResponse(path)


def _dav_find(user: str, file_id: str) -> str:
    """Resolve a Nextcloud file id to its WebDAV href via a DAV SEARCH."""
    uq = urllib.parse.quote(user)
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:basicsearch>
<d:select><d:prop><d:displayname/><oc:fileid/></d:prop></d:select>
<d:from><d:scope><d:href>/files/{uq}</d:href><d:depth>infinity</d:depth></d:scope></d:from>
<d:where><d:eq><d:prop><oc:fileid/></d:prop><d:literal>{int(file_id)}</d:literal></d:eq></d:where>
<d:orderby/></d:basicsearch></d:searchrequest>"""
    req = urllib.request.Request(f"{NEXTCLOUD_URL}/remote.php/dav/", data=body.encode(), method="SEARCH",
                                 headers={**_headers(user), "Content-Type": "text/xml"})
    with urllib.request.urlopen(req, timeout=60) as r:
        xml = r.read().decode("utf-8", "replace")
    m = re.search(r"<d:href>([^<]+)</d:href>", xml)
    if not m:
        raise FileNotFoundError("File not found in your Nextcloud")
    return m.group(1)


def fetch_user_file(user: str, file_id: str, dest_dir: str) -> tuple[str, str]:
    href = _dav_find(user, file_id)
    name = urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])
    dst = os.path.join(dest_dir, secrets.token_hex(8) + "_" + re.sub(r'[\\/:*?"<>|]', "_", name))
    req = urllib.request.Request(NEXTCLOUD_URL + href, headers=_headers(user))
    with urllib.request.urlopen(req, timeout=3600) as r, open(dst, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    return dst, name


# ------------------------------------------------------------ library support
_groups_cache: dict[str, tuple[float, list]] = {}
_scan_cache: dict[tuple, tuple[float, dict]] = {}


def user_groups(user: str) -> list[str]:
    """The user's Nextcloud groups (admins are in 'admin'), cached for five minutes."""
    import time
    if not user or not ENABLED:
        return []
    hit = _groups_cache.get(user)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    groups: list[str] = []
    try:
        url = f"{NEXTCLOUD_URL}/ocs/v2.php/cloud/users/{urllib.parse.quote(user)}/groups?format=json"
        req = urllib.request.Request(url, headers={**_headers(user), "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            groups = json.loads(r.read()).get("ocs", {}).get("data", {}).get("groups", []) or []
    except Exception:  # noqa: BLE001 - no groups means "regular user"
        groups = []
    _groups_cache[user] = (time.time(), groups)
    return groups


def dav_scan(user: str, folder: str, refresh: bool = False) -> dict:
    """Mail files under a folder of the user's Nextcloud, via one WebDAV SEARCH."""
    import time
    from .library import MAX_SCAN, SCAN_EXT
    key = (user, folder)
    hit = _scan_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < 60:
        return hit[1]
    uq = urllib.parse.quote(user)
    scope = f"/files/{uq}/" + "/".join(urllib.parse.quote(p) for p in folder.strip("/").split("/") if p)
    likes = "".join(f"<d:like><d:prop><d:displayname/></d:prop><d:literal>%{e}</d:literal></d:like>" for e in SCAN_EXT)
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:basicsearch>
<d:select><d:prop><d:displayname/><oc:fileid/><d:getcontentlength/><d:getlastmodified/></d:prop></d:select>
<d:from><d:scope><d:href>{scope}</d:href><d:depth>infinity</d:depth></d:scope></d:from>
<d:where><d:or>{likes}</d:or></d:where><d:orderby/>
<d:limit><d:nresults>{MAX_SCAN}</d:nresults></d:limit></d:basicsearch></d:searchrequest>"""
    req = urllib.request.Request(f"{NEXTCLOUD_URL}/remote.php/dav/", data=body.encode(), method="SEARCH",
                                 headers={**_headers(user), "Content-Type": "text/xml"})
    with urllib.request.urlopen(req, timeout=120) as r:
        xml = r.read().decode("utf-8", "replace")
    root: dict = {"name": folder.strip("/").split("/")[-1] or "Files", "dirs": {}, "files": []}
    base = f"/remote.php/dav/files/{uq}/" + "/".join(urllib.parse.quote(p) for p in folder.strip("/").split("/") if p)
    count = 0
    for resp in re.findall(r"<d:response>(.*?)</d:response>", xml, re.S):
        href = re.search(r"<d:href>([^<]+)</d:href>", resp)
        fid = re.search(r"<oc:fileid>(\d+)</oc:fileid>", resp)
        if not href or not fid or href.group(1).endswith("/"):
            continue
        rel = urllib.parse.unquote(href.group(1)[len(base):].lstrip("/")) if href.group(1).startswith(base) else \
            urllib.parse.unquote(href.group(1)).rsplit("/", 1)[-1]
        size = re.search(r"<d:getcontentlength>(\d+)<", resp)
        parts = rel.split("/")
        node = root
        for part in parts[:-1]:
            node = node["dirs"].setdefault(part, {"name": part, "dirs": {}, "files": []})
        node["files"].append({"name": parts[-1], "fileId": int(fid.group(1)), "size": int(size.group(1)) if size else 0})
        count += 1

    def finish(n):
        return {"name": n["name"], "dirs": [finish(c) for c in sorted(n["dirs"].values(), key=lambda x: x["name"].lower())],
                "files": sorted(n["files"], key=lambda f: f["name"].lower())}
    out = {**finish(root), "count": count, "truncated": count >= MAX_SCAN}
    _scan_cache[key] = (time.time(), out)
    return out
