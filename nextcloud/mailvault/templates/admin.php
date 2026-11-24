<?php /** @var array $_ */ ?>
<div id="mailvault-admin" class="section">
    <h2>MailVault</h2>
    <p class="settings-hint">Connect Nextcloud to the MailVault engine container that reads PST, OST, MSG, EML and MBOX files.
        Use the same token as the container's <code>MAILVAULT_TOKEN</code> setting.</p>
    <p><label for="mv-url">Engine URL</label><br>
        <input id="mv-url" type="url" style="width:420px" placeholder="http://mailvault:8080" value="<?php p($_['engine_url']); ?>"></p>
    <p><label for="mv-token">Shared token</label><br>
        <input id="mv-token" type="password" style="width:420px" autocomplete="off"
               placeholder="<?php p($_['has_token'] ? '•••••••• (saved, leave empty to keep)' : 'Paste MAILVAULT_TOKEN'); ?>"></p>
    <p><label for="mv-pathmap">Path map (lets the engine read mail files in place instead of receiving copies)</label><br>
        <textarea id="mv-pathmap" rows="3" style="width:620px;font-family:monospace" placeholder="/var/www/html/data=/ncdata"><?php p($_['path_map']); ?></textarea><br>
        <span class="settings-hint">One line per storage: <code>path in the Nextcloud container=path in the engine container</code>.
        Mount the same folders read-only into the engine. Files on storage that isn't mapped (or when server-side
        encryption is on) are uploaded to the engine once instead.</span></p>
    <button id="mv-save" class="primary">Save and test</button> <span id="mv-msg"></span>

</div>
