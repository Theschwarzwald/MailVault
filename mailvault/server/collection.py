"""The mail collection: every mail file in a user's mapped folders, imported and auto-sorted.

Like a music library: the Nextcloud app tells the engine which mail files sit in the
user's scan folders (with their on-disk path when the engine can read the file in
place, or by uploading it otherwise). The engine opens and indexes each file in the
background and lays the mail out like a mail program: one entry per email account
(yours, detected or listed), each with Inbox, Drafts, Sent Items, Deleted Items,
Archive, Junk Email and the user's own folders, plus Calendar, Contacts, Tasks and
Notes for the other modules. "More views" holds All mail, By date, People and Files.

Rows in these folders point at the real message (sid/fid/idx), so reading, exporting
and attachments work exactly as for an opened file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import zlib
from collections import Counter, defaultdict

from ..core.index import SCHEMA, fingerprint
from ..core.library import open_path

DATA = os.environ.get("MAILVAULT_COLLECTIONS") or os.path.join(
    os.path.dirname(os.environ.get("MAILVAULT_CACHE", "") or os.path.expanduser("~/.mailvault/cache")), "collections")
MAX_PEOPLE = 300

# the usual mailboxes: (mailbox, name shown, node kind, folder names that mean it)
_STD = [
    ("Inbox", "Inbox", "box-inbox", r"inbox|posteingang|bo[iî]te de r[ée]ception|bandeja de entrada|posta in arrivo|postvak in"),
    ("Drafts", "Drafts", "box-drafts", r"drafts?|entw[uü]rfe|brouillons?|borradores?|bozze|concepten"),
    ("Sent", "Sent Items", "box-sent", r"sent|sent items|sent mail|sent messages|outbox|postausgang|gesendet|"
     r"gesendete (?:elemente|objekte)|[ée]l[ée]ments envoy[ée]s|elementos enviados|posta inviata|verzonden(?: items)?"),
    ("Deleted", "Deleted Items", "box-deleted", r"deleted|deleted items|deleted messages|trash|bin|papierkorb|"
     r"gel[öo]schte (?:elemente|objekte)|[ée]l[ée]ments supprim[ée]s|elementos eliminados|posta eliminata|verwijderde items"),
    ("Archive", "Archive", "box-archive", r"archives?|archiv|archivo|archivio|archief"),
    ("Junk", "Junk Email", "box-junk", r"junk|junk e-?mail|junk mail|junk-e-mail|spam|bulk|bulk mail"),
]
STD = [(box, shown, kind) for box, shown, kind, _ in _STD]
STD_RX = [(box, re.compile(rf"^(?:{pat})$", re.I)) for box, _, _, pat in _STD]
ROOT_NAMES = re.compile(r"^(top of .*|root -.*|root|ipm_subtree|personal folders|outlook data file)$", re.I)
MODULE_NODES = [("calendar", "Calendar", "calendar", "calendar"), ("contact", "Contacts", "contacts", "contacts"),
                ("task", "Tasks", "tasks", "tasks"), ("note", "Notes", "notes", "notes")]
EMAIL_RX = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def _user_key(user: str) -> str:
    return hashlib.sha1(user.encode()).hexdigest()[:16]


class Node:
    __slots__ = ("id", "name", "kind", "view", "children", "refs", "path")

    def __init__(self, nid, name, kind="folder", view="mail"):
        self.id, self.name, self.kind, self.view = nid, name, kind, view
        self.children: list[Node] = []
        self.refs: list[dict] = []
        self.path = ""

    def tree(self, count=True) -> dict:
        unread = sum(1 for r in self.refs if r.get("unread") or "Unread" in (r.get("states") or []))             if self.kind.startswith("box-") or self.kind == "folder" else 0
        return {"id": self.id, "name": self.name, "kind": self.kind, "view": self.view,
                "count": len(self.refs) if count else 0, "unread": unread, "error": "",
                **({"path": self.path} if self.path else {}),
                "children": [c.tree() for c in self.children]}


class Collection:
    """One user's collection. Thread-safe; heavy work happens on a background thread."""

    def __init__(self, user: str, make_source):
        self.user = user
        self.make_source = make_source            # (sid, name, root, path, key) -> Source
        self.dir = os.path.join(DATA, _user_key(user))
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "user"), "w", encoding="utf-8") as fh:
            fh.write(user)
        self.lock = threading.RLock()
        self.files: dict[str, dict] = self._load()
        try:
            with open(os.path.join(self.dir, "accounts.json"), encoding="utf-8") as fh:
                self.accounts: list[str] = json.load(fh)
        except (OSError, ValueError):
            self.accounts = []
        self.sources: dict[str, object] = {}      # file id -> Source
        self.errors: dict[str, str] = {}
        self.root: Node | None = None
        self.nodes: dict[int, Node] = {}
        self.state = "idle"
        self.message = ""
        self.version = 0
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._wake.set()

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict:
        try:
            with open(os.path.join(self.dir, "files.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = os.path.join(self.dir, "files.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.files, fh)
        os.replace(tmp, os.path.join(self.dir, "files.json"))

    def upload_path(self, fid: str, name: str) -> str:
        d = os.path.join(self.dir, "files")
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r"[^\w.\- ]", "_", name)[:120] or "mail"
        return os.path.join(d, f"{re.sub(r'[^0-9A-Za-z_-]', '_', fid)}_{safe}")

    # ------------------------------------------------------------ sync from Nextcloud
    def set_accounts(self, accounts: list[str]):
        clean = sorted({a.strip().lower() for a in accounts if a and EMAIL_RX.fullmatch(a.strip())})
        with self.lock:
            if clean == self.accounts:
                return
            self.accounts = clean
            with open(os.path.join(self.dir, "accounts.json"), "w", encoding="utf-8") as fh:
                json.dump(clean, fh)
            self._resort = True
        self._wake.set()

    def sync(self, files: list[dict]) -> list[str]:
        """Replace the file list. Returns the ids the Nextcloud app must upload (not readable in place)."""
        need = []
        with self.lock:
            new = {}
            for f in files:
                fid = str(f.get("id") or "")
                if not fid:
                    continue
                old = self.files.get(fid, {})
                entry = {"name": str(f.get("name") or "mail"), "size": int(f.get("size") or 0),
                         "mtime": int(f.get("mtime") or 0), "folder": str(f.get("folder") or ""),
                         "where": str(f.get("where") or "")}
                path = f.get("path") or ""
                if path and os.path.isfile(path) and os.access(path, os.R_OK) and \
                        (not entry["size"] or os.path.getsize(path) == entry["size"]):
                    entry["path"] = path
                else:
                    up = old.get("path", "") if old.get("uploaded") else ""
                    if up and os.path.isfile(up) and old.get("mtime") == entry["mtime"] and old.get("size") == entry["size"]:
                        entry["path"], entry["uploaded"] = up, True
                    else:
                        need.append(fid)
                        entry["path"] = ""
                new[fid] = entry
            for fid, old in self.files.items():
                if fid not in new and old.get("uploaded") and old.get("path"):
                    try:
                        os.remove(old["path"])
                    except OSError:
                        pass
            changed = new != self.files
            self.files = new
            if changed:
                self._save()
        if changed or need:
            self._wake.set()                      # an unchanged list (the 5-minute rescan) costs nothing
        return need

    def store_upload(self, fid: str, tmp_path: str):
        with self.lock:
            f = self.files.get(fid)
            if f is None:
                os.remove(tmp_path)
                raise KeyError("That file is not in the collection")
            dst = self.upload_path(fid, f["name"])
            os.replace(tmp_path, dst)
            f["path"], f["uploaded"] = dst, True
            self._save()
        self._wake.set()

    # ------------------------------------------------------------ background import
    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            try:
                self._import()
            except Exception as e:  # noqa: BLE001 - keep the worker alive
                self.state, self.message = "error", str(e)

    # Loose single-message files are grouped per folder into one light mailbox that reads each
    # file only when needed; container files (PST, OST, MBOX, archives) get a mailbox each.
    LOOSE = (".msg", ".eml", ".emlx")

    def _units(self, files: dict) -> dict:
        units: dict[str, dict] = {}
        for fid, f in files.items():
            if not f.get("path"):
                continue
            if f["name"].lower().endswith(self.LOOSE):
                where = f.get("where") or ""
                gkey = "g:" + (f.get("folder") or "") + "/" + where
                u = units.setdefault(gkey, {"loose": True, "members": [], "where": where,
                                            "label": " / ".join(x for x in [f.get("folder") or "Mail", where] if x)})
                u["members"].append((fid, f))
            else:
                units["f:" + fid] = {"loose": False, "fid": fid, "f": f, "label": f["name"]}
        for u in units.values():
            if u["loose"]:
                u["members"].sort(key=lambda m: m[1]["name"].lower())
                u["sig"] = hashlib.sha1(json.dumps([(fid, f["mtime"], f["size"], f["path"]) for fid, f in u["members"]])
                                        .encode()).hexdigest()
            else:
                u["sig"] = f"{u['f']['mtime']}:{u['f']['size']}:{u['f']['path']}"
        return units

    def _open_unit(self, ukey: str, u: dict):
        from ..core.library import _single
        from ..core.model import Folder
        sid = "c" + hashlib.sha1((self.user + ukey).encode()).hexdigest()[:16]
        if not u["loose"]:
            f = u["f"]
            return self.make_source(sid, f["name"], open_path(f["path"], f["name"]), f["path"],
                                    fingerprint(f["path"], f["name"]))
        root = Folder(u["label"], kind="folder")
        for fid, f in u["members"]:
            fmt = f["name"].lower().rsplit(".", 1)[-1]

            def read(p=f["path"]):
                with open(p, "rb") as fh:
                    return fh.read()
            root.items.append(_single(f["name"], read, "eml" if fmt == "eml" else fmt).items[0])
        return self.make_source(sid, u["label"], root, "", f"coll-{SCHEMA}-" + u["sig"])

    def _import(self):
        with self.lock:
            files = dict(self.files)
        units = self._units(files)
        for ukey in [k for k in self.sources if k not in units or
                     getattr(self.sources[k], "_mv_sig", None) != units[k]["sig"]]:
            src = self.sources.pop(ukey)
            try:
                src.close()
            except Exception:  # noqa: BLE001
                pass
        todo = [(k, u) for k, u in units.items() if k not in self.sources]
        sig = sorted((k, u["sig"]) for k, u in units.items())
        if not todo and self.root is not None and sig == getattr(self, "_built_sig", None) and \
                not getattr(self, "_resort", False) and self.state == "ready":
            return                                # nothing new in the mapped folders
        self._resort = False
        last_build = time.time()
        for n, (ukey, u) in enumerate(todo, 1):
            self.state, self.message = "importing", f"Importing {u['label']} ({n} of {len(todo)})"
            try:
                src = self._open_unit(ukey, u)
                src._mv_sig = u["sig"]  # type: ignore[attr-defined]
                src._mv_label = u["label"]  # type: ignore[attr-defined]
                src._mv_where = u.get("where") if u["loose"] else None  # type: ignore[attr-defined]
                src._mv_nfiles = len(u["members"]) if u["loose"] else 1  # type: ignore[attr-defined]
                self.sources[ukey] = src
                self.errors.pop(ukey, None)
            except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
                self.errors[ukey] = f"{u['label']}: {e}"
            if time.time() - last_build > 20:      # let All mail fill in while a big import runs
                self._build()
                last_build = time.time()
        # wait for search indexes so sorting reads cached summaries instead of parsing everything twice
        deadline = time.time() + 1800
        while time.time() < deadline:
            building = [s for s in self.sources.values() if s.index and s.index.state == "building"]
            if not building:
                break
            done = sum(s.index.status().get("done", 0) for s in building)
            total = sum(s.index.status().get("total", 0) for s in building)
            self.state, self.message = "indexing", f"Reading {done} of {total} items" if total else "Indexing"
            ready_now = sum(1 for s in self.sources.values() if not s.index or s.index.state != "building")
            since = time.time() - last_build
            if (ready_now != getattr(self, "_ready_seen", -1) and since > 10) or since > 30:
                self._ready_seen = ready_now
                self._build()
                last_build = time.time()
            time.sleep(2)
        self.state, self.message = "sorting", "Sorting mail into folders"
        self._build()
        self._built_sig = sig
        self.state, self.message = "ready", ""

    # ------------------------------------------------------------ the sorted tree
    @staticmethod
    def _std_of(name: str) -> str | None:
        for box, rx in STD_RX:
            if rx.match(name.strip()):
                return box
        return None

    @classmethod
    def _place(cls, comps: list[str]) -> tuple[str | None, tuple]:
        """A folder path -> (usual mailbox or None, the sub-folder path under it / under the account)."""
        comps = [c.strip() for c in comps if c and c.strip()]
        while comps and ROOT_NAMES.match(comps[0]):
            comps.pop(0)
        at = max((i for i, c in enumerate(comps) if EMAIL_RX.search(c)), default=-1)
        comps = comps[at + 1:]                     # "me@work.com/Inbox/x" -> Inbox / x
        box, sub = None, comps
        for i, c in enumerate(comps):
            b = cls._std_of(c)
            if b:
                box, sub = b, comps[i + 1:]
        return box, tuple(sub)

    @staticmethod
    def _file_as(r: dict, box: str | None, sub: tuple, mine: set) -> tuple[str | None, tuple]:
        """Where a message is shown: the folder it was kept in, corrected by what the message itself says."""
        st = set(r.get("states") or [])
        if box in ("Drafts", "Junk", "Deleted"):
            return box, sub
        if "Draft" in st or "Unsent" in st:
            return "Drafts", ()
        if "Junk" in st:
            return "Junk", ()
        if "Deleted" in st or r.get("status") == "recovered":
            return "Deleted", ()
        sender = (r.get("sender_email") or "").lower()
        if not sub and box in (None, "Inbox", "Sent") and (r.get("kind") or "mail") == "mail" and \
                not (r.get("to") or "").strip() and "Received" not in st and (not sender or sender in mine):
            return "Drafts", ()                    # never addressed and never delivered
        if box == "Sent" or sub:
            return box, sub                        # your own folders keep their mail
        if "Sent" in st or (sender and sender in mine):
            return "Sent", ()
        if box:
            return box, ()
        return ("Archive" if "Archived" in st else "Inbox"), ()

    def _build(self):
        used: set[int] = {0}

        def nid(*parts) -> int:
            i = zlib.crc32("\x1f".join(parts).encode()) & 0x7FFFFFFF or 1
            while i in used:
                i = (i + 1) & 0x7FFFFFFF or 1
            used.add(i)
            return i

        root = Node(0, "All mail", "store")
        with self.lock:
            sources = list(self.sources.items())
        units = []        # (source, label, [(ref, box, sub)])
        for ukey, src in sorted(sources, key=lambda x: getattr(x[1], "_mv_label", x[1].name).lower()):
            partial = src.index is not None and src.index.state == "building"   # show what's read so far
            label = getattr(src, "_mv_label", src.name)
            where = getattr(src, "_mv_where", None)
            rows = []
            for f_id, folder in src.folders.items():
                if folder.kind == "invites" or not folder.items:
                    continue
                if where is not None:              # loose files: the Nextcloud folder they sit in
                    comps = where.split("/")
                else:
                    comps = list(src.paths.get(f_id, []))
                    if not comps:                  # "Sent.mbox"
                        stem = os.path.splitext(label)[0]
                        comps = [stem] if self._std_of(stem) else []
                box, sub = self._place(comps)
                try:
                    found = src.index.partial_summaries(f_id) if partial else src.summaries_for(f_id)
                except Exception:  # noqa: BLE001
                    continue
                for r in found:
                    ref = {**r, "sid": src.id, "fid": f_id,
                           "folder": " / ".join([label] + src.paths.get(f_id, []))}
                    rows.append((ref, box, sub))
            units.append((src, label, rows))

        mine, owners, path_owner = self._owners(units)
        everything: list[dict] = []
        places: dict[str, dict[tuple, list]] = defaultdict(lambda: defaultdict(list))   # account -> (box, sub) -> refs
        extra: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))      # account -> calendar/contacts/…
        years: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        people: dict[str, list] = defaultdict(list)
        people_name: dict[str, Counter] = defaultdict(Counter)
        attach: list[dict] = []
        file_nodes = []

        def addrs(text):
            return [a.lower() for a in EMAIL_RX.findall(text or "")]

        for src, label, rows in units:
            fnode = Node(nid("file", label), label, "file", "mail")
            if where is None and self.user == "desktop":
                fnode.path = src.path or ""             # a mailbox file: lets the Windows app remove it
            for ref, box, sub in rows:
                kind = ref.get("kind") or "mail"
                sender = (ref.get("sender_email") or "").lower()
                if id(src) in path_owner:
                    acct = path_owner[id(src)]
                elif kind in ("contact", "task", "note", "appointment") and not ref.get("invite"):
                    acct = owners.get(id(src)) or ""
                else:
                    acct = sender if sender in mine else next((a for a in addrs(ref.get("to")) if a in mine), "")
                    acct = acct or owners.get(id(src)) or ""
                acct = acct or "Unknown account"
                fnode.refs.append(ref)
                everything.append(ref)
                if kind in ("contact", "task", "note"):
                    extra[acct][kind].append(ref)
                    continue
                if ref.get("invite") or kind == "appointment":
                    extra[acct]["calendar"].append(ref)
                    if kind == "appointment" and not ref.get("invite"):
                        continue                   # calendar entries live only in the calendar
                fbox, fsub = self._file_as(ref, box, sub, mine)
                places[acct][(fbox, fsub)].append(ref)
                d = ref.get("date") or ""
                years[d[:4] or "Undated"][d[5:7] if len(d) >= 7 else ""].append(ref)
                if ref.get("attachments"):
                    attach.append(ref)
                if fbox == "Sent" or "Sent" in (ref.get("states") or []):
                    who = (ref.get("to") or "").split(",")[0].strip()
                    key = re.sub(r".*<([^>]+)>.*", r"\1", who).lower() or who.lower()
                    nm = re.sub(r"\s*<[^>]*>", "", who).strip(' "') or key
                else:
                    key = sender or (ref.get("from") or "").lower()
                    nm = ref.get("from_name") or key
                nm = (nm or "").strip()
                if nm.lower() in ("", "(no sender)", "undisclosed-recipients:;"):
                    nm = ""
                key = re.sub(r"\s+", " ", nm.lower()) if nm and "@" not in nm else key
                if key and key != "(no sender)":
                    people[key].append(ref)
                    people_name[key][nm or key] += 1
            file_nodes.append(fnode)

        # one entry per account, laid out like a mail program
        accts = set(places) | set(extra)
        order = sorted(accts, key=lambda a: (a == "Unknown account",
                                             -sum(len(v) for v in places[a].values()), a))
        for acct in order:
            an = Node(nid("acct", acct), acct, "account", "mail")
            mine_places = places[acct]
            for box, shown, kind in STD:
                own = mine_places.get((box, ()), [])
                subs = {s: v for (b, s), v in mine_places.items() if b == box and s}
                if not own and not subs and acct == "Unknown account":
                    continue
                bn = Node(nid("box", acct, box), shown, kind, "mail")
                bn.refs = own
                self._subfolders(bn, subs, nid, acct, box)
                an.children.append(bn)
            self._subfolders(an, {s: v for (b, s), v in mine_places.items() if b is None and s}, nid, acct, "")
            for k, shown, kind, view in MODULE_NODES:
                if extra[acct].get(k):
                    mn = Node(nid("mod", acct, k), shown, kind, view)
                    mn.refs = extra[acct][k]
                    an.children.append(mn)
            an.refs = [r for v in mine_places.values() for r in v]
            root.children.append(an)

        # other ways in, folded away under "More views"
        more = Node(nid("more"), "More views", "group", "mail")
        allm = Node(nid("more", "all"), "All mail", "allmail", "mail")
        allm.refs = [r for a in order for v in places[a].values() for r in v]
        more.children.append(allm)
        if attach:
            an = Node(nid("more", "attach"), "With attachments", "type-attach", "mail")
            an.refs = attach
            more.children.append(an)
        by_date = Node(nid("more", "date"), "By date", "group", "mail")
        for y in sorted([k for k in years if k != "Undated"], reverse=True) + (["Undated"] if "Undated" in years else []):
            yn = Node(nid("year", y), y, "year", "mail")
            for m in sorted(years[y], reverse=True):
                mname = MONTHS[int(m) - 1] if m.isdigit() and 1 <= int(m) <= 12 else "No month"
                mn = Node(nid("month", y, m), mname, "folder", "mail")
                mn.refs = years[y][m]
                yn.children.append(mn)
                yn.refs.extend(mn.refs)
            if len(yn.children) == 1 and yn.children[0].name == "No month":
                yn.refs, yn.children = yn.children[0].refs, []
            by_date.children.append(yn)
        by_date.refs = allm.refs
        by_person = Node(nid("more", "people"), "People", "group", "mail")
        ranked = sorted(people, key=lambda k: -len(people[k]))
        for key in ranked[:MAX_PEOPLE]:
            pn = Node(nid("person", key), people_name[key].most_common(1)[0][0], "person", "mail")
            pn.refs = people[key]
            by_person.children.append(pn)
        if len(ranked) > MAX_PEOPLE:
            rest = Node(nid("person", "*rest"), f"Everyone else ({len(ranked) - MAX_PEOPLE})", "person", "mail")
            for key in ranked[MAX_PEOPLE:]:
                rest.refs.extend(people[key])
            by_person.children.append(rest)
        by_person.children.sort(key=lambda n: (n.name.startswith("Everyone else"), n.name.lower()))
        by_person.refs = [r for n in by_person.children for r in n.refs]
        by_file = Node(nid("more", "files"), "Files", "group", "mail")
        by_file.children = file_nodes
        by_file.refs = everything
        more.children += [c for c in (by_date, by_person, by_file) if c.children]
        more.refs = allm.refs
        root.children.append(more)
        root.refs = everything

        nodes = {}
        stack = [root]
        while stack:
            n = stack.pop()
            nodes[n.id] = n
            stack.extend(n.children)
        with self.lock:
            self.root, self.nodes = root, nodes
            self.version += 1

    @staticmethod
    def _subfolders(parent: "Node", paths: dict, nid, acct: str, box: str):
        """Hang nested folders ("Projects/2024") under a mailbox or an account, sorted by name."""
        made = {(): parent}
        for path in sorted(paths, key=lambda p: [x.lower() for x in p]):
            for i in range(1, len(path) + 1):
                pre = path[:i]
                if pre not in made:
                    n = Node(nid("sub", acct, box, *pre), pre[-1], "folder", "mail")
                    made[pre[:-1]].children.append(n)
                    made[pre] = n
            made[path].refs = paths[path]

    def _owners(self, units):
        """Your addresses, and which of them each imported mailbox belongs to."""
        def addrs(text):
            return [a.lower() for a in EMAIL_RX.findall(text or "")]

        mine = set(self.accounts)
        # the address that sends the mail kept in Sent folders is yours
        sent_top = {}
        for src, _, rows in units:
            c = Counter((r.get("sender_email") or "").lower() for r, box, _ in rows if box == "Sent")
            c.pop("", None)
            if c:
                sent_top[id(src)] = c.most_common(1)[0][0]
        if not sent_top:                         # no Sent folders anywhere: fall back to messages marked sent
            for src, _, rows in units:
                c = Counter((r.get("sender_email") or "").lower() for r, _, _ in rows
                            if "Sent" in (r.get("states") or []))
                c.pop("", None)
                if c:
                    sent_top[id(src)] = c.most_common(1)[0][0]
        # a folder named after an address ("…/me@example.com/Sent") owns the mail inside it
        path_owner = {}
        for src, label, rows in units:
            found = EMAIL_RX.findall(label or "")
            if found:
                path_owner[id(src)] = found[-1].lower()
        # a whole PST/MBOX file is one mailbox: the address most of its mail was delivered to
        file_owner = {}
        for src, _, rows in units:
            if id(src) in sent_top or id(src) in path_owner or not rows or not src.path:   # containers only
                continue
            c = Counter(a for r, _, _ in rows for a in set(addrs(r.get("to"))))
            if c:
                top, n = c.most_common(1)[0]
                if n >= 0.3 * len(rows):
                    file_owner[id(src)] = top
        mine |= set(sent_top.values()) | set(path_owner.values()) | set(file_owner.values())
        if not mine:                             # still nothing: the address most mail was delivered to
            c = Counter(a for _, _, rows in units for r, _, _ in rows for a in addrs(r.get("to")))
            if c:
                mine.add(c.most_common(1)[0][0])
        overall = Counter(a for _, _, rows in units for r, _, _ in rows for a in addrs(r.get("to")) if a in mine)
        default = (overall.most_common(1) or [(next(iter(sorted(mine)), ""), 0)])[0][0]
        owners = {}
        for src, _, rows in units:
            for known in (path_owner, sent_top, file_owner):
                if id(src) in known:
                    owners[id(src)] = known[id(src)]
                    break
            else:
                c = Counter(a for r, _, _ in rows for a in addrs(r.get("to")) if a in mine)
                owners[id(src)] = (c.most_common(1) or [(default, 0)])[0][0]
        return mine, owners, path_owner

    def account_list(self) -> list[str]:
        with self.lock:
            if not self.root:
                return []
            return [n.name for n in self.root.children if n.kind == "account" and n.name != "Unknown account"]

    # ------------------------------------------------------------ queries
    def status(self) -> dict:
        with self.lock:
            return {"files": len(self.files), "imported": sum(getattr(s, "_mv_nfiles", 1) for s in self.sources.values()),
                    "waiting_upload": sum(1 for f in self.files.values() if not f.get("path")),
                    "errors": [{"error": v} for v in self.errors.values()],
                    "messages": len(self.root.refs) if self.root else 0, "state": self.state, "message": self.message,
                    "version": self.version}

    def tree(self) -> dict | None:
        with self.lock:
            if self.root is None:
                return None
            return self.root.tree()

    def rows(self, nid: int) -> list[dict]:
        with self.lock:
            n = self.nodes.get(nid)
            return list(n.refs) if n else []

    def source(self, sid: str):
        with self.lock:
            for s in self.sources.values():
                if s.id == sid:
                    return s
        return None

    def trigger(self):
        self._wake.set()


COLLECTIONS: dict[str, Collection] = {}
_clock = threading.Lock()


def get(user: str, make_source, create: bool = True) -> Collection | None:
    with _clock:
        c = COLLECTIONS.get(user)
        if c is None and create:
            c = COLLECTIONS[user] = Collection(user, make_source)
        return c


def existing_users() -> list[str]:
    """Users with a saved collection (reopened at engine start-up)."""
    out = []
    try:
        for d in os.listdir(DATA):
            p = os.path.join(DATA, d, "user")
            if os.path.isfile(p):
                out.append(open(p, encoding="utf-8").read().strip())
    except OSError:
        pass
    return out
