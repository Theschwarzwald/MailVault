/* Settings → MailVault: manage the folders MailVault scans for mail files. */
document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('mailvault-scan')
  if (!root) return
  const base = OC.generateUrl('/apps/mailvault/proxy/api/library')
  const H = { requesttoken: OC.requestToken, 'Content-Type': 'application/json' }
  const msg = (t) => { document.getElementById('mv-scan-msg').textContent = t || '' }
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
  const ago = (ts) => {
    if (!ts) return 'Not yet'
    const s = Math.round(Date.now() / 1000 - ts)
    if (s < 90) return 'Just now'
    if (s < 5400) return Math.round(s / 60) + ' minutes ago'
    if (s < 129600) return Math.round(s / 3600) + ' hours ago'
    return new Date(ts * 1000).toLocaleString()
  }
  const api = async (url, opt = {}) => {
    const r = await fetch(url, { ...opt, headers: H })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(j.error || r.statusText)
    return j
  }
  let folders = []

  const render = () => {
    const rows = folders.map((f) => `<tr data-id="${esc(f.id)}">
      <td>${f.mapped ? '<span class="mv-tag">shared</span> ' : ''}${esc(f.path)}${f.exists === false ? ' <span class="mv-warn">(missing)</span>' : ''}</td>
      <td><input class="mv-label" value="${esc(f.label)}" ${f.editable ? '' : 'disabled'}></td>
      <td class="mv-count">${f.count == null ? '–' : f.count.toLocaleString()}</td>
      <td class="mv-when">${esc(ago(f.scanned))}</td>
      <td class="mv-btns"><button data-act="scan">Scan now</button>${f.editable ? '<button data-act="remove">Remove</button>' : ''}</td></tr>`)
    document.getElementById('mv-scan-rows').innerHTML = rows.join('')
      || '<tr><td colspan="5" class="mv-muted">No folders yet. Add one to start scanning.</td></tr>'
  }
  const load = async () => {
    try {
      const j = await api(base)
      folders = (j.folders || []).filter((f) => f.kind === 'nextcloud')
      render()
      if (j.error) msg('MailVault engine: ' + j.error)
    } catch (e) { msg(e.message) }
  }
  const pickFolder = () => new Promise((resolve) => {
    OC.dialogs.filepicker(t('mailvault', 'Choose a folder to scan for mail files'),
      (p) => resolve(Array.isArray(p) ? p[0] : p), false, ['httpd/unix-directory'], true, OC.dialogs.FILEPICKER_TYPE_CHOOSE)
  })
  const scan = async (id) => {
    const row = root.querySelector(`tr[data-id="${id}"] .mv-count`)
    if (row) row.textContent = 'Scanning…'
    const j = await api(`${base}/${id}/scan?refresh=1`)
    const f = folders.find((x) => x.id === id)
    if (f) { f.count = j.count; f.scanned = j.scanned }
    render()
    return j.count
  }

  document.getElementById('mv-scan-add').onclick = async () => {
    const path = await pickFolder()
    if (!path) return
    msg('Adding and scanning ' + path + '…')
    try {
      await api(base, { method: 'POST', body: JSON.stringify({ kind: 'nextcloud', path, label: '' }) })
      await load(); msg('Added ' + path); showColl()
    } catch (e) { msg(e.message) }
  }
  const collBase = OC.generateUrl('/apps/mailvault/proxy/api/collection')
  let collTimer = null
  const showColl = async () => {
    clearTimeout(collTimer)
    try {
      const j = await api(collBase)
      const c = j.collection, el = document.getElementById('mv-coll')
      if (!c) { el.textContent = ''; return }
      const busy = c.state !== 'ready' && c.state !== 'idle'
      el.textContent = `All mail: ${c.imported} of ${c.files} file${c.files === 1 ? '' : 's'} imported, ${c.messages} items sorted`
        + (busy ? ` · ${c.message || 'updating…'}` : '') + (c.errors && c.errors.length ? ` · ${c.errors.length} could not be read` : '')
      if (busy) collTimer = setTimeout(showColl, 3000)
    } catch (e) {}
  }
  document.getElementById('mv-scan-all').onclick = async () => {
    msg('Scanning folders and updating All mail…')
    try {
      const r = await api(collBase + '/refresh', { method: 'POST', body: '{}' })
      await load()
      msg(`${r.files} mail file${r.files === 1 ? '' : 's'} in your scan folders`)
      showColl()
    } catch (e) { msg(e.message) }
  }
  root.addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-act]'); if (!b) return
    const id = b.closest('tr').dataset.id
    try {
      if (b.dataset.act === 'scan') { msg(''); const n = await scan(id); msg(`Found ${n} mail file${n === 1 ? '' : 's'}`) }
      if (b.dataset.act === 'remove') {
        await api(`${base}/${id}`, { method: 'DELETE' }); await load(); msg('Removed'); showColl()
      }
    } catch (er) { msg(er.message) }
  })
  root.addEventListener('change', async (e) => {
    if (!e.target.classList.contains('mv-label')) return
    const id = e.target.closest('tr').dataset.id
    try { await api(`${base}/${id}`, { method: 'PATCH', body: JSON.stringify({ label: e.target.value }) }); msg('Renamed') } catch (er) { msg(er.message) }
  })

  const mode = document.getElementById('mv-map-mode')
  if (mode) {
    const who = document.getElementById('mv-map-who')
    mode.onchange = () => { who.style.display = mode.value === 'list' ? '' : 'none' }
    document.getElementById('mv-map-add').onclick = async () => {
      const path = await pickFolder()
      if (!path) return
      msg('Mapping ' + path + '…')
      try {
        await api(base, { method: 'POST', body: JSON.stringify({ kind: 'nextcloud', path, label: '',
          scope: { mode: mode.value, who: who.value.split(',').map((x) => x.trim()).filter(Boolean) } }) })
        await load(); msg('Mapped ' + path)
      } catch (er) { msg(er.message) }
    }
  }
  const acctUrl = OC.generateUrl('/apps/mailvault/proxy/api/collection/accounts')
  const showFound = (d) => {
    document.getElementById('mv-accounts-found').textContent = d && d.length ? 'Accounts in All mail: ' + d.join(', ') : ''
  }
  api(acctUrl).then((j) => { document.getElementById('mv-accounts').value = (j.accounts || []).join('\n'); showFound(j.detected) }).catch(() => {})
  document.getElementById('mv-accounts-save').onclick = async () => {
    const m = document.getElementById('mv-accounts-msg')
    m.textContent = 'Saving…'
    try {
      const j = await api(acctUrl, { method: 'POST', body: JSON.stringify({ accounts: document.getElementById('mv-accounts').value.split(/[\s,;]+/).filter(Boolean) }) })
      document.getElementById('mv-accounts').value = j.accounts.join('\n')
      m.textContent = 'Saved. All mail is re-sorting.'
      showFound(j.detected); showColl()
    } catch (e) { m.textContent = e.message }
  }
  load()
  showColl()
})
