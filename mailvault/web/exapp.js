/* Nextcloud AppAPI (ExApp) page loader. AppAPI injects this script on the
   MailVault top-menu page; it mounts the shared viewer inside Nextcloud and
   routes every API call through AppAPI's authenticated proxy. */
(function () {
  const APP = 'mailvault'
  const base = OC.generateUrl(`/apps/app_api/proxy/${APP}`)
  const uid = OC.getCurrentUser().uid
  const dav = OC.linkToRemote('dav') + '/files/' + encodeURIComponent(uid)
  const rt = { requesttoken: OC.requestToken }
  window.MV_API_BASE = base
  window.MV_FETCH_HEADERS = rt

  const openId = async (id) => {
    const r = await fetch(`${base}/nc/open?fileId=${encodeURIComponent(id)}`, { method: 'POST', headers: rt })
    const j = await r.json()
    if (!r.ok) throw new Error(j.error || r.statusText)
    window.MV_openSource(j)
  }
  window.MV_NATIVE_OPEN = () => new Promise((resolve) => {
    OC.dialogs.filepicker(t(APP, 'Choose a mail file'), async (paths) => {
      for (const p of (Array.isArray(paths) ? paths : [paths])) {
        try {
          const r = await fetch(dav + p.split('/').map(encodeURIComponent).join('/'), {
            method: 'PROPFIND', headers: { ...rt, Depth: '0', 'Content-Type': 'application/xml' },
            body: '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop><oc:fileid/></d:prop></d:propfind>',
          })
          const id = (await r.text()).match(/<oc:fileid>(\d+)</)
          if (id) await openId(id[1])
        } catch (e) { OC.Notification.showTemporary(e.message) }
      }
      resolve()
    }, true, [], true)
  })

  // Library: choose a folder in the user's Files
  window.MV_PICK_NC_FOLDER = () => new Promise((resolve) => {
    OC.dialogs.filepicker(t('mailvault', 'Choose a folder with mail files'), (path) => resolve(Array.isArray(path) ? path[0] : path),
      false, ['httpd/unix-directory'], true, OC.dialogs.FILEPICKER_TYPE_CHOOSE)
  })

  window.MV_SAVE = async (name, blob) => {
    const toFiles = await new Promise((res) => OC.dialogs.confirmDestructive(
      t(APP, 'Save {name} to your Files, or download it?', { name }), t(APP, 'Save export'),
      { type: OC.dialogs.YES_NO_BUTTONS, confirm: t(APP, 'Save to Files'), cancel: t(APP, 'Download') }, (ok) => res(ok), true))
    if (!toFiles) {
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name
      document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove() }, 2000); return
    }
    await fetch(dav + '/MailVault%20exports', { method: 'MKCOL', headers: rt }).catch(() => {})
    let target = name, n = 1
    while ((await fetch(dav + '/MailVault%20exports/' + encodeURIComponent(target), { method: 'HEAD', headers: rt })).ok) {
      target = name.replace(/(\.[^.]*)?$/, ` (${++n})$1`)
    }
    const r = await fetch(dav + '/MailVault%20exports/' + encodeURIComponent(target), { method: 'PUT', body: blob, headers: rt })
    OC.Notification.showTemporary(r.ok ? t(APP, 'Saved to MailVault exports/{n}', { n: target }) : t(APP, 'Saving failed'))
  }
  window.MV_READY = () => {
    const ids = (new URLSearchParams(location.search).get('fileIds') || '').split(',').filter(Boolean)
    ids.forEach((id) => openId(id).catch((e) => OC.Notification.showTemporary(e.message)))
  }

  const mount = async () => {
    const host = document.getElementById('content') || document.body
    const html = await (await fetch(base + '/', { headers: rt })).text()
    const body = html.slice(html.indexOf('<body>') + 6, html.indexOf('<script'))
    const root = document.createElement('div')
    root.className = 'mailvault-nc'
    root.innerHTML = body
    host.innerHTML = ''
    host.append(root)
    const st = document.createElement('style')
    st.textContent = '.mailvault-nc #app{height:calc(100vh - var(--header-height,50px))}.mailvault-nc #title .name{display:none}' +
      '.mailvault-nc #title{background:var(--color-primary-element,#0f6cbd)}#content{padding-top:0}'
    document.head.append(st)
    const s = document.createElement('script')
    s.src = base + '/static/app.js'
    document.body.append(s)
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount)
  else mount()
})()
