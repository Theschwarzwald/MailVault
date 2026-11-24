document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('mv-save')
  if (!btn) return
  btn.addEventListener('click', async () => {
    const msg = document.getElementById('mv-msg')
    msg.textContent = 'Saving…'
    const body = new URLSearchParams({
      engineUrl: document.getElementById('mv-url').value,
      engineToken: document.getElementById('mv-token').value,
      pathMap: document.getElementById('mv-pathmap').value,
    })
    try {
      const r = await fetch(OC.generateUrl('/apps/mailvault/settings'), {
        method: 'POST', body, headers: { requesttoken: OC.requestToken },
      })
      const j = await r.json()
      msg.textContent = j.message || j.error
      document.getElementById('mv-token').value = ''
    } catch (e) { msg.textContent = e.message }
  })
})
