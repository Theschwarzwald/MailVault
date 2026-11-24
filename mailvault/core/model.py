"""Shared, format-independent mail model used by every reader and exporter."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

# Item status flags shown in the UI
OK = "ok"
RECOVERED = "recovered"   # found by carving / deleted-item recovery
ORPHANED = "orphaned"     # parent folder missing or unreachable
DAMAGED = "damaged"       # parsed partially, some data unreadable


@dataclass
class Address:
    name: str = ""
    email: str = ""

    def __str__(self) -> str:
        if self.name and self.email and self.name != self.email:
            return f"{self.name} <{self.email}>"
        return self.email or self.name


@dataclass
class Attachment:
    filename: str = "attachment.bin"
    mime_type: str = "application/octet-stream"
    size: int = 0
    content_id: str = ""
    inline: bool = False
    # Lazy loader keeps big archives cheap to browse
    loader: Optional[Callable[[], bytes]] = None
    _data: Optional[bytes] = None
    # An attached e-mail (message/rfc822 or embedded MSG)
    embedded: Optional["Message"] = None
    # Original MAPI attachment properties (PST/MSG), kept for lossless export
    props: dict = field(default_factory=dict)
    # Hidden attachment that stores one changed occurrence of a recurring meeting
    exception: bool = False

    @property
    def data(self) -> bytes:
        if self._data is None:
            self._data = self.loader() if self.loader else b""
        return self._data


@dataclass
class Message:
    subject: str = ""
    sender: Address = field(default_factory=Address)
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    bcc: list[Address] = field(default_factory=list)
    reply_to: list[Address] = field(default_factory=list)
    date: Optional[_dt.datetime] = None
    message_id: str = ""
    in_reply_to: str = ""
    headers: str = ""            # raw transport headers when known
    body_text: str = ""
    body_html: str = ""
    body_rtf: bytes = b""
    attachments: list[Attachment] = field(default_factory=list)
    importance: str = "normal"
    flags: set[str] = field(default_factory=set)   # read, flagged, draft...
    status: str = OK
    problems: list[str] = field(default_factory=list)
    source_format: str = ""
    # Raw RFC822 bytes when the source was EML/MBOX (lossless re-export)
    raw: Optional[bytes] = None
    extra: dict = field(default_factory=dict)       # format-specific props
    # mail | appointment | contact | task | note
    kind: str = "mail"
    # kind-specific fields (start/end/location, phones/emails, due/percent...)
    item: dict = field(default_factory=dict)
    # Saved state: Sent/Received/Draft, Read/Unread, Replied, Deleted, Archived...
    states: list = field(default_factory=list)
    categories: list = field(default_factory=list)

    @property
    def has_attachments(self) -> bool:
        return any(not a.inline for a in self.attachments)

    def summary(self) -> dict:
        return {
            "subject": self.subject or "(no subject)",
            "from": str(self.sender) or "(no sender)",
            "from_name": self.sender.name or self.sender.email or "(no sender)",
            "to": ", ".join(map(str, self.to)),
            "date": self.date.isoformat() if self.date else None,
            "size": len(self.raw) if self.raw else len(self.body_text) + len(self.body_html)
                    + sum(a.size for a in self.attachments),
            "attachments": self.has_attachments,
            "importance": self.importance,
            "status": self.status,
            "unread": "read" not in self.flags and self.source_format in ("pst", "msg"),
            "flagged": "flagged" in self.flags,
            "kind": self.kind,
            "states": list(self.states),
            "categories": list(self.categories),
            "sender_email": self.sender.email,
            "topic": normalize_topic(self.subject),
            **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in self.item.items()
               if k in SUMMARY_ITEM_KEYS},
        }


SUMMARY_ITEM_KEYS = {"start", "end", "location", "all_day", "company", "email", "phone", "job_title",
                     "due", "percent", "task_status", "complete", "color", "recurring", "busy", "recurrence",
                     "occurrences", "invite"}

_PREFIX = None


def normalize_topic(subject: str) -> str:
    """Conversation key: subject without Re:/Fwd:/AW:/SV: style prefixes."""
    global _PREFIX
    if _PREFIX is None:
        import re
        _PREFIX = re.compile(r"^\s*((\[\d+\]\s*)?(re|fw|fwd|aw|wg|sv|vs|tr|rif|antw|r|odp)\s*(\[\d+\])?\s*:\s*)+", re.I)
    return _PREFIX.sub("", subject or "").strip().lower()


VIEWS = {"IPF.Appointment": "calendar", "IPF.Contact": "contacts", "IPF.Task": "tasks",
         "IPF.StickyNote": "notes", "IPF.Journal": "journal"}


@dataclass
class Folder:
    name: str
    kind: str = "folder"          # folder, store, archive, recovered, orphaned
    children: list["Folder"] = field(default_factory=list)
    # Messages are produced lazily: list of zero-arg loaders
    items: list[Callable[[], Message]] = field(default_factory=list)
    count_hint: Optional[int] = None
    error: str = ""
    # mail | calendar | contacts | tasks | notes (from the PST container class)
    view: str = "mail"
    # Optional cheap summaries, parallel to items (None = parse the full item)
    quick: list = field(default_factory=list)

    def walk(self) -> Iterator["Folder"]:
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def total(self) -> int:
        return len(self.items)


def safe_load(loader: Callable[[], Message], label: str = "") -> Message:
    """Never let one broken item take down a folder listing."""
    try:
        return loader()
    except Exception as e:  # noqa: BLE001 - deliberate catch-all for damaged data
        m = Message(subject=label or "(unreadable item)", status=DAMAGED)
        m.problems.append(f"{type(e).__name__}: {e}")
        return m
