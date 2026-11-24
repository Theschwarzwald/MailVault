"""MailVault command line.

  python -m mailvault serve [--host 0.0.0.0] [--port 8080] [--data /mail]
  python -m mailvault desktop
  python -m mailvault info archive.pst
  python -m mailvault convert archive.pst --to eml --out exported.zip [--only recovered,orphaned]
"""
from __future__ import annotations

import argparse
import sys
import zipfile

from .core.export import FORMATS, export_many, iter_folder
from .core.library import open_path



def _info(path: str):
    root = open_path(path)

    def show(f, d=0):
        flag = f"  [{f.error}]" if f.error else ""
        print("  " * d + f"{f.name}  ({len(f.items)} items){flag}")
        for c in f.children:
            show(c, d + 1)
    show(root)
    stats: dict[str, int] = {}
    for _, m in iter_folder(root):
        stats[m.status] = stats.get(m.status, 0) + 1
    print("\nItems by status:", ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mailvault")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--data", action="append", default=[], help="Folder to browse (repeatable)")
    sub.add_parser("desktop", help="Run the desktop app")
    i = sub.add_parser("info", help="Show folders and item health")
    i.add_argument("file")
    c = sub.add_parser("convert", help="Convert/export a mail file")
    c.add_argument("file")
    c.add_argument("--to", required=True, choices=sorted(FORMATS))
    c.add_argument("--out", required=True, help="Output .zip (or .pst with --to pst)")
    c.add_argument("--only", help="Comma list of statuses: ok,damaged,orphaned,recovered")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        from .server.app import run
        run(a.host, a.port, a.data)
    elif a.cmd == "desktop":
        from .desktop.main import main as dmain
        dmain()
    elif a.cmd == "info":
        _info(a.file)
    elif a.cmd == "convert":
        root = open_path(a.file)
        statuses = set(a.only.split(",")) if a.only else None
        if a.to in ("pst", "pst-ansi"):
            from .core.pstwrite import write_pst
            out = a.out if a.out.lower().endswith(".pst") else a.out + ".pst"
            n = write_pst(out, iter_folder(root, statuses=statuses), root.name,
                          progress=lambda i, t: print(f"\r{i}/{t}", end="", file=sys.stderr), ansi=a.to == "pst-ansi")
            print(f"\nWrote {n} items to {out}")
            return
        with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
            n = export_many(iter_folder(root, statuses=statuses), a.to, z,
                            progress=lambda i, t: print(f"\r{i}/{t}", end="", file=sys.stderr))
        print(f"\nExported {n} messages to {a.out}")



if __name__ == "__main__":
    main()
