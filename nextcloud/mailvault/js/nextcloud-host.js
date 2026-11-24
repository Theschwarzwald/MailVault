/* Runs before app.js inside Nextcloud: routes API calls through the PHP proxy,
   adds Nextcloud's CSRF token, opens files picked in Files, and saves exports
   back into Files. */
(function () {
  window.MV_API_BASE = OC.generateUrl('/apps/mailvault/proxy')
  window.MV_FETCH_HEADERS = { requesttoken: OC.requestToken }

  const openNcFile = async (fileId) => {
    const r = await fetch(OC.generateUrl(`/apps/mailvault/files/${fileId}/open`), {
      method: 'POST', headers: { requesttoken: OC.requestToken },
    })
    const j = await r.json()
    if (!r.ok) throw new Error(j.error || r.statusText)
    window.MV_openSource(j)
  }
  window.MV_openNcFile = openNcFile

  // Replace "Open data file" with Nextcloud's own file picker
  window.MV_NATIVE_OPEN = () => new Promise((resolve) => {
    OC.dialogs.filepicker(t('mailvault', 'Choose a mail file'), async (paths) => {
      const list = Array.isArray(paths) ? paths : [paths]
      for (const p of list) {
        try {
          const r = await fetch(OC.linkToRemote('dav') + '/files/' + encodeURIComponent(OC.getCurrentUser().uid) + p.split('/').map(encodeURIComponent).join('/'), {
            method: 'PROPFIND', headers: { Depth: '0', 'Content-Type': 'application/xml', requesttoken: OC.requestToken },
            body: '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop><oc:fileid/></d:prop></d:propfind>',
          })
          const id = (await r.text()).match(/<oc:fileid>(\d+)</)
          if (id) await openNcFile(id[1])
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

  // Exports: offer download or saving into Files
  window.MV_SAVE = async (name, blob) => {
    const choice = await new Promise((res) => OC.dialogs.confirmDestructive(
      t('mailvault', 'Save {name} to your Files, or download it?', { name }),
      t('mailvault', 'Save export'),
      { type: OC.dialogs.YES_NO_BUTTONS, confirm: t('mailvault', 'Save to Files'), cancel: t('mailvault', 'Download') },
      (ok) => res(ok), true))
    if (!choice) {
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name
      document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove() }, 2000)
      return
    }
    const fd = new FormData(); fd.append('file', blob, name); fd.append('name', name); fd.append('dir', '/MailVault exports')
    // make sure the folder exists
    await fetch(OC.linkToRemote('dav') + '/files/' + encodeURIComponent(OC.getCurrentUser().uid) + '/MailVault%20exports',
      { method: 'MKCOL', headers: { requesttoken: OC.requestToken } }).catch(() => {})
    const r = await fetch(OC.generateUrl('/apps/mailvault/files/save'), { method: 'POST', body: fd, headers: { requesttoken: OC.requestToken } })
    const j = await r.json()
    OC.Notification.showTemporary(r.ok ? t('mailvault', 'Saved to {path}', { path: j.path }) : j.error)
  }

  // Opened from Files via ?fileId=
  window.MV_READY = () => {
    const id = new URLSearchParams(location.search).get('fileId')
    if (id) openNcFile(id).catch((e) => OC.Notification.showTemporary(e.message))
    const root = document.getElementById('mailvault-root')
    if (root && root.dataset.configured !== '1') {
      OC.Notification.showTemporary(t('mailvault', 'MailVault is not connected to its engine yet. An admin can set it up in Administration settings → MailVault.'), { timeout: 15 })
    }
  }
})()
