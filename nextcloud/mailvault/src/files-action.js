/* "Open in MailVault" for the Files app (Nextcloud 33+, @nextcloud/files v4).
   Clicking a mail file anywhere in Files (own folders, shares, group folders,
   external storage, search results, favorites, recent) opens it in MailVault.
   Built into js/files-action.js with `npm ci && npm run build`. */
import { registerFileAction, FileType, DefaultType } from '@nextcloud/files'
import { generateUrl } from '@nextcloud/router'
import { translate as t } from '@nextcloud/l10n'

const EXT = /\.(pst|ost|msg|eml|emlx|mbox|mbx)$/i
const ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect x="2" y="4.5" width="20" height="15" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 7l9 6 9-6" fill="none" stroke="currentColor" stroke-width="2"/></svg>'

const isMail = (node) => node && node.type === FileType.File && EXT.test(node.basename)
  && (node.permissions & 1) !== 0 // readable

registerFileAction({
  id: 'mailvault-open',
  displayName: () => t('mailvault', 'Open in MailVault'),
  iconSvgInline: () => ICON,
  enabled: ({ nodes }) => nodes.length === 1 && isMail(nodes[0]),
  async exec({ nodes }) {
    window.location.href = generateUrl('/apps/mailvault/') + '?fileId=' + encodeURIComponent(nodes[0].fileid)
    return null
  },
  default: DefaultType.DEFAULT, // the click action for these files, like Office documents
  order: -50,
})
