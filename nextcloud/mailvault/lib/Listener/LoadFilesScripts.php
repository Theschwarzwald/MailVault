<?php
declare(strict_types=1);

namespace OCA\MailVault\Listener;

use OCA\Files\Event\LoadAdditionalScriptsEvent;
use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\Util;

/** @template-implements IEventListener<LoadAdditionalScriptsEvent> */
class LoadFilesScripts implements IEventListener {
    public function handle(Event $event): void {
        if ($event instanceof LoadAdditionalScriptsEvent) {
            Util::addInitScript('mailvault', 'files-action');
        }
    }
}
