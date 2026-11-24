"""Desktop app (Windows, macOS, Linux): the web UI in a native window.

Runs the engine on a private localhost port, protected by a random token, and
shows it with pywebview (Edge WebView2 on Windows, WebKit on macOS, GTK/Qt on
Linux). Files passed on the command line (e.g. double-clicking a .pst after the
installer registers the file types) open straight away.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import string
import sys
import threading
import time


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _roots() -> list[str]:
    roots = [os.path.expanduser("~")]
    if sys.platform == "win32":
        roots += [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    elif sys.platform == "darwin":
        roots.append("/Volumes")
    else:
        roots += [p for p in ("/media", "/mnt") if os.path.isdir(p)]
    return roots


class Bridge:
    """Functions callable from the page as window.pywebview.api.*"""

    def __init__(self):
        self.window = None

    def pick(self):
        import webview
        types = ("Mail files (*.pst;*.ost;*.msg;*.eml;*.emlx;*.mbox;*.mbx;*.zip;*.tar;*.gz;*.tgz;*.7z;*.rar)",
                 "All files (*.*)")
        res = self.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=types)
        return list(res or [])

    def pick_folder(self):
        """Native folder chooser for adding folders to the library."""
        import webview
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return None
        return res if isinstance(res, str) else res[0]

    def save(self, name, b64):
        import base64
        import webview
        res = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=name)
        if not res:
            return False
        path = res if isinstance(res, str) else res[0]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(b64))
        return path


def main():
    try:
        import webview
    except ImportError:
        print("pywebview is not installed; opening in your browser instead.")
        webview = None

    os.environ.setdefault("MAILVAULT_TOKEN", secrets.token_hex(24))
    from ..server import app as srv
    srv.TOKEN = os.environ["MAILVAULT_TOKEN"]
    srv.LOCK = True
    srv.ROOTS[:] = _roots()
    port = _free_port()

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    files = [os.path.abspath(a) for a in sys.argv[1:] if os.path.isfile(a)]
    url = f"http://127.0.0.1:{port}/?t={srv.TOKEN}"
    if webview is None:
        import webbrowser
        webbrowser.open(url)
        threading.Event().wait()
        return

    bridge = Bridge()
    # The ?t= token is swapped for an HttpOnly cookie on first load, so only this
    # window can talk to the engine.
    boot = f"window.MV_START_FILES={json.dumps(files)};"
    win = webview.create_window("MailVault", url, js_api=bridge, width=1360, height=860, min_size=(900, 560))
    bridge.window = win

    def loaded():
        win.evaluate_js(boot + "window.MV_HOST_READY && window.MV_HOST_READY();")
    win.events.loaded += loaded
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.start(private_mode=False)
    server.should_exit = True


if __name__ == "__main__":
    main()
