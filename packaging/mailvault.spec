# PyInstaller spec: pyinstaller packaging/mailvault.spec
import os
block_cipher = None
root = os.path.abspath(os.path.join(SPECPATH, ".."))
a = Analysis([os.path.join(root, "packaging", "launch.py")], pathex=[root],
             datas=[(os.path.join(root, "mailvault", "web"), "mailvault/web")],
             hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                            "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on", "webview"],
             cipher=block_cipher)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MailVault", console=False,
          icon=(lambda p: p if os.path.exists(p) else None)(os.path.join(root, "packaging", "mailvault.ico")))
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="MailVault")
app = BUNDLE(coll, name="MailVault.app", bundle_identifier="app.mailvault",
             info_plist={"CFBundleDocumentTypes": [{"CFBundleTypeName": "Mail file", "CFBundleTypeRole": "Viewer",
                         "LSItemContentTypes": ["public.data"],
                         "CFBundleTypeExtensions": ["pst", "ost", "msg", "eml", "mbox"]}]})
