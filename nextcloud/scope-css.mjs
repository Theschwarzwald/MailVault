// Builds the Nextcloud copy of the viewer CSS: every rule is scoped under #mailvault-root
// so it outranks Nextcloud's global form styles (button:not(.button-vue), input:not(...)),
// and a reset clears the properties Nextcloud forces on buttons and inputs.
// Usage: node scope-css.mjs ../mailvault/web/app.css mailvault/css/app.css
import { readFileSync, writeFileSync } from 'node:fs'

const [src, dst] = process.argv.slice(2)
const ROOT = '#mailvault-root'
const css = readFileSync(src, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')

const RESET = `
${ROOT}{position:relative;height:100%;width:100%;overflow:hidden;font-family:var(--font);font-size:13px;color:var(--text);background:var(--bg);line-height:normal}
${ROOT} button,${ROOT} input,${ROOT} select,${ROOT} textarea{min-height:0;min-width:0;max-height:none;height:auto;width:auto;margin:0;box-shadow:none;font:inherit;font-weight:inherit;line-height:normal;color:inherit;cursor:auto;outline-offset:0;opacity:1;filter:none}
${ROOT} button{background:none;border:0;border-radius:0;padding:1px 6px;text-align:center;cursor:pointer;display:inline-block}
${ROOT} input:not([type=checkbox]):not([type=radio]),${ROOT} select,${ROOT} textarea{background:#fff;border:1px solid #8a8a8a;border-radius:2px;padding:2px 4px}
${ROOT} input[type=checkbox],${ROOT} input[type=radio]{appearance:auto;-webkit-appearance:auto;position:static;width:auto;height:auto;clip:auto;clip-path:none;opacity:1;margin:0 4px 0 0}
${ROOT} h1,${ROOT} h2,${ROOT} h3,${ROOT} h4,${ROOT} p,${ROOT} table,${ROOT} label,${ROOT} ul{font-family:inherit;color:inherit;line-height:normal}
${ROOT} h1,${ROOT} h2,${ROOT} h3,${ROOT} h4{font-weight:600}
${ROOT} a{color:inherit}
${ROOT} :focus-visible{box-shadow:none}
`

function scopeSelector(sel) {
  sel = sel.trim()
  if (!sel) return sel
  if (sel === ':root' || sel === 'html' || sel === 'body') return ROOT
  if (/^(html|body)\b/.test(sel)) return ROOT + sel.replace(/^(html|body)/, '')
  if (sel === '*') return `${ROOT},${ROOT} *`
  return `${ROOT} ${sel}`
}

// Walk top-level blocks; recurse into @media/@supports, keep @keyframes/@font-face as they are.
function scope(text) {
  let out = ''
  let i = 0
  while (i < text.length) {
    const open = text.indexOf('{', i)
    if (open < 0) { out += text.slice(i); break }
    const head = text.slice(i, open).trim()
    let depth = 1
    let j = open + 1
    while (j < text.length && depth) { if (text[j] === '{') depth++; else if (text[j] === '}') depth--; j++ }
    const inner = text.slice(open + 1, j - 1)
    if (/^@(media|supports|container)/.test(head)) out += `${head}{${scope(inner)}}`
    else if (head.startsWith('@')) out += `${head}{${inner}}`
    else out += head.split(',').map(scopeSelector).join(',') + `{${inner}}`
    out += '\n'
    i = j
  }
  return out
}

writeFileSync(dst, '/* Generated from mailvault/web/app.css by nextcloud/scope-css.mjs: do not edit */\n' + RESET + scope(css))
console.log(`scoped ${src} -> ${dst}`)
