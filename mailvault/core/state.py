"""Work out an item's saved state: sent/received/draft, read, replied/forwarded,
flagged, deleted, archived, junk, and categories, from whatever the source
format recorded (MAPI flags, Gmail labels, Thunderbird/mbox status headers,
Apple Mail flags) plus the folder it was found in."""
from __future__ import annotations

import re
from collections import Counter

ORDER = ["Draft", "Unsent", "Sent", "Received", "Unread", "Read", "Replied", "Replied all", "Forwarded",
         "Flagged", "Completed", "Starred", "Important", "Deleted", "Archived", "Junk"]

# ------------------------------------------------------------ MAPI (PST/MSG)
MSGFLAG_READ, MSGFLAG_UNSENT, MSGFLAG_FROMME, MSGFLAG_SUBMIT = 0x1, 0x8, 0x20, 0x4


def from_mapi(props: dict) -> tuple[list[str], dict]:
    s: list[str] = []
    info: dict = {}
    flags = props.get(0x0E07)
    if isinstance(flags, int):
        if flags & MSGFLAG_UNSENT:
            s.append("Draft")
        elif flags & MSGFLAG_FROMME:
            s.append("Sent")
        else:
            s.append("Received")
        s.append("Read" if flags & MSGFLAG_READ else "Unread")
        if flags & MSGFLAG_SUBMIT and flags & MSGFLAG_UNSENT:
            s.append("Unsent")
    verb = props.get(0x1081)
    icon = props.get(0x1080)
    if verb == 102 or icon == 0x105:
        s.append("Replied")
    elif verb == 103:
        s.append("Replied all")
    elif verb in (104, 105) or icon == 0x106:
        s.append("Forwarded")
    vt = props.get(0x1082)
    if vt is not None and hasattr(vt, "isoformat") and any(x in s for x in ("Replied", "Replied all", "Forwarded")):
        info["last_action_time"] = vt.isoformat()
    fs = props.get(0x1090)
    if fs == 2:
        s.append("Flagged")
    elif fs == 1:
        s.append("Completed")
    if props.get(0x301F) is not None or props.get(0x3018) is not None:
        s.append("Archived")
        if hasattr(props.get(0x301F), "isoformat"):
            info["archive_date"] = props[0x301F].isoformat()
    return s, info


# ------------------------------------------------------------ RFC 822 based
_GMAIL_MAP = {"sent": "Sent", "trash": "Deleted", "archived": "Archived", "unread": "Unread",
              "opened": "Read", "starred": "Starred", "important": "Important", "spam": "Junk",
              "drafts": "Draft", "draft": "Draft"}
_GMAIL_SYSTEM = {"inbox", "category personal", "category social", "category promotions", "category updates",
                 "category forums", "chat"}


_SPAM_TAG = re.compile(r"^\s*(\[spam\]|\*{2,3}\s*spam\s*\*{2,3}|\{spam\?\}|spam:)", re.I)


def is_spam(headers: str, subject: str = "") -> bool:
    """A spam verdict stamped by the receiving mail server (Exchange/Microsoft 365, SpamAssassin, Barracuda…)."""
    h = headers or ""
    if re.search(r"(?im)^X-Spam-Flag:\s*yes", h) or re.search(r"(?im)^X-Spam-Status:\s*yes", h)             or re.search(r"(?im)^X-Spam:\s*(yes|true|spam)", h) or re.search(r"(?im)^X-Barracuda-Spam-Status:\s*yes", h)             or re.search(r"(?im)^X-Spam-Level:\s*\*{5,}", h):
        return True
    for m in re.finditer(r"(?i)(?:X-MS-Exchange-Organization-SCL:\s*|\bSCL:)(-?\d+)", h):
        if int(m.group(1)) >= 5:
            return True
    return bool(_SPAM_TAG.search(subject or ""))


def from_headers(headers: str) -> tuple[list[str], list[str]]:
    """States and categories from Gmail, Thunderbird and classic mbox headers."""
    s: list[str] = []
    cats: list[str] = []
    h = headers or ""

    def get(name):
        m = re.search(rf"(?im)^{name}:[ \t]*(.*(?:\r?\n[ \t].*)*)", h)
        return re.sub(r"\r?\n[ \t]+", " ", m.group(1)).strip() if m else None

    labels = get("X-Gmail-Labels")
    if labels is not None:
        parts = [p.strip().strip('"') for p in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", labels) if p.strip()]
        low = [p.lower() for p in parts]
        for p, lo in zip(parts, low):
            if lo in _GMAIL_MAP:
                s.append(_GMAIL_MAP[lo])
            elif lo not in _GMAIL_SYSTEM:
                cats.append(p)
        if "sent" not in low and "drafts" not in low:
            s.append("Received")
        if "inbox" not in low and not {"sent", "trash", "spam", "drafts", "archived"} & set(low):
            s.append("Archived")   # Gmail: not in Inbox and not elsewhere = archived
        if "unread" not in low and "Read" not in s:
            s.append("Read")
    moz = get("X-Mozilla-Status")
    if moz:
        try:
            v = int(moz, 16)
            s += [x for bit, x in ((0x1, "Read"), (0x2, "Replied"), (0x4, "Flagged"), (0x8, "Deleted"),
                                   (0x1000, "Forwarded")) if v & bit]
            if not v & 0x1:
                s.append("Unread")
        except ValueError:
            pass
    st, xst = get("Status"), get("X-Status")
    if st is not None and not moz:
        s.append("Read" if "R" in st else "Unread")
    if xst:
        s += [x for c, x in (("A", "Replied"), ("F", "Flagged"), ("D", "Deleted"), ("T", "Draft")) if c in xst]
    kw = get("Keywords") or get("X-Keywords")
    if kw:
        cats += [k.strip() for k in re.split(r"[,\s]+", kw) if k.strip() and not k.startswith("$")]
    return s, cats


APPLE_FLAGS = ((1 << 0, "Read"), (1 << 1, "Deleted"), (1 << 2, "Replied"), (1 << 4, "Flagged"),
               (1 << 6, "Draft"), (1 << 8, "Forwarded"))


def from_emlx_flags(flags: int) -> list[str]:
    s = [x for bit, x in APPLE_FLAGS if flags & bit]
    if not flags & 1:
        s.append("Unread")
    return s


# --------------------------------------------------------- folder context
_DELETED = re.compile(r"\b(deleted items|deleted|trash|bin|recoverable items|deletions|purges|papierkorb|corbeille|papelera|eliminados|gelöschte)\b", re.I)
_ARCHIVE = re.compile(r"\b(archive|archives|archiv|archivo|archivio|all mail)\b", re.I)
_SENT = re.compile(r"\b(sent|sent items|sent mail|outbox|gesendet|envoy|enviados|inviata)\b", re.I)
_DRAFT = re.compile(r"\b(drafts?|entw[üu]rfe|brouillons|borradores)\b", re.I)
_JUNK = re.compile(r"\b(junk|spam|junk e-?mail|bulk)\b", re.I)


def finalize(intrinsic: list[str], folders: list[str], status: str, sender_email: str = "",
             owner: str = "", has_received_headers: bool = False, kind: str = "mail") -> list[str]:
    """Combine format flags with folder context and recovery status."""
    s = list(intrinsic)
    if kind != "mail":   # calendar/contact/task/note items have no sent/received/read meaning
        s = [x for x in s if x not in ("Sent", "Received", "Draft", "Read", "Unread", "Unsent")]
        path = " / ".join(folders)
        if _DELETED.search(path) or status == "recovered":
            s.append("Deleted")
        if _ARCHIVE.search(path):
            s.append("Archived")
        return sorted(dict.fromkeys(s), key=lambda x: ORDER.index(x) if x in ORDER else 99)
    path = " / ".join(folders)
    if _DELETED.search(path) or status == "recovered":
        s.append("Deleted")
    if _ARCHIVE.search(path):
        s.append("Archived")
    if _JUNK.search(path):
        s.append("Junk")
    if _DRAFT.search(path) and "Sent" not in s:
        s.append("Draft")
    direction = {"Sent", "Received", "Draft"} & set(s)
    if not direction:
        if _SENT.search(path) or (owner and sender_email and sender_email.lower() == owner.lower()):
            s.append("Sent")
        elif has_received_headers or folders:
            s.append("Received")
    elif "Received" in s and _SENT.search(path) and "Draft" not in s:
        s.remove("Received")
        s.append("Sent")
    if "Read" in s and "Unread" in s:
        s.remove("Read")
    out = []
    for x in s:
        if x not in out:
            out.append(x)
    return sorted(out, key=lambda x: ORDER.index(x) if x in ORDER else 99)


def guess_owner(sent_senders: list[str], received_to: list[str]) -> str:
    """The mailbox owner: most common sender in Sent folders, else most common recipient."""
    for pool in (sent_senders, received_to):
        c = Counter(a.lower() for a in pool if a and "@" in a)
        if c:
            return c.most_common(1)[0][0]
    return ""
