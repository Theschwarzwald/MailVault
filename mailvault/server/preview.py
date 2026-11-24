"""Attachment previews: images, PDF, text, e-mail, and Office files (via LibreOffice when installed)."""
from __future__ import annotations

import html as H
import mimetypes
import os
import shutil
import subprocess
import tempfile

from starlette.responses import HTMLResponse, Response

from ..core import export as X
from ..core.model import Attachment

TEXT_EXT = (".txt", ".csv", ".log", ".json", ".xml", ".md", ".ini", ".cfg", ".yaml", ".yml", ".ics", ".vcf",
            ".py", ".js", ".css", ".sql", ".sh", ".bat", ".ps1")
OFFICE_EXT = (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".pps", ".ppsx")
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/svg+xml")


def _soffice() -> str | None:
    for b in ("soffice", "libreoffice", r"C:\Program Files\LibreOffice\program\soffice.exe",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        p = shutil.which(b) or (b if os.path.isfile(b) else None)
        if p:
            return p
    return None


def _mime(a: Attachment) -> str:
    t = (a.mime_type or "").lower()
    if not t or t == "application/octet-stream":
        t = mimetypes.guess_type(a.filename)[0] or t
    return t


def preview_kind(a: Attachment) -> str:
    n, t = a.filename.lower(), _mime(a)
    if a.embedded is not None or t == "message/rfc822" or n.endswith((".eml", ".msg")):
        return "message"
    if t in IMAGE_TYPES or n.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
        return "image"
    if t == "application/pdf" or n.endswith(".pdf"):
        return "pdf"
    if t.startswith("text/") or n.endswith(TEXT_EXT):
        return "text"
    if n.endswith(OFFICE_EXT):
        return "office" if _soffice() else ""
    return ""


def preview(a: Attachment) -> Response:
    kind = preview_kind(a)
    t = _mime(a)
    if kind == "image":
        if t == "image/svg+xml":  # never render SVG with scripts: show as image inside a sandbox
            return Response(a.data, media_type=t, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        return Response(a.data, media_type=t or "image/png")
    if kind == "pdf":
        return Response(a.data, media_type="application/pdf",
                        headers={"Content-Disposition": "inline", "Content-Security-Policy": "sandbox allow-scripts"})
    if kind == "text":
        raw = a.data
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                txt = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        return HTMLResponse(f"<pre style='white-space:pre-wrap;font:12px Consolas,monospace;margin:12px'>{H.escape(txt[:2_000_000])}</pre>",
                            headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
    if kind == "message":
        m = a.embedded
        if m is None:
            from ..core.library import open_bytes
            from ..core.model import safe_load
            f = open_bytes(a.filename, a.data)
            m = safe_load(f.items[0]) if f.items else None
        if m is None:
            return HTMLResponse("<p>Can't read this message.</p>", 415)
        return HTMLResponse(X.render_html(m), headers={"Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'"})
    if kind == "office":
        so = _soffice()
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in" + os.path.splitext(a.filename)[1].lower())
            open(src, "wb").write(a.data)
            try:
                subprocess.run([so, "--headless", "--convert-to", "pdf", "--outdir", td, src],
                               capture_output=True, timeout=120)
                out = os.path.join(td, "in.pdf")
                if os.path.exists(out):
                    return Response(open(out, "rb").read(), media_type="application/pdf",
                                    headers={"Content-Disposition": "inline"})
            except (OSError, subprocess.TimeoutExpired):
                pass
    return HTMLResponse("<p style='font-family:sans-serif;margin:16px'>No preview available for this file type. Save it to open it.</p>", 415)
