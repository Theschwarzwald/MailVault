"""Library folders: folders MailVault keeps an eye on and lists in the folder pane.

Kinds
  server     a folder on the machine running the engine (Docker host mount, NAS
             path, a local folder on the desktop). Admin-managed in Nextcloud,
             with visibility for everyone, admins only, or named users/groups.
  nextcloud  a folder in the signed-in user's own Nextcloud Files (ExApp mode;
             the PHP app serves these itself).

Config lives in <data dir>/library.json, so folders survive restarts.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time

from ..core.index import CACHE_DIR
from ..core.library import ARCHIVE_EXT, MAIL_EXT

LIB_FILE = os.environ.get("MAILVAULT_LIBRARY_FILE") or os.path.join(CACHE_DIR, "library.json")
# Where server folders may live (path prefixes, os.pathsep separated). Default: anywhere.
ALLOW = [os.path.realpath(p) for p in os.environ.get("MAILVAULT_LIBRARY_ALLOW", os.sep).split(os.pathsep) if p]
SCAN_EXT = tuple(e for e in MAIL_EXT if e != ".txt") + ARCHIVE_EXT
MAX_SCAN = 500000
_lock = threading.Lock()
_scan_cache: dict = {}


def _load() -> dict:
    try:
        with open(LIB_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {}
    d.setdefault("server", [])
    d.setdefault("users", {})
    for p in [p for p in os.environ.get("MAILVAULT_ROOTS", "").split(os.pathsep) if p]:
        if not any(f["path"] == os.path.abspath(p) for f in d["server"]):     # folders mounted at startup
            d["server"].append({"id": "env-" + str(abs(hash(p)) % 10 ** 8), "path": os.path.abspath(p),
                                "label": os.path.basename(p.rstrip("/\\")) or p, "scope": {"mode": "everyone"},
                                "fixed": True})
    return d


def _save(d: dict):
    os.makedirs(os.path.dirname(LIB_FILE) or ".", exist_ok=True)
    tmp = LIB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"server": [f for f in d["server"] if not f.get("fixed")], "users": d["users"]}, fh, indent=2)
    os.replace(tmp, LIB_FILE)


def allowed_base(path: str) -> bool:
    rp = os.path.realpath(path)
    return any(rp == a or rp.startswith(a.rstrip(os.sep) + os.sep) for a in ALLOW)


class Who:
    """The caller, as far as library permissions are concerned."""

    def __init__(self, user: str, admin: bool, groups: list[str], can_edit: bool):
        self.user, self.admin, self.groups, self.can_edit = user, admin, groups, can_edit

    def sees(self, folder: dict) -> bool:
        sc = folder.get("scope") or {"mode": "everyone"}
        mode = sc.get("mode", "everyone")
        if mode == "everyone" or self.admin:
            return True
        if mode == "admins":
            return False
        names = {x.strip().lower() for x in sc.get("who", []) if x.strip()}
        short = self.user.split(":", 1)[-1].lower()
        return short in names or bool(names & {g.lower() for g in self.groups})


def list_folders(who: Who) -> list[dict]:
    d = _load()
    out = [{**f, "kind": "server", "editable": who.can_edit and not f.get("fixed"),
            "exists": os.path.isdir(f["path"])} for f in d["server"] if who.sees(f)]
    for f in d["users"].get(who.user, []):
        out.append({**f, "kind": f.get("kind", "server"), "editable": True,
                    "exists": True if f.get("kind") == "nextcloud" else os.path.isdir(f["path"])})
    if not who.admin:
        for f in out:
            if f["kind"] == "server" and not f["editable"]:
                f.pop("scope", None)       # don't reveal who else can see it
    return out


def add_folder(who: Who, path: str, label: str = "", scope: dict | None = None, personal: bool = False,
               kind: str = "server") -> dict:
    with _lock:
        d = _load()
        if kind == "server":
            path = os.path.abspath(os.path.expanduser(path))
            if not os.path.isdir(path):
                raise FileNotFoundError(f"Folder not found on the server: {path}")
            if not allowed_base(path):
                raise PermissionError("That folder is outside the locations MailVault may read "
                                      "(MAILVAULT_LIBRARY_ALLOW)")
        f = {"id": secrets.token_hex(5), "path": path, "label": label or os.path.basename(path.rstrip("/\\")) or path}
        if kind == "nextcloud" or personal:
            f["kind"] = kind
            d["users"].setdefault(who.user, []).append(f)
        else:
            if not who.can_edit:
                raise PermissionError("Only an administrator can add server folders")
            mode = (scope or {}).get("mode", "everyone")
            if mode not in ("everyone", "admins", "list"):
                mode = "everyone"
            f["scope"] = {"mode": mode, "who": [w.strip() for w in (scope or {}).get("who", []) if w.strip()]}
            d["server"].append(f)
        _save(d)
        return f


def update_folder(who: Who, fid: str, label: str | None = None, scope: dict | None = None) -> dict:
    with _lock:
        d = _load()
        for f in d["server"]:
            if f["id"] == fid and not f.get("fixed"):
                if not who.can_edit:
                    raise PermissionError("Only an administrator can change server folders")
                if label:
                    f["label"] = label
                if scope:
                    f["scope"] = {"mode": scope.get("mode", "everyone"), "who": scope.get("who", [])}
                _save(d)
                return f
        for f in d["users"].get(who.user, []):
            if f["id"] == fid:
                if label:
                    f["label"] = label
                _save(d)
                return f
    raise KeyError("Folder not found")


def remove_folder(who: Who, fid: str):
    with _lock:
        d = _load()
        mine = d["users"].get(who.user, [])
        if any(f["id"] == fid for f in mine):
            d["users"][who.user] = [f for f in mine if f["id"] != fid]
        elif any(f["id"] == fid and not f.get("fixed") for f in d["server"]):
            if not who.can_edit:
                raise PermissionError("Only an administrator can remove server folders")
            d["server"] = [f for f in d["server"] if f["id"] != fid]
        else:
            raise KeyError("Folder not found")
        _save(d)


def find(who: Who, fid: str) -> dict:
    for f in list_folders(who):
        if f["id"] == fid:
            return f
    raise KeyError("Folder not found or not shared with you")


def visible_roots(who: Who) -> list[str]:
    return [f["path"] for f in list_folders(who) if f["kind"] == "server"]


def scan(folder_path: str, refresh: bool = False) -> dict:
    """Mail files under a folder, as a tree. Cached for a minute."""
    now = time.time()
    hit = _scan_cache.get(folder_path)
    if hit and not refresh and now - hit[0] < 60:
        return hit[1]
    root: dict = {"name": os.path.basename(folder_path.rstrip("/\\")) or folder_path, "dirs": {}, "files": []}
    count, truncated = 0, False
    for dirpath, dirnames, filenames in os.walk(folder_path, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d.lower() not in ("@eadir", "#recycle"))
        rel = os.path.relpath(dirpath, folder_path)
        node = root
        if rel != ".":
            for part in rel.split(os.sep):
                node = node["dirs"].setdefault(part, {"name": part, "dirs": {}, "files": []})
        for fn in sorted(filenames):
            low = fn.lower()
            if fn.startswith(".") or not low.endswith(SCAN_EXT):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            node["files"].append({"name": fn, "path": full, "size": st.st_size, "mtime": int(st.st_mtime)})
            count += 1
            if count >= MAX_SCAN:
                truncated = True
                break
        if truncated:
            break

    def finish(n):
        dirs = [finish(c) for c in n["dirs"].values()]
        dirs = [x for x in dirs if x["files"] or x["dirs"]]
        return {"name": n["name"], "dirs": dirs, "files": n["files"]}
    out = {**finish(root), "count": count, "truncated": truncated}
    _scan_cache[folder_path] = (now, out)
    return out
