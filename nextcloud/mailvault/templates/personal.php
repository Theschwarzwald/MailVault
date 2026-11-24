<?php /** @var array $_ */ ?>
<div id="mailvault-scan" class="section" data-admin="<?php p($_['is_admin'] ? '1' : '0'); ?>">
    <h2>MailVault scan folders</h2>
    <p class="settings-hint">
        Choose the Nextcloud folders MailVault should scan for mail files (PST, OST, MSG, EML, MBOX and archives).
        You can pick any folder you can open in Files: your own, one shared with you, a group folder or external storage.
        Subfolders are included. Every mail file found is imported into <b>All mail</b> in
        <a href="<?php p($_['app_url']); ?>">MailVault</a> and sorted automatically by date, person, type, folder and
        file, like a music library. Folders are rescanned every hour, so new mail files appear on their own.
    </p>
    <table class="mv-scan-table">
        <thead><tr><th>Folder</th><th>Name in MailVault</th><th>Mail files</th><th>Last scan</th><th></th></tr></thead>
        <tbody id="mv-scan-rows"><tr><td colspan="5" class="mv-muted">Loading…</td></tr></tbody>
    </table>
    <p id="mv-coll" class="mv-muted"></p>
    <p class="mv-scan-actions">
        <button id="mv-scan-add" class="primary">Add folder…</button>
        <button id="mv-scan-all">Scan all and update All mail</button>
        <span id="mv-scan-msg" class="mv-muted"></span>
    </p>
    <h3>Your email accounts</h3>
    <p class="settings-hint">MailVault lays your mail out like a mail program: one entry per email account, each with
        Inbox, Drafts, Sent Items, Deleted Items, Archive, Junk Email and your own folders. MailVault detects the owner of each
        mailbox; list any other addresses you use or used (old jobs, old providers), one per line.</p>
    <p><textarea id="mv-accounts" rows="4" style="width:420px;font-family:monospace" placeholder="me@example.com&#10;old.me@oldjob.com"></textarea></p>
    <p><button id="mv-accounts-save">Save accounts</button> <span id="mv-accounts-msg" class="mv-muted"></span></p>
    <p id="mv-accounts-found" class="mv-muted"></p>
    <?php if ($_['is_admin']): ?>
    <h3>Map a folder for other people</h3>
    <p class="settings-hint">As an administrator you can map one of your folders for everyone, or for specific users and
        groups. They open its mail files through your access, even if the folder isn't shared with them in Files.</p>
    <p class="mv-map-row">
        <select id="mv-map-mode">
            <option value="everyone">Everyone</option>
            <option value="admins">Administrators only</option>
            <option value="list">Specific users or groups</option>
        </select>
        <input id="mv-map-who" type="text" placeholder="alice, finance-team" style="display:none">
        <button id="mv-map-add">Map a folder…</button>
    </p>
    <?php endif; ?>
</div>
