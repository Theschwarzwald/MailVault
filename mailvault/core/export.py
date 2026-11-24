"""Exporters: PDF, HTML, TXT, EML, MSG, MBOX, attachments, CSV and JSON."""
from __future__ import annotations

import base64
import csv
import html as _html
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Iterable

from .items import item_html, to_ics, to_vcf
from .mbox import write_mbox
from .mime import build_eml, html_to_text
from .model import Folder, Message, safe_load
from .msg import build_msg

FORMATS = {
    "pdf": "PDF document (one per message)",
    "pdf-combined": "Single PDF with every message",
    "html": "HTML page",
    "txt": "Plain text",
    "eml": "EML (RFC 822)",
    "msg": "Outlook MSG",
    "pst": "Outlook data file (.pst)",
    "pst-ansi": "Outlook 97-2002 data file (.pst, ANSI)",
    "mbox": "MBOX mailbox (single file)",
    "attachments": "Attachments only",
    "ics": "Calendar (.ics) for appointments and tasks",
    "vcf": "Contacts (.vcf vCard)",
    "csv": "CSV index (spreadsheet)",
    "json": "JSON (metadata and bodies)",
}

_SAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(s: str, limit: int = 120) -> str:
    s = _SAFE.sub("_", s or "").strip(" .") or "untitled"
    return s[:limit]


def base_name(m: Message) -> str:
    d = m.date.strftime("%Y-%m-%d %H%M") if m.date else "undated"
    return safe_name(f"{d} - {m.subject or '(no subject)'}")


# ------------------------------------------------------------------ HTML
def sanitize_html(h: str) -> str:
    h = re.sub(r"(?is)<script.*?</script>", "", h)
    h = re.sub(r"(?is)<(iframe|object|embed)[^>]*>.*?</\1>", "", h)
    h = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", h)
    h = re.sub(r"(?i)(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", r"\1=\2#\2", h)
    return h


def inline_cid(m: Message, h: str) -> str:
    for a in m.attachments:
        if a.content_id and a.mime_type.startswith("image/"):
            uri = f"data:{a.mime_type};base64,{base64.b64encode(a.data).decode()}"
            h = h.replace(f"cid:{a.content_id}", uri)
    return h


def body_html(m: Message, block_remote: bool = False) -> str:
    if m.body_html:
        h = inline_cid(m, sanitize_html(m.body_html))
        if block_remote:
            h = re.sub(r"(?i)(<img[^>]+src\s*=\s*[\"'])https?://", r"\1about:blank#", h)
        return h
    return f"<pre style='white-space:pre-wrap;font-family:inherit'>{_html.escape(m.body_text)}</pre>"


def render_html(m: Message, standalone: bool = True) -> str:
    e = _html.escape
    rows = [("From", str(m.sender)), ("Sent", m.date.strftime("%A, %B %d, %Y %I:%M %p") if m.date else ""),
            ("To", "; ".join(map(str, m.to))), ("Cc", "; ".join(map(str, m.cc))),
            ("Bcc", "; ".join(map(str, m.bcc))), ("Subject", m.subject)]
    if m.kind in ("contact", "note"):
        rows = [("Subject" if m.kind == "note" else "Name", m.subject)]
    elif m.kind in ("appointment", "task"):
        rows = [("Subject", m.subject)]
    head = "".join(f"<tr><th>{k}:</th><td>{e(v)}</td></tr>" for k, v in rows if v)
    atts = [a for a in m.attachments if not a.inline]
    if atts:
        head += "<tr><th>Attachments:</th><td>" + "; ".join(
            f"{e(a.filename)} ({_size(a.size or len(a.data))})" for a in atts) + "</td></tr>"
    note = ""
    if m.status != "ok":
        note = (f"<div class='mv-status'>Status: {e(m.status)}"
                + (": " + e("; ".join(m.problems[:3])) if m.problems else "") + "</div>")
    if m.kind != "mail":
        head += item_html(m).replace("<table class='mv-head'>", "").replace("</table>", "")
    inner = (f"<div class='mv-msg'>{note}<table class='mv-head'>{head}</table><hr>"
             f"<div class='mv-body'>{body_html(m)}</div></div>")
    if not standalone:
        return inner
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>" + e(m.subject) + "</title>"
            "<style>body{font-family:Calibri,'Segoe UI',Arial,sans-serif;font-size:11pt;margin:24px}"
            ".mv-head th{text-align:left;vertical-align:top;padding:1px 10px 1px 0;white-space:nowrap}"
            ".mv-head{border-collapse:collapse}.mv-status{background:#fff4ce;padding:6px 10px;"
            "margin-bottom:8px;border:1px solid #e0c060}.mv-msg{page-break-after:always}"
            "img{max-width:100%}</style></head><body>" + inner + "</body></html>")


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def render_text(m: Message) -> str:
    lines = [f"From: {m.sender}", f"Sent: {m.date or ''}", f"To: {'; '.join(map(str, m.to))}"]
    if m.cc:
        lines.append(f"Cc: {'; '.join(map(str, m.cc))}")
    lines += [f"Subject: {m.subject}"]
    if m.attachments:
        lines.append("Attachments: " + "; ".join(a.filename for a in m.attachments))
    body = m.body_text or html_to_text(m.body_html)
    return "\n".join(lines) + "\n\n" + body


# ------------------------------------------------------------------- PDF
def _browser() -> str | None:
    for b in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "msedge",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
        p = shutil.which(b) or (b if os.path.isfile(b) else None)
        if p:
            return p
    return None


def html_to_pdf(html: str) -> bytes:
    """Render HTML to PDF using an installed browser, wkhtmltopdf, or a text fallback."""
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "in.html"), os.path.join(td, "out.pdf")
        open(src, "w", encoding="utf-8").write(html)
        br = _browser()
        if br:
            try:
                subprocess.run([br, "--headless=new", "--disable-gpu", "--no-sandbox",
                                "--no-pdf-header-footer", f"--print-to-pdf={dst}", src],
                               capture_output=True, timeout=120)
                if os.path.exists(dst) and os.path.getsize(dst) > 0:
                    return open(dst, "rb").read()
            except (OSError, subprocess.TimeoutExpired):
                pass
        wk = shutil.which("wkhtmltopdf")
        if wk:
            try:
                subprocess.run([wk, "-q", "--encoding", "utf-8", src, dst], capture_output=True, timeout=120)
                if os.path.exists(dst) and os.path.getsize(dst) > 0:
                    return open(dst, "rb").read()
            except (OSError, subprocess.TimeoutExpired):
                pass
    return _text_pdf(html_to_text(html))


def _text_pdf(text: str) -> bytes:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        raise RuntimeError("No PDF engine: install Chrome/Edge, wkhtmltopdf, or reportlab")
    import textwrap
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 750
    for para in text.splitlines() or [""]:
        for line in textwrap.wrap(para, 95) or [""]:
            if y < 50:
                c.showPage()
                y = 750
            c.setFont("Helvetica", 10)
            c.drawString(50, y, line.encode("latin-1", "replace").decode("latin-1"))
            y -= 13
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------- batches
def one(m: Message, fmt: str) -> tuple[str, bytes]:
    """Export a single message, returning (filename, bytes)."""
    b = base_name(m)
    if fmt == "eml":
        return b + ".eml", build_eml(m)
    if fmt == "msg":
        return b + ".msg", build_msg(m)
    if fmt == "html":
        return b + ".html", render_html(m).encode("utf-8")
    if fmt == "txt":
        return b + ".txt", render_text(m).encode("utf-8")
    if fmt == "pdf":
        return b + ".pdf", html_to_pdf(render_html(m))
    if fmt == "mbox":
        buf = io.BytesIO()
        write_mbox(buf, [m])
        return b + ".mbox", buf.getvalue()
    if fmt in ("pst", "pst-ansi"):
        import tempfile
        from .pstwrite import write_pst
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "out.pst")
            write_pst(path, [("Imported", m)], m.subject or "Message", ansi=fmt == "pst-ansi")
            return b + ".pst", open(path, "rb").read()
    if fmt == "ics":
        return b + ".ics", to_ics([m])
    if fmt == "vcf":
        return safe_name(m.subject or "contact") + ".vcf", to_vcf([m])
    raise ValueError(f"Unsupported single-item format {fmt}")


def _unique(name: str, used: set) -> str:
    root, ext = os.path.splitext(name)
    n, cand = 1, name
    while cand.lower() in used:
        n += 1
        cand = f"{root} ({n}){ext}"
    used.add(cand.lower())
    return cand


def iter_folder(f: Folder, path: str = "", statuses: set | None = None):
    """Yield (folder_path, message) for every message under a folder tree."""
    here = os.path.join(path, safe_name(f.name)) if path or f.kind != "store" else safe_name(f.name)
    for it in f.items:
        m = safe_load(it)
        if statuses is None or m.status in statuses:
            yield here, m
    for c in f.children:
        yield from iter_folder(c, here, statuses)


class Cancelled(Exception):
    pass


def export_many(items: Iterable[tuple[str, Message]], fmt: str, out: zipfile.ZipFile,
                progress=None, cancelled=None) -> int:
    """Write messages into a zip, keeping their folder paths."""
    used: set = set()
    count = 0
    items = list(items)
    total = len(items)

    def load_all():
        """Resolve lazy loaders (callables) with progress, for whole-set formats."""
        out_items = []
        for i, (p, m) in enumerate(items):
            if cancelled and cancelled():
                raise Cancelled()
            out_items.append((p, m() if callable(m) else m))
            if progress:
                progress(i + 1, total)
        return out_items

    if fmt in ("pst", "pst-ansi"):
        import tempfile
        from .pstwrite import write_pst
        fd, tmp = tempfile.mkstemp(suffix=".pst")
        os.close(fd)
        try:
            n = write_pst(tmp, items, "MailVault export", progress, cancelled, ansi=fmt == "pst-ansi")
            out.write(tmp, "export.pst")
        finally:
            os.remove(tmp)
        return n
    if fmt not in ("pdf", "html", "txt", "eml", "msg", "attachments"):
        items = load_all()
    if fmt in ("ics", "vcf"):
        want = ("appointment", "task") if fmt == "ics" else ("contact",)
        by_folder: dict[str, list] = {}
        for p, m in items:
            if m.kind in want:
                by_folder.setdefault(p, []).append(m)
        for p, ms in by_folder.items():
            data = to_ics(ms) if fmt == "ics" else to_vcf(ms)
            out.writestr(_unique(f"{p or 'items'}.{fmt}", used), data)
            count += len(ms)
        if not count:
            raise ValueError("No " + ("calendar or task" if fmt == "ics" else "contact") + " items in this selection")
        return count
    if fmt == "mbox":
        by_folder: dict[str, list] = {}
        for p, m in items:
            by_folder.setdefault(p, []).append(m)
        for p, ms in by_folder.items():
            buf = io.BytesIO()
            write_mbox(buf, ms)
            out.writestr(_unique((p or "mailbox") + ".mbox", used), buf.getvalue())
            count += len(ms)
        return count
    if fmt == "pdf-combined":
        if progress:
            progress(0, 1)
        html = "<!DOCTYPE html><html><head><meta charset='utf-8'>" \
               "<style>body{font-family:Calibri,Arial,sans-serif;font-size:11pt}" \
               ".mv-head th{text-align:left;padding-right:10px}.mv-msg{page-break-after:always}" \
               "img{max-width:100%}</style></head><body>" + \
               "".join(render_html(m, False) for _, m in items) + "</body></html>"
        out.writestr("messages.pdf", html_to_pdf(html))
        return len(items)
    if fmt in ("csv", "json"):
        rows = []
        for p, m in items:
            rows.append({"folder": p, "date": m.date.isoformat() if m.date else "", "from": str(m.sender),
                         "to": "; ".join(map(str, m.to)), "cc": "; ".join(map(str, m.cc)),
                         "subject": m.subject, "attachments": "; ".join(a.filename for a in m.attachments),
                         "status": m.status, "problems": "; ".join(m.problems), "message_id": m.message_id,
                         **({"body": m.body_text or html_to_text(m.body_html)} if fmt == "json" else {})})
        if fmt == "json":
            out.writestr("messages.json", json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=list(rows[0].keys()) if rows else ["subject"])
            w.writeheader()
            w.writerows(rows)
            out.writestr("messages.csv", "\ufeff" + s.getvalue())
        return len(rows)
    for i, (p, m) in enumerate(items):
        if cancelled and cancelled():
            raise Cancelled()
        if callable(m):
            m = m()
        if fmt == "attachments":
            for a in m.attachments:
                data = build_eml(a.embedded) if a.embedded is not None and not a.data else a.data
                out.writestr(_unique(os.path.join(p, base_name(m), safe_name(a.filename)), used), data)
        else:
            name, data = one(m, fmt)
            out.writestr(_unique(os.path.join(p, name), used), data)
        count += 1
        if progress:
            progress(i + 1, len(items))
    return count
