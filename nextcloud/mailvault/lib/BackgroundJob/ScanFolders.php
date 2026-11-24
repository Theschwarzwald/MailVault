<?php
declare(strict_types=1);

namespace OCA\MailVault\BackgroundJob;

use OCA\MailVault\Service\CollectionSync;
use OCA\MailVault\Service\EngineClient;
use OCA\MailVault\Service\ScanService;
use OCP\AppFramework\Utility\ITimeFactory;
use OCP\BackgroundJob\IJob;
use OCP\BackgroundJob\TimedJob;

/**
 * Every cron run (5 minutes): rescan every mapped folder, then bring each user's mail
 * up to date, so new mail files dropped into a mapped folder show up on their own.
 * A rescan of ~10,000 files takes a couple of seconds, and the engine ignores a file
 * list that hasn't changed.
 */
class ScanFolders extends TimedJob {
    public function __construct(ITimeFactory $time, private ScanService $scans, private CollectionSync $collections,
        private EngineClient $engine) {
        parent::__construct($time);
        $this->setInterval(300);
        $this->setTimeSensitivity(IJob::TIME_SENSITIVE);
    }

    protected function run($argument): void {
        $this->scans->scanEverything();
        if ($this->engine->configured()) {
            $this->collections->syncAll($this->engine);
        }
    }
}
