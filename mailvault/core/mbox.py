"""MBOX reading (mboxo/mboxrd/mboxcl) with offset indexing and damage tolerance."""
from __future__ import annotations

import email.utils
import re
import time
from typing import BinaryIO, Iterable

from .mime import build_eml, parse_eml
from .model import DAMAGED, Folder, Message

_FROM_RE = re.compile(rb"^From \S*.*\d{4}\s*$|^From \S+ ")
_ESC_RE = re.compile(rb"^>(>*From )", re.M)


def index_mbox(data: bytes) -> list[tuple[int, int]]:
    """Return (start, end) byte ranges, one per message."""
    starts = []
    pos = 0
    n = len(data)
    prev_blank = True
    while pos < n:
        nl = data.find(b"\n", pos)
        line_end = n if nl < 0 else nl + 1
        line = data[pos:line_end]
        if prev_blank and line.startswith(b"From ") and _FROM_RE.match(line.rstrip(b"\r\n")):
            starts.append(pos)
        prev_blank = line.strip() == b""
        pos = line_end
    if not starts and bytes(data[:65536]).strip():
        # No separators: treat the whole file as a single damaged message
        return [(0, n)]
    ranges = [(s, starts[i + 1] if i + 1 < len(starts) else n) for i, s in enumerate(starts)]
    if starts and bytes(data[:starts[0]]).strip():
        ranges.insert(0, (0, starts[0]))  # junk/partial message before first separator
    return ranges


def _message_from_slice(chunk: bytes) -> Message:
    nl = chunk.find(b"\n")
    first = chunk[:nl] if nl >= 0 else b""
    body = chunk[nl + 1:] if first.startswith(b"From ") else chunk
    body = _ESC_RE.sub(rb"\1", body)
    m = parse_eml(body.rstrip(b"\r\n") + b"\n", source_format="mbox")
    if not first.startswith(b"From "):
        m.status = DAMAGED
        m.problems.append("Missing mbox separator line")
    if not m.headers.strip() or (":" not in m.headers.splitlines()[0] if m.headers else True):
        m.status = DAMAGED
        m.problems.append("Message headers missing or truncated")
    return m


def open_mbox(name: str, data: bytes) -> Folder:
    root = Folder(name=name, kind="store")
    for s, e in index_mbox(data):
        root.items.append(lambda s=s, e=e: _message_from_slice(data[s:e]))
    return root


def _from_line(m: Message) -> bytes:
    who = (m.sender.email or "MAILER-DAEMON").replace(" ", "_")
    ts = m.date.timetuple() if m.date else time.gmtime()
    return f"From {who} {time.strftime('%a %b %d %H:%M:%S %Y', ts)}\n".encode()


def write_mbox(out: BinaryIO, messages: Iterable[Message]) -> int:
    """Write mboxrd (the escaping variant that round-trips safely)."""
    count = 0
    for m in messages:
        raw = build_eml(m).replace(b"\r\n", b"\n")
        raw = re.sub(rb"(?m)^(>*From )", rb">\1", raw)
        out.write(_from_line(m))
        out.write(raw)
        if not raw.endswith(b"\n"):
            out.write(b"\n")
        out.write(b"\n")
        count += 1
    return count


__all__ = ["open_mbox", "write_mbox", "index_mbox", "email"]
