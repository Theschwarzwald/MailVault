/* MailVault web UI - Outlook classic layout. Hosts (desktop, Nextcloud) may set
   window.MV_API_BASE and window.MV_FETCH_HEADERS before this script loads. */
(() => {
const API = (window.MV_API_BASE || "").replace(/\/$/, "");
const HDRS = window.MV_FETCH_HEADERS || {};
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = (n, cls = "i") => `<svg class="${cls}"><use href="#i-${n}"/></svg>`;
const S = {sources: [], cur: null, rows: [], total: 0, sel: new Set(), focus: -1, filter: "", sort: "date",
           desc: true, q: "", msg: null, remote: false, tab: "home", config: {formats: {}, roots: []},
           scope: "folder", onlyProblems: false, globalHits: null, module: "mail", layout: "compact",
           threads: false, expanded: new Set(), state: "", category: "", preview: null, jobs: {}, index: {},
           calMonth: null, exportState: "", library: {folders: [], can_edit: false, admin: false},
           libScan: {}, libOpen: new Set()};
const STATES = ["Received","Sent","Draft","Unread","Read","Replied","Replied all","Forwarded","Flagged","Completed",
                "Starred","Important","Deleted","Archived","Junk"];
const STATE_ICON = {Replied: "reply", "Replied all": "reply", Forwarded: "forward", Archived: "archive", Deleted: "trash",
                    Sent: "send", Draft: "note", Flagged: "flag", Junk: "warn"};
const CAT_COLORS = ["#e74856","#ff8c00","#ffb900","#16c60c","#0078d4","#8764b8","#038387","#c239b3"];
const catColor = c => CAT_COLORS[[...c].reduce((a, ch) => a + ch.charCodeAt(0), 0) % CAT_COLORS.length];
const MODULES = [["mail","mail","Mail"],["calendar","calendar","Calendar"],["contacts","people","People"],["tasks","task","Tasks"],["notes","note","Notes"]];

async function api(path, opt = {}) {
  opt.headers = {...HDRS, ...(opt.headers || {})};
  const r = await fetch(API + path, opt);
  const ct = r.headers.get("content-type") || "";
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).error || m; } catch {} throw new Error(m); }
  return ct.includes("json") ? r.json() : r;
}
function toast(t, ms = 2600) { const d = document.createElement("div"); d.className = "toast"; d.textContent = t;
  mvHost().append(d); setTimeout(() => d.remove(), ms); }
function fmtSize(n) { if (n < 1024) return n + " B"; if (n < 1048576) return (n/1024).toFixed(0) + " KB"; return (n/1048576).toFixed(1) + " MB"; }
function fmtDate(iso, long) {
  if (!iso) return ""; const d = new Date(iso), now = new Date();
  if (long) return d.toLocaleString(undefined, {weekday:"short", year:"numeric", month:"numeric", day:"numeric", hour:"numeric", minute:"2-digit"});
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString(undefined, {hour:"numeric", minute:"2-digit"});
  if ((now - d) < 6 * 864e5) return d.toLocaleDateString(undefined, {weekday:"short"}) + " " + d.toLocaleTimeString(undefined, {hour:"numeric", minute:"2-digit"});
  return d.toLocaleDateString();
}
function initials(s) { const n = (s || "?").replace(/<.*>/, "").trim().split(/\s+/); return ((n[0]||"?")[0] + (n.length > 1 ? n[n.length-1][0] : "")).toUpperCase(); }
const PALETTE = ["#0f6cbd","#8764b8","#038387","#ca5010","#4f6bed","#498205","#c239b3","#986f0b"];
const color = s => PALETTE[[...(s||"")].reduce((a, c) => a + c.charCodeAt(0), 0) % PALETTE.length];

/* ------------------------------------------------------------ ribbon */
const TABS = [["file","File"],["home","Home"],["tools","Tools"],["export","Export"],["view","View"]];
const FORMAT_BTNS = [["pdf","PDF","pdf"],["pdf-combined","Single PDF","pdf"],["eml","EML","mail"],["msg","MSG","mail"],
  ["pst","PST","db"],["pst-ansi","PST 97–2002","db"],["mbox","MBOX","db"],["html","HTML","code"],["txt","Text","list"],["ics","Calendar (ICS)","calendar"],["vcf","Contacts (VCF)","people"],
  ["csv","CSV index","csv"],["json","JSON","code"],["attachments","Attachments","clip"]];
function big(act, ic, label) { return `<button class="big" data-act="${act}">${icon(ic)}<span>${label}</span></button>`; }
function sm(act, ic, label) { return `<button class="sm" data-act="${act}">${icon(ic)}${label}</button>`; }
function grp(cap, inner) { return `<div class="grp"><div class="btns">${inner}</div><div class="cap">${cap}</div></div>`; }
function ribbon() {
  $("#tabs").innerHTML = TABS.map(([k, l]) => `<button data-tab="${k}" class="${k === "file" ? "file" : ""} ${S.tab === k ? "on" : ""}">${l}</button>`).join("");
  let h = "";
  if (S.tab === "home") h =
    grp("Open", big("open","open","Open data file") + big("addfolder","folder","Add folder") + big("upload","upload","Upload files") + big("close","close","Close mailbox")) +
    grp("Find", `<div class="stack">${sm("find","search","Search all mailboxes")}${sm("filter-problem","warn","Problem items")}${sm("filter-unread","mail","Unread")}</div>`) +
    grp("Current item", big("save-pdf","pdf","Save as PDF") + `<div class="stack">${sm("save-msg","mail","Save as MSG")}${sm("save-eml","mail","Save as EML")}${sm("print","print","Print")}</div>`) +
    grp("Attachments", big("save-atts","clip","Save all attachments"));
  if (S.tab === "tools") h =
    grp("Recovery", big("health","aid","Health report") + big("deepscan","scan","Deep scan") + big("report","pdf","Recovery report") +
      `<div class="stack">${sm("goto-recovered","warn","Recovered items")}${sm("goto-orphaned","unlink","Orphaned items")}${sm("filter-problem","warn","Damaged items")}</div>`) +
    grp("Inspect", big("headers","code","Message headers") + `<div class="stack">${sm("props","list","MAPI properties")}${sm("source","code","View source")}${sm("open-att","zip","Open attached mail")}</div>`) +
    grp("Clean up", big("dups","dup","Find duplicates"));
  if (S.tab === "export") h =
    grp("Export to", FORMAT_BTNS.map(([f, l, ic]) => big("export:" + f, ic, l)).join("")) +
    grp("Scope", `<div class="stack"><select class="ribbon-sel" id="scope">
      ${[["selected","Selected items"],["folder","Current folder"],["recursive","Folder and subfolders"],["all","Entire mailbox"]]
        .map(([v,l]) => `<option value="${v}" ${S.scope===v?"selected":""}>${l}</option>`).join("")}</select>
      <label class="sm"><input type="checkbox" id="onlyprob" ${S.onlyProblems?"checked":""}> Only damaged, orphaned, recovered</label>
      <select class="ribbon-sel" id="exstate"><option value="">Any state</option>${STATES.map(x => `<option ${S.exportState===x?"selected":""}>${x}</option>`).join("")}</select></div>`);
  if (S.tab === "view") h =
    grp("Layout", big("pane-right","layout","Reading pane right") + big("pane-off","list","Reading pane off")) +
    grp("Message list", big("layout-compact","list",(S.layout==="compact"?"✓ ":"")+"Compact") + big("layout-columns","columns",(S.layout==="columns"?"✓ ":"")+"Columns") +
      big("threads","thread",(S.threads?"✓ ":"")+"Show as conversations")) +
    grp("Privacy", big("remote", "eye", S.remote ? "Block remote images" : "Load remote images")) +
    grp("Sort", `<div class="stack">${sm("sort:date","list","Date")}${sm("sort:from","list","From")}${sm("sort:subject","list","Subject")}${sm("sort:size","list","Size")}</div>`) +
    grp("Help", big("about","info","About"));
  if (S.tab === "file") { S.tab = "home"; ribbon(); openDialog(); return; }
  $("#ribbon").innerHTML = h;
  const sc = $("#scope"); if (sc) sc.onchange = () => S.scope = sc.value;
  const op = $("#onlyprob"); if (op) op.onchange = () => S.onlyProblems = op.checked;
  const es = $("#exstate"); if (es) es.onchange = () => S.exportState = es.value;
}
document.addEventListener("click", e => {
  const t = e.target.closest("[data-tab]"); if (t) { S.tab = t.dataset.tab; ribbon(); return; }
  const a = e.target.closest("[data-act]"); if (a) act(a.dataset.act);
  if (!e.target.closest(".ctx")) document.querySelectorAll(".ctx").forEach(c => c.remove());
});

async function act(a) {
  try {
    if (a.startsWith("export:")) return exportBatch(a.slice(7));
    if (a.startsWith("sort:")) { setSort(a.slice(5)); return; }
    const m = S.msg, c = S.cur;
    const needMsg = () => { if (!m) throw new Error("Select a message first"); };
    switch (a) {
      case "open": if (window.MV_NATIVE_OPEN) return window.MV_NATIVE_OPEN();
        if (window.pywebview?.api?.pick) { for (const f of await window.pywebview.api.pick()) await openPath(f); return; } return openDialog();
      case "addfolder": return addFolderDialog();
      case "upload": return $("#filepick").click();  // in Nextcloud this uploads from your computer
      case "close": if (c) { await api(`/api/sources/${c.sid}`, {method: "DELETE"}); S.sources = S.sources.filter(s => s.id !== c.sid); S.cur = null; S.msg = null; renderTree(); renderRows(); renderRead(); } return;
      case "find": $("#search").focus(); return;
      case "filter-problem": return setFilter("problem");
      case "filter-unread": return setFilter("unread");
      case "save-pdf": needMsg(); return download(`${msgUrl()}/export?fmt=pdf`);
      case "save-msg": needMsg(); return download(`${msgUrl()}/export?fmt=msg`);
      case "save-eml": needMsg(); return download(`${msgUrl()}/export?fmt=eml`);
      case "save-ics": needMsg(); return download(`${msgUrl()}/export?fmt=ics`);
      case "save-vcf": needMsg(); return download(`${msgUrl()}/export?fmt=vcf`);
      case "print": needMsg(); { const f = $("#bodyframe"); const w = window.open("", "_blank");
        const r = await api(`${msgUrl()}/export?fmt=html`); w.document.write(await r.text()); w.document.close(); w.focus(); w.print(); } return;
      case "save-atts": needMsg(); return exportBatch("attachments", "selected");
      case "health": return healthDialog();
      case "goto-recovered": return gotoKind("recovered");
      case "goto-orphaned": return gotoKind("orphaned");
      case "headers": needMsg(); return dialog("Message headers", `<pre class="src">${esc(m.headers || "(no transport headers stored for this item)")}</pre>`);
      case "props": needMsg(); return dialog("Properties", `<table class="kv">${
        [["Format", m.format], ["Status", m.status], ["Message-ID", m.message_id], ...Object.entries(m.props)]
        .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("")}</table>`);
      case "source": needMsg(); { const r = await api(`${msgUrl()}/export?fmt=eml`); const t = await r.text();
        return dialog("Source (RFC 822)", `<pre class="src">${esc(t.slice(0, 400000))}</pre>`); }
      case "open-att": needMsg(); { const at = m.attachments.find(x => x.embedded || /\.(msg|eml|zip|pst|mbox)$/i.test(x.name));
        if (!at) throw new Error("This message has no attached mail or archive"); return openAttachment(at.i); }
      case "dups": return dupDialog();
      case "deepscan": if (!c) throw new Error("Open a mailbox first"); return trackJob(await api(`/api/sources/${c.sid}/deepscan`, {method: "POST"}), "deepscan");
      case "report": if (!c) throw new Error("Open a mailbox first"); return download(`/api/sources/${c.sid}/report`);
      case "layout-compact": S.layout = "compact"; $("#main").classList.remove("cols"); ribbon(); renderRows(); return;
      case "layout-columns": S.layout = "columns"; $("#main").classList.add("cols"); ribbon(); renderRows(); return;
      case "threads": S.threads = !S.threads; ribbon(); renderRows(); return;
      case "pane-right": $("#main").style.removeProperty("--lw"); $("#read").style.display = ""; return;
      case "pane-off": $("#main").style.setProperty("--lw", "1fr"); $("#main").style.gridTemplateColumns = "var(--fw,230px) 4px 1fr 0 0"; $("#read").style.display = "none"; return;
      case "remote": S.remote = !S.remote; ribbon(); if (S.msg) renderRead(); return;
      case "about": return dialog("About MailVault", `<p>MailVault ${esc(S.config.version || "")} — viewer, converter and recovery tool for PST, OST, MSG, EML and MBOX files, including archives (ZIP, TAR, GZ, 7Z, RAR).</p>
        <p>Formats: ${Object.values(S.config.formats).map(esc).join(", ")}.</p>`);
    }
  } catch (err) { toast(err.message); }
}

/* -------------------------------------------------------- open files */
async function openDialog() {
  let path = null, picked = null;
  const roots = S.config.roots || [];
  const d = dialog("Open Outlook data file", `
    ${roots.length ? `<div style="margin-bottom:6px;color:var(--muted)" id="cwd"></div><div class="fl" id="fl"></div>` : ""}
    <div class="drop" id="drop">Drop .pst, .ost, .msg, .eml, .mbox or archive files here, or <a href="#" id="pick">choose files</a> to upload</div>`,
    roots.length ? [["Open", "pri", async () => { if (!picked) return toast("Choose a file"); await openPath(picked); return true; }]] : []);
  $("#pick", d).onclick = e => { e.preventDefault(); $("#filepick").click(); };
  dropZone($("#drop", d), () => d.closest(".mask").remove());
  async function load(p) {
    const r = await api(`/api/browse?path=${encodeURIComponent(p || "")}`); path = r.path; picked = null;
    $("#cwd", d).textContent = r.path;
    $("#fl", d).innerHTML = (r.parent ? `<div class="fe" data-dir="${esc(r.parent)}">${icon("folder")} ..</div>` : "") +
      r.entries.map(e => `<div class="fe" ${e.dir ? `data-dir="${esc(e.path)}"` : `data-file="${esc(e.path)}"`}>${icon(e.dir ? "folder" : /\.(zip|tar|gz|7z|rar)$/i.test(e.name) ? "zip" : /\.(pst|ost)$/i.test(e.name) ? "db" : "mail")}
        <span>${esc(e.name)}</span><span class="sz">${e.dir ? "" : fmtSize(e.size)}</span></div>`).join("");
    $("#fl", d).onclick = ev => { const f = ev.target.closest(".fe"); if (!f) return;
      d.querySelectorAll(".fe.on").forEach(x => x.classList.remove("on")); f.classList.add("on");
      if (f.dataset.dir) load(f.dataset.dir); else picked = f.dataset.file; };
    $("#fl", d).ondblclick = async ev => { const f = ev.target.closest(".fe"); if (f && f.dataset.file) { await openPath(f.dataset.file); d.closest(".mask").remove(); } };
  }
  if (roots.length) load(roots[0]).catch(e => toast(e.message));
}
async function openPath(p) {
  status("Opening " + p.split(/[\\/]/).pop() + "…");
  const r = await api("/api/open", {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify({path: p})});
  if (!r.collection) return addSource(r);
  // Windows app: mailbox files join your mail, sorted into accounts and folders in the background
  toast(`Adding ${r.name} to your mail. Its messages appear in your accounts as they're read.`, 5000);
  S.pendingUntil = Date.now() + 20000; S.pendingVersion = S.collection ? S.collection.version : -1; status(); setTimeout(poll, 1500);
}
async function uploadFiles(files) {
  for (const f of files) {
    status(`Uploading ${f.name} (${fmtSize(f.size)})…`);
    try { addSource(await api(`/api/upload?name=${encodeURIComponent(f.name)}`, {method: "POST", body: f})); }
    catch (e) { toast(`${f.name}: ${e.message}`); }
  }
}
window.MV_openSource = addSource; // hosts (Nextcloud) can inject an already-opened mailbox
window.MV_openPath = p => openPath(p).catch(e => toast(e.message));
window.MV_HOST_READY = () => (window.MV_START_FILES || []).forEach(f => window.MV_openPath(f));
if (!window.MV_SAVE) window.MV_SAVE = async (name, blob) => {
  if (!window.pywebview?.api?.save) { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name;
    mvHost().append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000); return; }
  const b64 = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result.split(",")[1]); fr.readAsDataURL(blob); });
  await window.pywebview.api.save(name, b64);
};
function dropZone(el, done) {
  el.ondragover = e => { e.preventDefault(); el.classList.add("over"); };
  el.ondragleave = () => el.classList.remove("over");
  el.ondrop = async e => { e.preventDefault(); el.classList.remove("over"); done && done(); await uploadFiles([...e.dataTransfer.files]); };
}
$("#filepick").onchange = e => { uploadFiles([...e.target.files]); e.target.value = ""; document.querySelectorAll(".mask").forEach(m => m.remove()); };
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => { if (!e.target.closest(".drop")) { e.preventDefault(); if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]); } });

function addSource(s) {
  S.sources.push(s); if (s.index) S.index[s.id] = s.index; poll(); renderTree();
  const first = firstWithItems(s.tree) || s.tree;
  selectFolder(s.id, first.id);
  const bad = countKinds(s.tree);
  status(bad ? `Opened ${s.name}: ${bad} recovered or orphaned items found` : `Opened ${s.name}`);
}
function firstWithItems(f) { if (/inbox/i.test(f.name) && f.count) return f; for (const c of f.children) { const r = firstWithItems(c); if (r) return r; } return f.count ? f : null; }
function countKinds(f) { return (["recovered","orphaned"].includes(f.kind) ? f.count : 0) + f.children.reduce((a, c) => a + countKinds(c), 0); }

/* ------------------------------------------------------------- library */
async function loadLibrary() {
  try { S.library = await api("/api/library"); } catch { S.library = {folders: [], can_edit: false, admin: false}; }
  renderTree();
}
async function scanFolder(id, refresh) {
  S.libScan[id] = await api(`/api/library/${id}/scan${refresh ? "?refresh=1" : ""}`);
}
const KIND_ICON = {server: "server", nextcloud: "cloud", local: "monitor"};
function libraryHtml() {
  const L = S.library, fs = L.folders || [];
  const canAdd = L.can_edit || ["nextcloud", "exapp"].includes(S.config.host);
  let h = `<div class="fav">Library${canAdd ? `<button class="add" data-libadd title="Add folder" aria-label="Add folder">${icon("plus")}</button>` : ""}</div>`;
  if (!fs.length) return canAdd ? h + `<div class="node libnote" style="padding-left:12px">Add folders to keep their mail files one click away.</div>` : "";
  for (const f of fs) {
    const key = f.id + ":", open = S.libOpen.has(key), sc = S.libScan[f.id];
    const ic = f.mapped ? "people" : f.kind === "nextcloud" ? "cloud" : S.config.host === "desktop" ? "monitor" : "server";
    h += `<div class="node lib ${f.exists === false ? "missing" : ""}" data-lib="${f.id}" data-key="${esc(key)}" data-root="1" title="${esc(f.path)}${f.exists === false ? " (not available)" : ""}" style="padding-left:8px">
      <span class="tw">${open ? "◢" : "▷"}</span>${icon(ic)}<span class="lbl">${esc(f.label)}</span>${sc ? `<span class="cnt">${sc.count}${sc.truncated ? "+" : ""}</span>` : f.count != null ? `<span class="cnt">${f.count}</span>` : ""}</div>`;
    if (open) h += sc ? libNodeHtml(f, sc, "", 1) : `<div class="node libnote" style="padding-left:34px">Scanning…</div>`;
  }
  return h;
}
function libNodeHtml(f, node, rel, depth) {
  let h = "";
  for (const d of node.dirs) {
    const r = rel ? rel + "/" + d.name : d.name, key = f.id + ":" + r, open = S.libOpen.has(key);
    h += `<div class="node lib" data-lib="${f.id}" data-key="${esc(key)}" style="padding-left:${8 + depth * 14}px"><span class="tw">${open ? "◢" : "▷"}</span>${icon("folder")}<span class="lbl">${esc(d.name)}</span></div>`;
    if (open) h += libNodeHtml(f, d, r, depth + 1);
  }
  const LIB_MAX = 300;                 // a huge folder lists its first files; All mail and search have them all
  for (const x of node.files.slice(0, LIB_MAX)) {
    const ic = /\.(pst|ost)$/i.test(x.name) ? "db" : /\.(zip|tar|gz|tgz|7z|rar|bz2|xz)$/i.test(x.name) ? "zip" : "mail";
    h += `<div class="node libfile" data-lib="${f.id}" data-name="${esc(x.name)}" ${x.fileId ? `data-fileid="${x.fileId}"` : `data-path="${esc(x.path)}"`} style="padding-left:${22 + depth * 14}px" title="${esc(x.name)}">${icon(ic)}<span class="lbl">${esc(x.name)}</span><span class="sz">${fmtSize(x.size || 0)}</span></div>`;
  }
  if (node.files.length > LIB_MAX) h += `<div class="node libnote" style="padding-left:${22 + depth * 14}px">…and ${node.files.length - LIB_MAX} more (all of them are in All mail)</div>`;
  if (!node.dirs.length && !node.files.length && depth === 1) h += `<div class="node libnote" style="padding-left:34px">No mail files here.</div>`;
  return h;
}
async function openAll(f, rel) {
  if (!S.libScan[f.id]) await scanFolder(f.id);
  const files = []; const walk = n => { n.files.forEach(x => files.push(x)); n.dirs.forEach(walk); }; walk(S.libScan[f.id]);
  if (!files.length) return toast("No mail files in this folder");
  if (files.length > 25 && !confirm(`Open ${files.length} files? Only the first 25 will be opened.`)) return;
  for (const x of files.slice(0, 25)) {
    status("Opening " + x.name + "…");
    try { addSource(await api(`/api/library/${f.id}/open`, {method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify(x.fileId ? {fileId: x.fileId} : {path: x.path})})); } catch (er) { toast(`${x.name}: ${er.message}`); }
  }
}
function scopeHtml(sc, me) {
  sc = sc || {mode: me ? "me" : "everyone", who: []};
  return `<label>Visible to</label><select id="lf-scope">${me ? `<option value="me" ${sc.mode === "me" ? "selected" : ""}>Only me</option>` : ""}<option value="everyone" ${sc.mode === "everyone" ? "selected" : ""}>Everyone</option>
    <option value="admins" ${sc.mode === "admins" ? "selected" : ""}>Administrators only</option>
    <option value="list" ${sc.mode === "list" ? "selected" : ""}>Specific users or groups</option></select>
    <label>Users or groups</label><input id="lf-who" placeholder="alice, finance-team" value="${esc((sc.who || []).join(", "))}">`;
}
function readScope(d) {
  const m = $("#lf-scope", d); if (!m) return null;
  return {mode: m.value, who: $("#lf-who", d).value.split(",").map(x => x.trim()).filter(Boolean)};
}
async function addFolderDialog() {
  const L = S.library, host = S.config.host;
  const nc = ["nextcloud", "exapp"].includes(host), serverOK = L.can_edit;
  const tabs = [];
  if (serverOK) tabs.push(["server", host === "desktop" ? "On this computer" : "On the server"]);
  if (nc) tabs.push(["nextcloud", "From my Nextcloud Files"]);
  if (!tabs.length) return toast("Ask an administrator to add folders");
  let tab = tabs[0][0], cwd = null;
  const d = dialog("Add folder to the library", `
    ${tabs.length > 1 ? `<div class="lib-tabs">${tabs.map(([k, l]) => `<button data-tab-l="${k}" class="${k === tab ? "on" : ""}">${l}</button>`).join("")}</div>` : ""}
    <div id="lf-body"></div>`, [["Add folder", "pri", async () => {
      const label = ($("#lf-label", d) || {}).value || "";
      if (tab === "nextcloud") {
        const path = $("#lf-ncpath", d).value; if (!path) { toast("Choose a folder first"); return false; }
        await api("/api/library", {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify({kind: "nextcloud", path, label, scope: readScope(d)})});
      } else {
        const path = $("#lf-path", d).value.trim(); if (!path) { toast("Choose or type a folder"); return false; }
        await api("/api/library", {method: "POST", headers: {"content-type": "application/json"},
          body: JSON.stringify({kind: "server", path, label, scope: readScope(d)})});
      }
      await loadLibrary(); toast("Folder added to the library"); return true; }]]);
  async function browseTo(p) {
    try {
      const r = await api(`/api/browse?pick=1${p ? "&path=" + encodeURIComponent(p) : ""}`); cwd = r.path;
      $("#lf-path", d).value = r.path;
      $("#lf-list", d).innerHTML = (r.parent ? `<div class="fe" data-dir="${esc(r.parent)}">${icon("folder")} ..</div>` : "") +
        r.entries.map(e => `<div class="fe" data-dir="${esc(e.path)}">${icon("folder")}<span>${esc(e.name)}</span></div>`).join("") ||
        `<div class="hint" style="padding:8px">No subfolders.</div>`;
    } catch (er) { toast(er.message); }
  }
  function render() {
    d.querySelectorAll("[data-tab-l]").forEach(b => b.classList.toggle("on", b.dataset.tabL === tab));
    const body = $("#lf-body", d);
    if (tab === "server") {
      body.innerHTML = `${host === "desktop" && window.pywebview?.api?.pick_folder ? `<p><button class="btn" id="lf-native">${icon("folder")} Choose a folder…</button></p>` : ""}
        <div class="fl" id="lf-list" style="height:220px"></div>
        <div class="frm"><label for="lf-path">Folder</label><input id="lf-path" placeholder="${host === "desktop" ? "C:\\Mail archives" : "/archives"}">
        <label for="lf-label">Name in MailVault</label><input id="lf-label" placeholder="Optional">
        ${nc && L.admin ? scopeHtml() : ""}</div>
        ${host === "web" || nc ? `<p class="hint">The folder must be readable by the MailVault engine. In Docker, mount it into the container first (for example <code>-v /srv/archives:/archives:ro</code>).</p>` : ""}`;
      $("#lf-list", d).onclick = ev => { const f = ev.target.closest("[data-dir]"); if (f) browseTo(f.dataset.dir); };
      const nb = $("#lf-native", d); if (nb) nb.onclick = async () => { const p = await window.pywebview.api.pick_folder(); if (p) { $("#lf-path", d).value = p; browseTo(p); } };
      browseTo(null);
    } else {
      body.innerHTML = `<p>Pick any folder you can open in Nextcloud: your own, one shared with you, a group folder or external storage. MailVault lists the mail files inside it (including subfolders).${L.nc_admin ? "" : " Only you see it."}</p>
        <p><button class="btn" id="lf-ncpick">${icon("cloud")} Choose a folder in Files…</button> <span id="lf-ncshow" class="hint"></span></p>
        <input type="hidden" id="lf-ncpath"><div class="frm"><label for="lf-label">Name in MailVault</label><input id="lf-label" placeholder="Optional">${L.nc_admin ? scopeHtml(null, true) : ""}</div>
        ${L.nc_admin ? `<p class="hint">As an administrator you can also map this folder for other people. They open its files through your access, even if it isn't shared with them in Files.</p>` : ""}`;
      $("#lf-ncpick", d).onclick = async () => {
        if (!window.MV_PICK_NC_FOLDER) return toast("Folder picking isn't available here");
        const p = await window.MV_PICK_NC_FOLDER(); if (p) { $("#lf-ncpath", d).value = p; $("#lf-ncshow", d).textContent = p; }
      };
    }
  }
  d.addEventListener("click", ev => { const t = ev.target.closest("[data-tab-l]"); if (t) { tab = t.dataset.tabL; render(); } });
  render();
}
function editFolderDialog(f) {
  const showScope = (f.kind === "server" || f.mapped) && (S.library.admin || S.library.nc_admin) && ["nextcloud", "exapp"].includes(S.config.host);
  const d = dialog(`Edit ${f.label}`, `<div class="frm"><label>Folder</label><span>${esc(f.path)}</span>
    <label for="lf-label">Name in MailVault</label><input id="lf-label" value="${esc(f.label)}">${showScope ? scopeHtml(f.scope) : ""}</div>`,
    [["Save", "pri", async () => {
      await api(`/api/library/${f.id}`, {method: "PATCH", headers: {"content-type": "application/json"},
        body: JSON.stringify({label: $("#lf-label", d).value, scope: showScope ? readScope(d) : null})});
      await loadLibrary(); return true; }]]);
}

/* ------------------------------------------------------------- folders */
const FICON = {store: "db", archive: "zip", file: "mail", recovered: "warn", orphaned: "unlink", search: "search", invites: "calendar", group: "list", person: "people", account: "mail",
  "box-inbox": "inbox", "box-drafts": "note", "box-sent": "send", "box-deleted": "trash", "box-archive": "archive", "box-junk": "warn",
  allmail: "inbox", year: "calendar", calendar: "calendar", contacts: "people", tasks: "task", notes: "note",
  "type-mail": "mail", "type-calendar": "calendar", "type-contact": "people", "type-task": "task", "type-note": "note", "type-attach": "clip"};
function folderIcon(f) { if (f.kind === "store" && f.name === "All mail") return "inbox"; if (FICON[f.kind]) return FICON[f.kind]; const n = f.name.toLowerCase();
  return n.includes("inbox") ? "inbox" : n.includes("sent") ? "send" : n.includes("deleted") || n.includes("trash") ? "trash" : "folder"; }
const collapsed = new Set();
function matches(f) { return S.module === "mail" ? (f.view || "mail") === "mail" : f.view === S.module; }
function hasMatch(f) { return matches(f) || f.children.some(hasMatch); }
function renderTree() {
  const out = [];
  const walk = (s, f, depth) => {
    if ((depth > 0 || s.id === "all") && !hasMatch(f)) return;
    const key = s.id + ":" + f.id, kids = f.children.length, isRoot = depth === 0;
    const on = S.cur && S.cur.sid === s.id && S.cur.fid === f.id;
    // like a mail program: unread mail in bold blue, otherwise a quiet item count (none on accounts and groups)
    const quiet = ["account", "group", "store"].includes(f.kind);
    const cnt = f.unread ? `<span class="cnt unread">${f.unread}</span>` : f.count && !quiet ? `<span class="cnt">${f.count}</span>` : "";
    out.push(`<div class="node ${f.kind} ${isRoot ? "store" : ""} ${on ? "on" : ""} ${f.unread ? "hasunread" : ""}" data-sid="${s.id}" data-fid="${f.id}" ${f.path ? `data-path="${esc(f.path)}"` : ""} style="padding-left:${8 + depth * 14}px" title="${esc(f.error || f.name)}">
      <span class="tw" data-tw="${key}">${kids ? (collapsed.has(key) ? "▷" : "◢") : ""}</span>${f.kind === "account" ? "" : icon(folderIcon(f))}<span class="lbl">${esc(f.name)}</span>
      ${f.error ? `<span class="err">${icon("warn")}</span>` : ""}${cnt}
      ${isRoot && s.id !== "all" ? `<button class="x" data-closesrc="${s.id}" title="Close">${icon("close")}</button>` : ""}</div>`);
    if (!collapsed.has(key)) f.children.forEach(c => walk(s, c, depth + 1));
  };
  // All mail shows its accounts as the top level, like the accounts in a mail program
  S.sources.forEach(s => s.id === "all" ? s.tree.children.forEach(c => walk(s, c, 0)) : walk(s, s.tree, 0));
  const lib = S.module === "mail" ? libraryHtml() : "";
  $("#tree").innerHTML = (S.sources.length ? out.join("") :
    `<div style="padding:20px;color:var(--muted);text-align:center">No mail yet.<br><br><button class="btn" data-act="open">Open data file</button></div>`) +
    (lib ? `<div class="libsep"></div>` + lib : "");
  const avail = new Set(["mail"]); const scan = f => { if (f.view) avail.add(f.view); f.children.forEach(scan); }; S.sources.forEach(s => scan(s.tree));
  $("#modnav").innerHTML = MODULES.map(([k, ic, l]) => `<button data-mod="${k}" title="${l}" class="${S.module === k ? "on" : ""}" ${avail.has(k) ? "" : 'style="opacity:.35"'}>${icon(ic)}</button>`).join("");
}
$("#modnav").onclick = e => { const b = e.target.closest("[data-mod]"); if (b) setModule(b.dataset.mod); };
function setModule(mod) {
  S.module = mod; S.preview = null;
  S.sort = mod === "contacts" || mod === "notes" ? "subject" : mod === "tasks" ? "due" : "date"; S.desc = mod === "mail";
  renderTree();
  for (const s of S.sources) { const stack = [s.tree]; while (stack.length) { const f = stack.shift();
    if (f !== s.tree && matches(f)) return selectFolder(s.id, f.id); stack.push(...f.children); } }
  S.cur = null; S.rows = []; renderRows(); renderRead();
}
$("#tree").oncontextmenu = e => {
  const fp = e.target.closest(".node[data-path]");
  if (fp) {             // Windows app: a mailbox file you opened can be taken out of your mail again
    e.preventDefault(); document.querySelectorAll(".ctx").forEach(c => c.remove());
    const c = document.createElement("div"); c.className = "ctx"; c.style.left = e.clientX + "px"; c.style.top = e.clientY + "px";
    c.innerHTML = `<button data-l="forget">${icon("trash")}Remove from MailVault</button>`;
    c.onclick = async () => { c.remove();
      try { await api("/api/collection/forget", {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify({path: fp.dataset.path})});
        toast("Removed. The file itself is untouched."); setTimeout(poll, 1500); } catch (er) { toast(er.message); } };
    mvHost().append(c); return;
  }
  const n = e.target.closest(".node.lib[data-root]"); if (!n) return;
  e.preventDefault(); document.querySelectorAll(".ctx").forEach(c => c.remove());
  const f = S.library.folders.find(x => x.id === n.dataset.lib); if (!f) return;
  const c = document.createElement("div"); c.className = "ctx"; c.style.left = e.clientX + "px"; c.style.top = e.clientY + "px";
  c.innerHTML = `<button data-l="openall">${icon("open")}Open all mail files</button><button data-l="rescan">${icon("refresh")}Rescan</button>` +
    (f.editable ? `<hr><button data-l="edit">${icon("note")}Rename${(f.kind === "server" || f.mapped) && (S.library.admin || S.library.nc_admin) && ["nextcloud","exapp"].includes(S.config.host) ? " and visibility" : ""}</button><button data-l="remove">${icon("trash")}Remove from library</button>` : "");
  c.onclick = async ev => { const b = ev.target.closest("[data-l]"); if (!b) return; c.remove();
    try {
      if (b.dataset.l === "openall") await openAll(f, "");
      if (b.dataset.l === "rescan") { await scanFolder(f.id, true); renderTree(); }
      if (b.dataset.l === "edit") editFolderDialog(f);
      if (b.dataset.l === "remove") { await api(`/api/library/${f.id}`, {method: "DELETE"}); delete S.libScan[f.id]; await loadLibrary(); toast(`Removed ${f.label} from the library`); }
    } catch (er) { toast(er.message); } };
  mvHost().append(c);
};
$("#tree").onclick = async e => {
  if (e.target.closest("[data-libadd]")) return addFolderDialog();
  const lf = e.target.closest(".node.libfile");
  if (lf) { const f = S.library.folders.find(x => x.id === lf.dataset.lib);
    status("Opening " + lf.dataset.name + "…");
    try { addSource(await api(`/api/library/${f.id}/open`, {method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify(lf.dataset.fileid ? {fileId: +lf.dataset.fileid} : {path: lf.dataset.path})})); } catch (er) { toast(er.message); status(); }
    return; }
  const ln = e.target.closest(".node.lib");
  if (ln) { const key = ln.dataset.key;
    if (S.libOpen.has(key)) S.libOpen.delete(key); else { S.libOpen.add(key); if (!S.libScan[ln.dataset.lib]) { renderTree(); try { await scanFolder(ln.dataset.lib); } catch (er) { toast(er.message); } } }
    renderTree(); return; }
  const x = e.target.closest("[data-closesrc]");
  if (x) { S.cur = {sid: x.dataset.closesrc}; return act("close"); }
  const tw = e.target.closest("[data-tw]");
  if (tw && tw.textContent) { const k = tw.dataset.tw; collapsed.has(k) ? collapsed.delete(k) : collapsed.add(k); renderTree(); return; }
  const n = e.target.closest(".node"); if (n) selectFolder(n.dataset.sid, +n.dataset.fid);
};
function findFolder(f, id) { if (f.id === id) return f; for (const c of f.children) { const r = findFolder(c, id); if (r) return r; } }
function gotoKind(kind) {
  for (const s of S.sources) { const stack = [s.tree]; while (stack.length) { const f = stack.pop();
    if (f.kind === kind) return selectFolder(s.id, f.id); stack.push(...f.children); } }
  toast(kind === "recovered" ? "No recovered items in the open mailboxes" : "No orphaned items in the open mailboxes");
}
async function selectFolder(sid, fid) {
  S.cur = {sid, fid}; S.sel.clear(); S.focus = -1; S.msg = null; S.globalHits = null; S.preview = null; S.category = "";
  const s = S.sources.find(x => x.id === sid); const f = s && findFolder(s.tree, fid);
  const view = (f && f.view) || "mail"; S.view = view;
  if (f && f !== s.tree && view !== S.module) { S.module = view; S.sort = view === "contacts" || view === "notes" ? "subject" : view === "tasks" ? "due" : "date"; S.desc = view === "mail"; }
  $("#main").classList.toggle("wide", view === "calendar" || view === "notes");
  let owner = s && s.name;
  if (f && sid === "all") { let p = parentOf(s.tree, f.id); while (p && p.kind !== "account" && p !== s.tree) p = parentOf(s.tree, p.id); owner = p && p !== s.tree ? p.name : "MailVault"; }
  $("#doctitle").textContent = f ? (f === s.tree || f.kind === "account" ? f.name : `${f.name} - ${owner}`) : "MailVault";
  sortLabel(); renderTree(); renderRead(); await loadRows();
}
async function loadRows(more) {
  if (!S.cur) return;
  status("Loading…");
  const big = S.view === "calendar" || S.threads;
  const p = new URLSearchParams({sort: S.sort, desc: S.desc ? 1 : 0, q: S.q, offset: more ? S.rows.length : 0, limit: big ? 5000 : 500});
  if (S.filter === "problem") p.set("status", "damaged,orphaned,recovered");
  if (S.filter === "unread") p.set("unread", "1");
  if (S.state) p.set("state", S.state);
  if (S.category) p.set("category", S.category);
  try {
    const r = await api(`/api/sources/${S.cur.sid}/folders/${S.cur.fid}?${p}`);
    S.rows = more ? S.rows.concat(r.items) : r.items; S.total = r.total; S.categories = r.categories || [];
    $("#catbtn").hidden = !S.categories.length;
  } catch (e) { S.rows = []; S.total = 0; toast(e.message); }
  renderRows(); status();
}

/* ---------------------------------------------------------------- list */
function groupLabel(iso) {
  if (!iso) return "Undated"; const d = new Date(iso), now = new Date(); const days = Math.floor((now - d) / 864e5);
  if (d.toDateString() === now.toDateString()) return "Today"; if (days < 2) return "Yesterday";
  if (days < 7) return "This Week"; if (days < 31 && d.getMonth() === now.getMonth()) return "This Month";
  return d.toLocaleDateString(undefined, {month: "long", year: "numeric"});
}
function stateBadges(r, inSent) {
  const st = (r.states || []).filter(x => ["Replied","Replied all","Forwarded","Draft","Deleted","Archived","Junk"].includes(x)
    || (x === "Sent" && !inSent));
  return st.map(x => `<span class="st ${x.replace(" ", "")}" title="${x}">${STATE_ICON[x] ? icon(STATE_ICON[x]) : ""}${x === "Replied all" ? "All" : ["Replied","Forwarded","Sent"].includes(x) ? "" : x}</span>`).join("")
    + (r.categories || []).map(c => `<span class="cat" style="background:${catColor(c)}" title="${esc(c)}"></span>`).join("");
}
function inSentFolder() { const s = S.sources.find(x => x.id === S.cur?.sid); const f = s && findFolder(s.tree, S.cur.fid); return !!(f && (f.kind === "box-sent" || f.kind === "box-drafts" || /sent|outbox|gesendet|enviados|draft/i.test(f.name))); }
function whoCol(r, inSent) { return inSent || ((r.states || []).includes("Sent") && r.to) ? "To: " + (r.to || "") : (r.from_name || r.from); }
function threadOrder(rows) {
  if (!S.threads) return rows.map((r, i) => ({r, i, depth: 0}));
  const groups = new Map();
  rows.forEach((r, i) => { const k = r.topic || r.subject; if (!groups.has(k)) groups.set(k, []); groups.get(k).push({r, i}); });
  const out = [];
  for (const [k, g] of groups) {
    out.push({...g[0], depth: 0, head: g.length > 1, key: k, count: g.length});
    if (g.length > 1 && S.expanded.has(k)) g.forEach(x => out.push({...x, depth: 1}));
  }
  return out;
}
function renderRows() {
  const rows = S.globalHits || S.rows;
  if (!S.cur && !S.globalHits) { $("#rows").innerHTML = `<div id="welcome" style="padding-top:60px"><h2>MailVault</h2><p>Open a PST, OST, MSG, EML, MBOX file or an archive of them.</p><p><button class="btn pri" data-act="open">Open data file</button></p></div>`; return; }
  $("#listhead").style.display = S.view === "calendar" && !S.globalHits ? "none" : "";
  if (!S.globalHits && S.view === "calendar") return renderCalendar();
  if (!S.globalHits && S.view === "contacts") return renderPeople();
  if (!S.globalHits && S.view === "tasks") return renderTasks();
  if (!S.globalHits && S.view === "notes") return renderNotes();
  const inSent = !S.globalHits && inSentFolder();
  let last = null, h = "";
  const cols = S.layout === "columns";
  if (cols) h += `<div class="colhdr"><span>!</span><span>${icon("clip")}</span><span data-sort="from">${inSent ? "To" : "From"}</span><span data-sort="subject">Subject</span><span data-sort="date">Received</span><span data-sort="size">Size</span><span>State</span></div>`;
  for (const {r, i, depth, head, key, count} of threadOrder(rows)) {
    if (!cols && !S.threads && S.sort === "date") { const g = groupLabel(r.date); if (g !== last) { h += `<div class="grouphdr">${esc(g)}</div>`; last = g; } }
    const tag = r.status !== "ok" ? `<span class="tag ${r.status}">${r.status}</span>` : "";
    const unread = r.unread || (r.states || []).includes("Unread");
    const tw = head ? `<span class="tw2" data-thread="${esc(key)}">${S.expanded.has(key) ? "▾" : "▸"}</span>` : "";
    const subj = esc(r.subject) + (head ? ` <span style="color:var(--muted)">(${count})</span>` : "") +
      (S.globalHits ? ` <span style="color:var(--muted)">· ${esc(r.folder)}</span>` : "");
    if (cols) {
      h += `<div class="crow ${unread ? "unread" : ""} ${S.sel.has(i) ? "on" : ""} ${depth ? "tchild" : ""}" data-i="${i}">
        <span>${r.importance === "high" ? '<span class="imp">!</span>' : ""}</span><span>${r.attachments ? icon("clip") : ""}</span>
        <span title="${esc(r.from)}">${tw}${esc(whoCol(r, inSent))}</span><span>${subj}</span><span>${esc(fmtDate(r.date))}</span>
        <span>${r.size ? fmtSize(r.size) : ""}</span><span>${tag}${stateBadges(r, inSent)}</span></div>`;
    } else {
      h += `<div class="row ${unread ? "unread" : ""} ${S.sel.has(i) ? "on" : ""} ${depth ? "tchild" : ""}" data-i="${i}">
        <div class="bar"></div><div class="from" title="${esc(r.from)}">${tw}${esc(whoCol(r, inSent))}</div>
        <div class="meta">${stateBadges(r, inSent)}${r.invite || (r.kind === "appointment" && S.view === "mail") ? `<span class="invm" title="${esc(r.invite ? r.invite.label : "Meeting request")}">${icon("calendar")}</span>` : ""}${r.importance === "high" ? '<span class="imp">!</span>' : ""}${r.attachments ? icon("clip") : ""}${r.flagged ? icon("flag") : ""}${tag}</div>
        <div class="subj">${subj}</div><div class="dt">${fmtDate(r.date)}</div></div>`;
    }
  }
  if (!S.globalHits && S.rows.length < S.total) h += `<div style="text-align:center;padding:10px"><button class="btn" id="more">Load more (${S.total - S.rows.length})</button></div>`;
  $("#rows").innerHTML = h || `<div style="padding:30px;text-align:center;color:var(--muted)">${S.q ? "No items match your search." : "We didn't find anything to show here."}</div>`;
  const mb = $("#more"); if (mb) mb.onclick = () => loadRows(true);
}
const mvHost = () => document.getElementById("mailvault-root") || document.body; // dialogs and menus live inside the viewer (scoped styles in Nextcloud)
function sameDay(a, b) { return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); }
const inviteCancelled = v => !!v && (v.method === "CANCEL" || v.status === "CANCELLED");
function inviteWhen(ir) {
  let s0 = new Date(ir.start), e0 = ir.end ? new Date(ir.end) : null;
  if (isNaN(s0)) return "";
  if (ir.all_day) { s0 = new Date(s0.getUTCFullYear(), s0.getUTCMonth(), s0.getUTCDate());
    const last = e0 ? new Date(e0.getUTCFullYear(), e0.getUTCMonth(), e0.getUTCDate() - 1) : s0;
    const f = {weekday: "long", month: "long", day: "numeric", year: "numeric"};
    return s0.toLocaleDateString(undefined, f) + (last > s0 ? " – " + last.toLocaleDateString(undefined, f) : "") + " · All day"; }
  const same = e0 && sameDay(s0, e0);
  return s0.toLocaleString(undefined, {weekday: "long", month: "long", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit"}) +
    (e0 ? " – " + (same ? e0.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"})
      : e0.toLocaleString(undefined, {weekday: "long", month: "long", day: "numeric", hour: "numeric", minute: "2-digit"})) : "");
}
function inviteCard(m) {
  const ir = m.item_raw || {}, v = ir.invite; if (!v || !ir.start) return "";
  const inCal = S.view === "calendar";
  const resp = {ACCEPTED: "accepted", DECLINED: "declined", TENTATIVE: "tentative", DELEGATED: "delegated"};
  const att = (v.attendees || []).map(a => esc(a) + (v.responses && v.responses[a] ? ` <span class="sub">(${resp[v.responses[a]] || v.responses[a].toLowerCase()})</span>` : "")).join("; ") +
    (v.more_attendees ? ` and ${v.more_attendees} more` : "");
  const rows = [["When", esc(inviteWhen(ir))], ["Location", esc(ir.location || "")], ["Organizer", esc(v.organizer || "")],
    ["Attendees", att], ["Repeats", v.rrule ? esc(v.rrule.replace(/^FREQ=/, "").toLowerCase().replace(/;/g, ", ")) : ""]].filter(r => r[1]);
  return `<div class="invite ${inviteCancelled(v) ? "cancel" : ""}"><div class="ih">${icon("calendar")}<b>${esc(v.label)}</b>${v.summary && v.summary !== m.subject ? `<span class="sub">· ${esc(v.summary)}</span>` : ""}
    <span class="ib">${inCal ? "" : `<button class="btn" data-showcal="1">${icon("calendar")} Show in calendar</button>`}${v.attachment != null ? `<button class="btn" data-att-save="${v.attachment}">${icon("down")} .ics</button>` : ""}</span></div>
    <table class="fields">${rows.map(([k, x]) => `<tr><td>${k}</td><td>${x}</td></tr>`).join("")}</table></div>`;
}
async function showInCalendar() {
  const ref = S.msgRef, ir = (S.msg && S.msg.item_raw) || {}; if (!ref) return;
  const s = S.sources.find(x => x.id === ref.sid); if (!s) return;
  const inv = (s.tree.children || []).find(c => c.kind === "invites");
  if (!inv) return toast("No calendar for this mailbox");
  let month = null;
  const d = new Date(ir.start);
  if (!isNaN(d)) { const dd = ir.all_day ? new Date(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) : d; month = new Date(dd.getFullYear(), dd.getMonth(), 1); }
  await selectFolder(ref.sid, inv.id);
  if (month) S.calMonth = month;
  const i = S.rows.findIndex(r => r.fid === ref.fid && r.idx === ref.idx);
  if (i >= 0) { S.occ = null; pick(i, {keepOcc: true}); }
  renderRows();
}
function renderCalendar() {
  // All-day items are stored as UTC midnight: read them as calendar dates, not instants
  const floating = d => new Date(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  const evs = [];
  S.rows.forEach((r, i) => {
    const occ = r.occurrences && r.occurrences.length ? r.occurrences : [[r.start || r.date, r.end || r.start || r.date]];
    occ.forEach(([os, oe, osub, oloc, oex], k) => {
      let s0 = new Date(os), e0 = new Date(oe || os);
      if (r.all_day) { s0 = floating(s0); e0 = floating(e0); }
      if (!isNaN(s0)) evs.push({r: osub || oloc ? {...r, subject: osub || r.subject, location: oloc || r.location} : r, i, s: s0, e: e0,
        rec: !!r.recurring, k: r.occurrences && r.occurrences.length ? k : null, ex: oex ?? null});
    });
  });
  if (!S.calMonth) { const latest = evs.length ? new Date(Math.max(...evs.filter(x => !x.rec).map(x => x.s).concat(evs.length ? [evs[0].s] : []))) : new Date(); S.calMonth = new Date(latest.getFullYear(), latest.getMonth(), 1); }
  const m0 = S.calMonth, first = new Date(m0); first.setDate(1 - ((m0.getDay() + 6) % 7));
  let h = `<div class="cal"><div class="calbar"><button data-cal="-1" aria-label="Previous month">${icon("left")}</button><button data-cal="1" aria-label="Next month">${icon("right")}</button>
    <button data-cal="0" class="btn">Today</button><h3>${m0.toLocaleDateString(undefined, {month: "long", year: "numeric"})}</h3>
    <span style="margin-left:auto;color:var(--muted)">${S.rows.length} items</span></div><div class="month">`;
  ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"].forEach(d => h += `<div class="dow">${d}</div>`);
  const today = new Date();
  for (let k = 0; k < 42; k++) {
    const d = new Date(first); d.setDate(first.getDate() + k);
    const dayEvs = evs.filter(x => { const end = x.r.all_day ? new Date(x.e - 1) : x.e; return d >= new Date(x.s.getFullYear(), x.s.getMonth(), x.s.getDate()) && d <= end; })
      .sort((a, b) => (b.r.all_day ? 1 : 0) - (a.r.all_day ? 1 : 0) || a.s - b.s);
    h += `<div class="day ${d.getMonth() !== m0.getMonth() ? "other" : ""} ${sameDay(d, today) ? "today" : ""}"><div class="n">${d.getDate()}</div>` +
      dayEvs.slice(0, 4).map(x => `<div class="ev ${x.r.all_day ? "allday" : ""} ${x.r.busy === "out of office" ? "oof" : ""} ${x.ex !== null ? "exc" : ""} ${x.r.invite ? "inv" : ""} ${inviteCancelled(x.r.invite) ? "cancel" : ""} ${S.sel.has(x.i) && (S.occ?.k ?? null) === x.k ? "on" : ""}" data-ev="${x.i}" data-occ="${x.k ?? ""}" data-s="${x.s.toISOString()}" data-e="${x.e.toISOString()}" data-ex="${x.ex ?? ""}" title="${esc(x.r.subject)}${x.r.location ? " · " + esc(x.r.location) : ""}${x.rec ? " · recurring" : ""}${x.r.invite ? " · " + esc(x.r.invite.label) : ""}${x.r.folder ? " · in " + esc(x.r.folder) : ""}">${x.rec ? "↻ " : ""}${x.r.all_day ? "" : x.s.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"}) + " "}${esc(x.r.subject)}</div>`).join("") +
      (dayEvs.length > 4 ? `<div class="sub" style="font-size:11px;color:var(--muted)">+${dayEvs.length - 4} more</div>` : "") + `</div>`;
  }
  $("#rows").innerHTML = h + `</div></div>`;
}
function renderPeople() {
  const h = S.rows.map((r, i) => `<div class="prow ${S.sel.has(i) ? "on" : ""}" data-i="${i}"><div class="avatar" style="background:${color(r.subject)}">${esc(initials(r.subject))}</div>
    <div style="min-width:0"><div>${esc(r.subject)}</div><div class="sub">${esc([r.job_title, r.company].filter(Boolean).join(", "))}</div><div class="sub">${esc(r.email || r.phone || "")}</div></div></div>`).join("");
  $("#rows").innerHTML = h || `<div style="padding:30px;text-align:center;color:var(--muted)">No contacts here.</div>`;
}
function renderTasks() {
  const now = new Date();
  const h = S.rows.map((r, i) => { let due = r.due ? new Date(r.due) : null; if (due) due = new Date(due.getUTCFullYear(), due.getUTCMonth(), due.getUTCDate());
    const over = due && !r.complete && due < now;
    return `<div class="trow ${r.complete ? "done" : ""} ${over ? "overdue" : ""} ${S.sel.has(i) ? "on" : ""}" data-i="${i}">
      <span>${icon(r.complete ? "task" : "task")}</span><div><div class="ts">${esc(r.subject)}</div><div class="sub" style="font-size:11.5px;color:var(--muted)">${esc(r.task_status || "")}</div></div>
      <span class="td">${due ? "Due " + due.toLocaleDateString() : ""}</span><div class="pct" title="${r.percent || 0}% complete"><i style="width:${r.percent || 0}%"></i></div></div>`; }).join("");
  $("#rows").innerHTML = h || `<div style="padding:30px;text-align:center;color:var(--muted)">No tasks here.</div>`;
}
function renderNotes() {
  $("#rows").innerHTML = `<div class="notes">` + S.rows.map((r, i) => `<div class="ntile ${r.color || "yellow"} ${S.sel.has(i) ? "on" : ""}" data-i="${i}">${esc(r.subject)}</div>`).join("") + `</div>`;
}
$("#rows").onclick = e => {
  const th = e.target.closest("[data-thread]"); if (th) { const k = th.dataset.thread; S.expanded.has(k) ? S.expanded.delete(k) : S.expanded.add(k); renderRows(); return; }
  const so = e.target.closest("[data-sort]"); if (so) return setSort(so.dataset.sort);
  const cn = e.target.closest("[data-cal]"); if (cn) { const d = +cn.dataset.cal; const m = S.calMonth;
    S.calMonth = d === 0 ? new Date(new Date().getFullYear(), new Date().getMonth(), 1) : new Date(m.getFullYear(), m.getMonth() + d, 1); renderRows(); return; }
  const ev = e.target.closest("[data-ev]");
  if (ev) { S.occ = ev.dataset.occ === "" ? null : {k: +ev.dataset.occ, start: ev.dataset.s, end: ev.dataset.e,
                                                     ex: ev.dataset.ex === "" ? null : +ev.dataset.ex};
    pick(+ev.dataset.ev, {keepOcc: true}); renderRows(); return; }
  const r = e.target.closest("[data-i]"); if (!r) return; pick(+r.dataset.i, e); };
$("#rows").ondblclick = e => { const r = e.target.closest(".row,.crow"); if (r) act("print"); };
$("#rows").oncontextmenu = e => {
  const r = e.target.closest("[data-i]"); if (!r) return; e.preventDefault();
  if (!S.sel.has(+r.dataset.i)) pick(+r.dataset.i, {});
  document.querySelectorAll(".ctx").forEach(c => c.remove());
  const c = document.createElement("div"); c.className = "ctx"; c.style.left = e.clientX + "px"; c.style.top = e.clientY + "px";
  c.innerHTML = `<button data-act="save-pdf">${icon("pdf")}Save as PDF</button><button data-act="save-msg">${icon("mail")}Save as MSG</button>
    <button data-act="save-eml">${icon("mail")}Save as EML</button><hr>
    <button data-act="export:pdf-combined" data-scope="selected">${icon("pdf")}Export selected to one PDF</button>
    <button data-act="export:mbox" data-scope="selected">${icon("db")}Export selected to MBOX</button>
    <button data-act="export:pst" data-scope="selected">${icon("db")}Export selected to PST</button>
    <button data-act="save-atts">${icon("clip")}Save attachments</button><hr>
    <button data-act="headers">${icon("code")}Message headers</button><button data-act="props">${icon("list")}Properties</button>
    <button data-act="print">${icon("print")}Print</button>`;
  c.onclick = ev => { const b = ev.target.closest("[data-scope]"); if (b) { ev.stopPropagation(); exportBatch(b.dataset.act.slice(7), "selected"); c.remove(); } };
  mvHost().append(c);
};
$("#rows").onkeydown = e => {
  const n = (S.globalHits || S.rows).length;
  if (e.key === "ArrowDown" && S.focus < n - 1) { pick(S.focus + 1, e); e.preventDefault(); }
  if (e.key === "ArrowUp" && S.focus > 0) { pick(S.focus - 1, e); e.preventDefault(); }
  if ((e.ctrlKey || e.metaKey) && e.key === "a") { e.preventDefault(); S.sel = new Set([...Array(n).keys()]); renderRows(); status(); }
};
function pick(i, e) {
  if (!e.keepOcc) S.occ = null;
  if (e.shiftKey && S.focus >= 0) { const [a, b] = [Math.min(S.focus, i), Math.max(S.focus, i)]; for (let k = a; k <= b; k++) S.sel.add(k); }
  else if (e.ctrlKey || e.metaKey) { S.sel.has(i) ? S.sel.delete(i) : S.sel.add(i); }
  else { S.sel = new Set([i]); }
  S.focus = i;
  document.querySelectorAll("#rows [data-i]").forEach(r => r.classList.toggle("on", S.sel.has(+r.dataset.i)));
  document.querySelectorAll("#rows [data-ev]").forEach(r => r.classList.toggle("on", S.sel.has(+r.dataset.ev)));
  const el = $(`#rows [data-i="${i}"]`); if (el) el.scrollIntoView({block: "nearest"});
  loadMessage(i); status();
}
function rowRef(i) { const r = (S.globalHits || S.rows)[i]; return S.globalHits ? {sid: r.sid, fid: r.fid, idx: r.idx} : {sid: r.sid ?? S.cur.sid, fid: r.fid ?? S.cur.fid, idx: r.idx}; }
function msgUrl(ref = S.msgRef) { return `/api/sources/${ref.sid}/folders/${ref.fid}/${ref.idx}`; }
let loadSeq = 0;
async function loadMessage(i) {
  const seq = ++loadSeq, ref = rowRef(i);
  try { const m = await api(msgUrl(ref)); if (seq !== loadSeq) return; S.msg = m; S.msgRef = ref; S.preview = null; renderRead();
    const row = (S.globalHits || S.rows)[i]; if (row) row.unread = false; $(`#rows [data-i="${i}"]`)?.classList.remove("unread"); }
  catch (e) { toast(e.message); }
  $("#main").classList.add("reading");
}

/* ------------------------------------------------------------- reading */
function stateLine(m) {
  const parts = (m.states || []).map(x => `<span class="st ${x.replace(" ", "")}">${STATE_ICON[x] ? icon(STATE_ICON[x]) : ""}${x}</span>`);
  if (m.archive_date) parts.push(`<span class="s">archived ${esc(new Date(m.archive_date).toLocaleDateString())}</span>`);
  const cats = (m.categories || []).map(c => `<span class="chip c" style="background:${catColor(c)}">${esc(c)}</span>`).join("");
  return parts.length || cats ? `<div class="chips">${parts.join("")}${cats}</div>` : "";
}
function attachStrip(m) {
  const atts = m.attachments.filter(a => !a.inline);
  return atts.length ? `<div class="atts">${atts.map(a => `<div class="att" data-att="${a.i}" title="${esc(a.name)}${a.preview ? " — click to preview" : ""}">${icon(a.embedded ? "mail" : /\.(zip|7z|rar|gz)$/i.test(a.name) ? "zip" : a.preview === "image" ? "eye" : "clip")}
      <div><div class="an">${esc(a.name)}</div><div class="as">${a.embedded ? "Outlook item" : fmtSize(a.size)}${a.preview ? " · preview" : ""}</div></div></div>`).join("")}</div>` : "";
}
const bodyFrame = () => `<iframe id="bodyframe" sandbox="allow-popups allow-popups-to-escape-sandbox" src="${API}${msgUrl()}/body?remote=${S.remote ? 1 : 0}"></iframe>`;
const DATE_FIELDS = {Start: "start", End: "end", Due: "due", Birthday: "birthday"};
function localField(m, k, v) {
  const raw = (m.item_raw || {})[DATE_FIELDS[k]]; if (!raw) return v;
  let d = new Date(raw); if (isNaN(d)) return v;
  const dateOnly = k === "Birthday" || k === "Due" || (m.item_raw || {}).all_day || (m.kind === "task" && k === "Start");
  if (dateOnly) { d = new Date(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
    return d.toLocaleDateString(undefined, {weekday: k === "Birthday" ? undefined : "long", year: "numeric", month: "long", day: "numeric"}); }
  return d.toLocaleString(undefined, {weekday: "long", year: "numeric", month: "long", day: "numeric", hour: "numeric", minute: "2-digit"});
}
function fieldsTable(m) { return m.item && m.item.length ? `<table class="fields">${m.item.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(localField(m, k, v))}</td></tr>`).join("")}</table>` : ""; }
function renderRead() {
  const m = S.msg, el = $("#read");
  if (!m) { el.innerHTML = `<div class="empty">${icon("mail").replace('class="i"', 'class="i" style="width:64px;height:64px;stroke-width:1;color:#bbb"')}<p>Select an item to read</p></div>`; return; }
  if (S.preview !== null) {
    const a = m.attachments[S.preview];
    el.innerHTML = `<div class="pvbar"><button class="btn" data-pv="back">${icon("left")} Back to item</button><b style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.name)}</b>
      ${a.preview === "message" ? `<button class="btn" data-pv="open">Open in MailVault</button>` : ""}<button class="btn" data-pv="save">${icon("down")} Save</button></div>
      <iframe id="pvframe" sandbox="${a.preview === "pdf" || a.preview === "office" ? "allow-scripts allow-same-origin" : ""}" src="${API}${msgUrl()}/attachments/${a.i}/preview"></iframe>`;
    return;
  }
  const info = [];
  if (m.status === "recovered") info.push(["", "This item was recovered from deleted or unreferenced space in the data file."]);
  if (m.status === "orphaned") info.push(["", "This is an orphaned item: its folder is missing or no longer lists it."]);
  if (m.status === "damaged") info.push(["bad", "This item is damaged. MailVault showed everything it could read."]);
  if (m.problems.length) info.push([m.status === "damaged" ? "bad" : "", "Details: " + m.problems.slice(0, 4).map(esc).join("; ")]);
  if (m.has_html && !S.remote) info.push(["", `Remote pictures were blocked to protect your privacy. <a data-act="remote">Download pictures</a>`]);
  if (m.importance === "high") info.push(["", "This message was sent with High importance."]);
  const infos = info.map(([c, t]) => `<div class="info ${c}">${t}</div>`).join("");
  const actions = `<div class="act"><button data-act="save-pdf" title="Save as PDF">${icon("pdf")}PDF</button>${
    m.kind === "appointment" || m.kind === "task" ? `<button data-act="save-ics" title="Save as ICS">${icon("calendar")}ICS</button>` :
    m.kind === "contact" ? `<button data-act="save-vcf" title="Save as vCard">${icon("people")}VCF</button>` :
    `<button data-act="save-msg" title="Save as MSG">${icon("down")}MSG</button>`}<button data-act="print" title="Print">${icon("print")}</button></div>`;
  const ir = m.item_raw || {};
  if (m.kind === "contact") {
    el.innerHTML = `<div class="card"><div class="avatar" style="background:${color(m.subject)}">${esc(initials(m.subject))}</div>
      <div style="flex:1"><h1>${esc(m.subject || "(no name)")}</h1><div class="sub">${esc([ir.job_title, ir.company].filter(Boolean).join(", "))}</div>
      ${ir.email ? `<div><a href="mailto:${esc(ir.email)}" style="color:var(--blue)">${esc(ir.email)}</a></div>` : ""}</div>${actions}</div>
      ${infos}${fieldsTable(m)}${attachStrip(m)}${bodyFrame()}`;
    return;
  }
  if (m.kind === "note") {
    el.innerHTML = `${infos}<div class="bignote ${ir.color || "yellow"}">${esc(m.subject)}</div>`;
    api(`${msgUrl()}/export?fmt=txt`).then(r => r.text()).then(t => { const b = $(".bignote", el); if (b) b.textContent = t.split("\n\n").slice(1).join("\n\n") || m.subject; }).catch(() => {});
    return;
  }
  let when = "";
  const occ = m.kind === "appointment" && S.occ ? S.occ : null;
  const exd = occ && occ.ex !== null ? (ir.exceptions || [])[occ.ex] : null;
  if (occ) {
    const s0 = new Date(occ.start), e0 = new Date(occ.end);
    when = s0.toLocaleString(undefined, {weekday: "long", month: "long", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit"}) +
      " – " + e0.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"});
  } else if (m.kind === "appointment" && ir.start) {
    let s0 = new Date(ir.start), e0 = ir.end ? new Date(ir.end) : null;
    if (ir.all_day) s0 = new Date(s0.getUTCFullYear(), s0.getUTCMonth(), s0.getUTCDate());
    when = ir.all_day ? s0.toLocaleDateString(undefined, {weekday: "long", month: "long", day: "numeric", year: "numeric"}) + " · All day"
      : s0.toLocaleString(undefined, {weekday: "long", month: "long", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit"}) +
        (e0 ? " – " + e0.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"}) : "");
  }
  const hdr = m.kind === "mail" ? `<div class="who"><div class="avatar" style="background:${color(m.from)}">${esc(initials(m.from))}</div>
      <div><div class="n">${esc(m.from || "(no sender)")}</div>
        <div class="s">${m.to ? "To: " + esc(m.to) : ""}${m.cc ? "<br>Cc: " + esc(m.cc) : ""}${m.bcc ? "<br>Bcc: " + esc(m.bcc) : ""}</div>
        <div class="s">${esc(fmtDate(m.date, true))}</div>${stateLine(m)}</div>${actions}</div>`
    : `${when ? `<div class="when">${icon("calendar")} ${esc(when)}</div>` : ""}${stateLine(m)}`;
  const title = `<h1>${m.kind === "task" && ir.complete ? "✓ " : ""}${esc((exd && exd.subject) || m.subject || "(no subject)")}</h1>`;
  const occInfo = occ ? `<div class="info">${exd ? `This occurrence was changed. It was originally on ${esc(new Date(exd.original).toLocaleString(undefined, {weekday: "long", month: "long", day: "numeric", hour: "numeric", minute: "2-digit"}))}.`
    : `One occurrence of a recurring meeting: ${esc(ir.recurrence || "recurring")}.`} <a data-occ-series="1">Open the series</a></div>` : "";
  const occFields = exd ? `<table class="fields">${[["When", when], ["Location", exd.location || ir.location || ""],
      ["Attendees", (exd.attendees || []).join("; ")], ["Series", ir.recurrence || ""]].filter(r => r[1])
      .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}</table>` : null;
  const curF = (() => { const s = S.sources.find(x => x.id === S.cur?.sid); return s && findFolder(s.tree, S.cur.fid); })();
  const calLink = m.kind === "appointment" && ir.start && curF && curF.view !== "calendar"
    ? `<div class="info">This meeting request is in ${esc(curF.name)}. <a data-showcal="1">Show it in the calendar</a></div>` : "";
  el.innerHTML = `<div class="rh">${m.kind === "mail" ? title : `<div style="display:flex;gap:8px;align-items:flex-start">${title}${actions}</div>`}${hdr}</div>
    ${m.kind === "mail" ? inviteCard(m) : calLink}${occInfo}${infos}${m.kind !== "mail" ? (occFields ?? fieldsTable(m)) : ""}${attachStrip(m)}${exd
      ? `<iframe id="bodyframe" sandbox="allow-popups allow-popups-to-escape-sandbox" src="${API}${msgUrl()}/body?remote=${S.remote ? 1 : 0}&exc=${occ.ex}"></iframe>`
      : bodyFrame()}`;
}
$("#read").onclick = e => {
  if (e.target.closest("[data-occ-series]")) { S.occ = null; renderRead(); renderRows(); return; }
  if (e.target.closest("[data-showcal]")) { showInCalendar().catch(er => toast(er.message)); return; }
  const as = e.target.closest("[data-att-save]"); if (as) { download(`${msgUrl()}/attachments/${as.dataset.attSave}`); return; }
  const pv = e.target.closest("[data-pv]");
  if (pv) { const i = S.preview;
    if (pv.dataset.pv === "back") { S.preview = null; renderRead(); }
    if (pv.dataset.pv === "save") download(`${msgUrl()}/attachments/${i}`);
    if (pv.dataset.pv === "open") openAttachment(i).catch(er => toast(er.message));
    return; }
  const a = e.target.closest("[data-att]"); if (!a) return;
  const at = S.msg.attachments[+a.dataset.att];
  if (at.preview) { S.preview = at.i; renderRead(); return; }
  if (/\.(mbox|pst|ost|zip|tar|gz|7z|rar)$/i.test(at.name)) {
    dialog(at.name, `<p>What do you want to do with this attachment?</p>`, [
      ["Open in MailVault", "pri", () => openAttachment(at.i).then(() => true)],
      ["Save", "", () => { download(`${msgUrl()}/attachments/${at.i}`); return true; }]]);
  } else download(`${msgUrl()}/attachments/${at.i}`);
};
async function openAttachment(i) { addSource(await api(`${msgUrl()}/attachments/${i}/open`, {method: "POST"})); }

/* ----------------------------------------------------------- downloads */
async function download(url, opt) {
  status("Preparing download…");
  try {
    const r = await api(url, opt); const blob = await r.blob();
    const cd = r.headers.get("content-disposition") || "";
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i), m2 = cd.match(/filename="?([^";]+)"?/i);
    const name = m ? decodeURIComponent(m[1]) : m2 ? m2[1] : "download";
    await window.MV_SAVE(name, blob);
    status("Saved " + name);
  } catch (e) { toast(e.message); status(); }
}
async function exportBatch(fmt, scopeOverride) {
  if (!S.cur && !S.globalHits) return toast("Open a mailbox first");
  const scope = scopeOverride || S.scope;
  if (S.globalHits && scope === "selected") return toast("Select items inside a folder to export them");
  if (S.cur && S.cur.sid === "all") return toast("To export, open the file from More views > Files, or right-click a message and save it");
  const items = [...S.sel].map(i => S.rows[i]?.idx).filter(x => x !== undefined);
  if (scope === "selected" && !items.length) return toast("Select one or more messages");
  try {
    const j = await api("/api/export", {method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify({sid: S.cur.sid, fid: S.cur.fid, fmt, scope, items,
        statuses: S.onlyProblems ? ["damaged","orphaned","recovered"] : null, states: S.exportState ? [S.exportState] : null})});
    trackJob(j, "export");
  } catch (e) { toast(e.message); }
}

/* ---------------------------------------------------------------- jobs */
function trackJob(j, kind) { S.jobs[j.id] = {...j, kind}; renderJobs(); poll(); }
function renderJobs() {
  const js = Object.values(S.jobs);
  $("#jobs").innerHTML = js.map(j => `<div class="job ${j.state === "error" ? "err" : ""}">${icon(j.kind === "deepscan" ? "scan" : "down")}
    <span>${esc(j.title)}</span>
    ${j.state === "running" || j.state === "queued" ? `<div class="pb"><i style="width:${j.percent}%"></i></div><span>${j.state === "queued" ? "Waiting…" : j.percent + "%"}</span><a data-jcancel="${j.id}">Cancel</a>` :
      j.state === "done" ? `<span>${esc(j.message || "Done")}</span>${j.has_file ? `<a data-jget="${j.id}">Save ${esc(j.file)}</a>` : ""}<a data-jx="${j.id}">Dismiss</a>` :
      `<span>${j.state === "error" ? esc(j.error) : "Cancelled"}</span><a data-jx="${j.id}">Dismiss</a>`}</div>`).join("");
}
$("#jobs").onclick = async e => {
  const c = e.target.closest("[data-jcancel]"); if (c) { await api(`/api/jobs/${c.dataset.jcancel}`, {method: "DELETE"}).catch(() => {}); return; }
  const g = e.target.closest("[data-jget]"); if (g) return download(`/api/jobs/${g.dataset.jget}/download`);
  const x = e.target.closest("[data-jx]"); if (x) { delete S.jobs[x.dataset.jx]; renderJobs(); }
};
let pollT = null;
async function poll() {
  clearTimeout(pollT);
  let busy = false;
  try {
    const st = await api("/api/status");
    S.index = st.index || {};
    for (const j of st.jobs) {
      const old = S.jobs[j.id]; if (!old) continue;
      S.jobs[j.id] = {...j, kind: old.kind};
      if (old.state !== "done" && j.state === "done") {
        if (j.has_file) download(`/api/jobs/${j.id}/download`);
        if (old.kind === "deepscan") { await refreshSources(); toast(j.message || "Deep scan finished"); }
      }
      if (j.state === "running" || j.state === "queued") busy = true;
    }
    for (const v of Object.values(S.index)) if (v && v.state === "building") busy = true;
    if (st.collection) {
      const old = S.collection; S.collection = st.collection;
      if (st.collection.state !== "ready") busy = true;
      if (!old || old.version !== st.collection.version) await refreshCollection();
      if (st.collection.version !== S.pendingVersion || st.collection.state !== "ready") S.pendingUntil = 0;
    }
    else if (S.collection && S.sources.some(s => s.id === "all")) {     // the last file was removed
      S.collection = null; S.sources = S.sources.filter(s => s.id !== "all");
      if (S.cur && S.cur.sid === "all") { S.cur = null; S.msg = null; S.rows = []; $("#doctitle").textContent = "MailVault"; renderRows(); renderRead(); }
      renderTree();
    }
    if (S.pendingUntil > Date.now()) busy = true;     // a file was just added: watch closely until its import starts
    renderJobs(); status();
  } catch {}
  pollT = setTimeout(poll, busy ? 1200 : 60000);   // idle: look for newly imported mail once a minute
}
// All mail opens tidy: accounts and their folders open, "More views" and everything inside it folded (once per node).
const collSeen = new Set();
function foldCollection(tree) {
  const walk = (n, fold) => {
    const key = "all:" + n.id;
    if (fold && n.children.length && !collSeen.has(key)) { collSeen.add(key); collapsed.add(key); }
    n.children.forEach(c => walk(c, fold || n.kind === "group"));
  };
  for (const n of tree.children || []) walk(n, n.kind === "group");
}
function firstInbox(tree) { for (const a of tree.children || []) { const b = (a.children || []).find(x => x.kind === "box-inbox"); if (b) return b.id; } return 0; }
function parentOf(f, id) { for (const c of f.children) { if (c.id === id) return f; const r = parentOf(c, id); if (r) return r; } return null; }
async function refreshCollection() {
  const c = await api("/api/collection"); if (!c.tree) return;
  const o = S.sources.find(x => x.id === "all");
  foldCollection(c.tree);
  if (o) o.tree = c.tree; else S.sources.unshift(c);
  S.collection = c.collection; renderTree();
  // nothing picked yet, or still on the hidden top level from before any account existed: go to the first Inbox
  if (!S.cur || !S.sources.some(s => s.id === S.cur.sid) || (S.cur.sid === "all" && S.cur.fid === 0 && firstInbox(c.tree)))
    return selectFolder("all", firstInbox(c.tree));
  if (S.cur && S.cur.sid === "all" && !S.msg) loadRows();   // don't pull the list from under a message being read
}
async function refreshSources() {
  const ss = await api("/api/sources");
  for (const s of ss) { const o = S.sources.find(x => x.id === s.id); if (o) o.tree = s.tree; }
  renderTree();
}

/* ------------------------------------------------------------- dialogs */
function dialog(title, html, buttons = []) {
  const m = document.createElement("div"); m.className = "mask";
  m.innerHTML = `<div class="dlg" role="dialog" aria-label="${esc(title)}"><div class="dh">${esc(title)}<button aria-label="Close">×</button></div>
    <div class="db">${html}</div><div class="df">${buttons.map((b, i) => `<button class="btn ${b[1]}" data-b="${i}">${esc(b[0])}</button>`).join("")}<button class="btn" data-b="x">Close</button></div></div>`;
  m.onclick = async e => {
    if (e.target === m || e.target.closest(".dh button") || e.target.dataset.b === "x") return m.remove();
    const b = e.target.closest("[data-b]"); if (b) { try { if (await buttons[+b.dataset.b][2]()) m.remove(); } catch (er) { toast(er.message); } }
  };
  mvHost().append(m); return m.querySelector(".dlg");
}
async function healthDialog() {
  if (!S.cur) return toast("Open a mailbox first");
  status("Checking every item…");
  const r = await api(`/api/sources/${S.cur.sid}/health`); status();
  const st = r.stats, total = Object.values(st).reduce((a, b) => a + b, 0);
  const d = dialog("Health report", `<div class="stats">${[["Total items", total], ["OK", st.ok || 0], ["Damaged", st.damaged || 0], ["Orphaned", st.orphaned || 0], ["Recovered", st.recovered || 0]]
    .map(([k, v]) => `<div class="stat"><b>${v}</b>${k}</div>`).join("")}</div>
    ${r.problems.length ? `<table class="kv">${r.problems.map((p, i) => `<tr data-p="${i}" style="cursor:pointer"><td>${esc(p.status)}</td><td><b>${esc(p.subject)}</b> · ${esc(p.folder)}<br>${p.problems.map(esc).join("<br>")}</td></tr>`).join("")}</table>`
    : "<p>No problems found. Every item was read cleanly.</p>"}
    <h3 style="font-weight:600;font-size:13px;margin:16px 0 6px">Saved state</h3><div class="stats" style="flex-wrap:wrap">${Object.entries(r.states || {}).sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `<div class="stat"><b>${v}</b>${esc(k)}</div>`).join("") || "<span>No state information.</span>"}</div>
    ${r.is_pst && !r.deep_scanned ? `<div class="info">A deep scan searches unreferenced space for more items whose index entries are gone. It reads the whole file.</div>` : ""}`,
    [["Export problem items", "pri", () => { S.onlyProblems = true; S.scope = "all"; exportBatch("eml"); return true; }],
     ["Recovery report (PDF)", "", () => { act("report"); return true; }],
     ...(r.is_pst && !r.deep_scanned ? [["Deep scan", "", () => { act("deepscan"); return true; }]] : [])]);
  d.onclick = async e => { const tr = e.target.closest("[data-p]"); if (!tr) return; const p = r.problems[+tr.dataset.p]; if (p.idx == null) return;
    d.closest(".mask").remove(); await selectFolder(S.cur.sid, p.fid); const i = S.rows.findIndex(x => x.idx === p.idx); if (i >= 0) pick(i, {}); };
}
async function dupDialog() {
  if (!S.cur) return toast("Open a mailbox first");
  const r = await api(`/api/sources/${S.cur.sid}/duplicates`);
  dialog("Duplicate items", r.length ? `<p>${r.length} sets of duplicates (same Message-ID, or same subject, sender and time).</p><table class="kv">${
    r.map(g => `<tr><td>${g.length}×</td><td>${esc(g[0].subject)}<br>${g.map(x => esc(x.folder)).join(", ")}</td></tr>`).join("")}</table>` : "<p>No duplicates found.</p>");
}

/* ---------------------------------------------------------- search etc */
let qt;
$("#search").oninput = e => { clearTimeout(qt); qt = setTimeout(() => { S.q = e.target.value.trim(); loadRows(); }, 250); };
$("#search").onkeydown = async e => { if (e.key === "Enter" && e.shiftKey) { const r = await api(`/api/search?q=${encodeURIComponent(e.target.value)}`); S.globalHits = r.items; renderRows(); status(`${r.total} results in all mailboxes`); } };
document.querySelectorAll("#listhead .flt button[data-f]").forEach(b => b.onclick = () => setFilter(b.dataset.f));
function setFilter(f) { S.filter = S.filter === f ? "" : f; document.querySelectorAll("#listhead .flt button[data-f]").forEach(b => b.classList.toggle("on", b.dataset.f === S.filter)); loadRows(); }
function setSort(k) { if (S.sort === k) S.desc = !S.desc; else { S.sort = k; S.desc = k === "date" || k === "size"; }
  sortLabel(); loadRows(); }
function sortLabel() { const k = S.sort; $("#sortbtn").textContent = `By ${k[0].toUpperCase() + k.slice(1)} ${S.desc ? "▾" : "▴"}`; }
function dropdown(btn, items, cur, onPick) {
  const c = document.createElement("div"); c.className = "ctx"; const r = btn.getBoundingClientRect();
  c.style.left = r.left + "px"; c.style.top = r.bottom + "px";
  c.innerHTML = [["", "Any"], ...items.map(x => [x, x])].map(([v, l]) => `<button data-v="${esc(v)}">${v === cur ? "✓ " : ""}${esc(l)}</button>`).join("");
  c.onclick = ev => { const b = ev.target.closest("[data-v]"); if (b) { onPick(b.dataset.v); c.remove(); } };
  setTimeout(() => mvHost().append(c));
}
$("#statebtn").onclick = e => dropdown(e.currentTarget, STATES, S.state, v => { S.state = v; $("#statebtn").textContent = (v || "Filter") + " ▾"; $("#statebtn").classList.toggle("on", !!v); loadRows(); });
$("#catbtn").onclick = e => dropdown(e.currentTarget, S.categories || [], S.category, v => { S.category = v; $("#catbtn").textContent = (v || "Category") + " ▾"; $("#catbtn").classList.toggle("on", !!v); loadRows(); });
$("#sortbtn").onclick = e => { const c = document.createElement("div"); c.className = "ctx"; const r = e.target.getBoundingClientRect();
  c.style.left = r.left + "px"; c.style.top = r.bottom + "px"; e.stopPropagation();
  c.innerHTML = ["date","from","subject","size"].map(k => `<button data-act="sort:${k}">${k[0].toUpperCase() + k.slice(1)}</button>`).join(""); mvHost().append(c); };
function status(t) {
  const n = S.globalHits ? S.globalHits.length : S.total;
  $("#st-items").textContent = S.cur || S.globalHits ? `Items: ${n}` + (S.sel.size > 1 ? `   Selected: ${S.sel.size}` : "") : "No mailbox open";
  let ix = "";
  const src = S.sources.find(x => x.id === S.cur?.sid); const st = src && S.index[src.id];
  const cs = S.collection;
  if (cs && cs.state !== "ready" && cs.state !== "idle") ix = cs.message || "Updating All mail…";
  else if (st && st.state === "building") ix = `Indexing ${src.name}: ${Math.round(100 * st.done / Math.max(st.total, 1))}%`;
  else if (st && st.state === "ready") ix = "Search index ready";
  $("#st-right").textContent = t || ix || "All folders are up to date.";
}
document.querySelectorAll(".split").forEach(sp => sp.onmousedown = e => {
  e.preventDefault(); const v = sp.dataset.v, prev = sp.previousElementSibling, start = e.clientX, w = prev.getBoundingClientRect().width;
  const mv = ev => $("#main").style.setProperty(v, Math.max(140, w + ev.clientX - start) + "px");
  document.addEventListener("mousemove", mv); document.addEventListener("mouseup", () => document.removeEventListener("mousemove", mv), {once: true});
});
document.addEventListener("keydown", e => { if ((e.ctrlKey || e.metaKey) && e.key === "o") { e.preventDefault(); openDialog(); }
  if ((e.ctrlKey || e.metaKey) && e.key === "e") { e.preventDefault(); $("#search").focus(); } if (e.key === "Escape") document.querySelectorAll(".mask,.ctx").forEach(m => m.remove()); });

(async () => {
  ribbon(); renderTree(); renderRows();
  try { S.config = await api("/api/config"); loadLibrary(); const ss = await api("/api/sources"); ss.forEach(s => S.sources.push(s));
    const coll = ss.find(s => s.id === "all"); if (coll) { S.collection = coll.collection; foldCollection(coll.tree); if (coll.collection && coll.collection.state !== "ready") poll(); }
    renderTree(); if (ss.length) selectFolder(ss[0].id, ss[0].id === "all" ? firstInbox(ss[0].tree) : (firstWithItems(ss[0].tree) || ss[0].tree).id); } catch (e) { toast("Can't reach the MailVault engine: " + e.message); }
  if (window.MV_READY) window.MV_READY();
})();
})();
