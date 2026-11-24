"""Compressed RTF (LZFu, MS-OXRTFCP) and RTF -> HTML / text conversion."""
from __future__ import annotations

import html as _html
import re
import struct

_PREBUF = (b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman "
           b"\\fswiss \\fmodern \\fscript \\fdecor MS Sans SerifSymbolArialTimes New Roman"
           b"Courier{\\colortbl\\red0\\green0\\blue0\r\n\\par \\pard\\plain\\f0\\fs20\\b\\i"
           b"\\u\\tab\\tx")


def decompress_rtf(data: bytes) -> bytes:
    if len(data) < 16:
        return b""
    comp_size, raw_size, magic, _crc = struct.unpack_from("<IIII", data, 0)
    if magic == 0x414C454D:  # "MELA" - stored uncompressed
        return data[16:16 + raw_size]
    if magic != 0x75465A4C:  # "LZFu"
        raise ValueError("Unknown compressed RTF signature")
    buf = bytearray(4096)
    buf[:len(_PREBUF)] = _PREBUF
    wp = len(_PREBUF)
    out = bytearray()
    src = data[16:16 + comp_size - 12] if comp_size >= 12 else data[16:]
    i, n = 0, len(src)
    while i < n:
        ctrl = src[i]
        i += 1
        for bit in range(8):
            if i >= n:
                break
            if ctrl & (1 << bit):
                if i + 1 >= n:
                    i = n
                    break
                ref = (src[i] << 8) | src[i + 1]
                i += 2
                off, ln = ref >> 4, (ref & 0xF) + 2
                if off == wp:
                    return bytes(out)
                for k in range(ln):
                    c = buf[(off + k) & 0xFFF]
                    out.append(c)
                    buf[wp] = c
                    wp = (wp + 1) & 0xFFF
            else:
                c = src[i]
                i += 1
                out.append(c)
                buf[wp] = c
                wp = (wp + 1) & 0xFFF
    return bytes(out)


_TOKEN = re.compile(rb"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|(\r\n|\n|\r)|([^\\{}\r\n]+)")
_SKIP_DESTS = {b"fonttbl", b"colortbl", b"stylesheet", b"info", b"pict", b"object",
               b"header", b"footer", b"listtable", b"listoverridetable", b"themedata",
               b"datastore", b"latentstyles", b"generator", b"xmlnstbl", b"rsidtbl",
               b"mmathPr", b"filetbl", b"revtbl"}


def _codepage(rtf: bytes) -> str:
    m = re.search(rb"\\ansicpg(\d+)", rtf)
    return f"cp{m.group(1).decode()}" if m else "cp1252"


def rtf_extract(rtf: bytes) -> tuple[str, bool]:
    """Return (content, is_html). HTML for \\fromhtml1 encapsulated bodies."""
    is_html = b"\\fromhtml1" in rtf[:1024]
    cp = _codepage(rtf)
    out: list[str] = []
    pend = bytearray()

    def flush():
        if pend:
            try:
                out.append(pend.decode(cp))
            except (LookupError, UnicodeDecodeError):
                out.append(pend.decode("cp1252", "replace"))
            pend.clear()

    # stack frames: [skip, htmlrtf, in_htmltag, uc]
    stack = [[False, False, False, 1]]
    star = False
    uc_skip = 0
    for m in _TOKEN.finditer(rtf):
        word, arg, hexv, sym, brace, nl, text = m.groups()
        st = stack[-1]
        if brace == b"{":
            stack.append(list(st))
            star = False
            continue
        if brace == b"}":
            flush()
            if len(stack) > 1:
                stack.pop()
            continue
        if uc_skip and (text or hexv):
            if text:
                cut = min(uc_skip, len(text))
                uc_skip -= cut
                text = text[cut:]
                if not text:
                    continue
            else:
                uc_skip -= 1
                continue
        visible = not st[0] and (not is_html or st[2] or not st[1])
        if sym is not None:
            if sym == b"*":
                star = True
            elif visible and sym in (b"\\", b"{", b"}"):
                pend += sym
            elif visible and sym == b"~":
                pend += b"\xa0"
            continue
        if word is not None:
            w = word
            if star and w == b"htmltag":
                st[2] = True
                st[0] = False
            elif star or w in _SKIP_DESTS:
                st[0] = True  # unknown/ignorable destination, incl. \*\mhtmltag
            elif w == b"htmlrtf":
                st[1] = arg != b"0"
            elif w == b"uc":
                st[3] = int(arg or 1)
            elif w == b"u" and arg is not None:
                if visible:
                    flush()
                    cpv = int(arg)
                    out.append(chr(cpv + 65536 if cpv < 0 else cpv))
                uc_skip = st[3]
            elif visible and w in (b"par", b"line") and not is_html:
                flush()
                out.append("\n")
            elif visible and w in (b"par", b"line") and is_html and not st[1]:
                flush()
                out.append("\r\n")
            elif visible and w == b"tab":
                flush()
                out.append("\t")
            star = False
            continue
        if hexv is not None:
            if visible:
                pend.append(int(hexv, 16))
            continue
        if nl is not None:
            continue
        if text is not None and visible:
            pend += text
    flush()
    return "".join(out), is_html


def rtf_to_html(rtf: bytes) -> str:
    content, is_html = rtf_extract(rtf)
    if is_html:
        return content
    return "<div style='white-space:pre-wrap;font-family:Calibri,Arial,sans-serif'>" + \
        _html.escape(content) + "</div>"


def rtf_to_text(rtf: bytes) -> str:
    content, is_html = rtf_extract(rtf)
    if is_html:
        from .mime import html_to_text
        return html_to_text(content)
    return content
