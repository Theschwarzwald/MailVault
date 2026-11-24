<?php
declare(strict_types=1);

namespace OCA\MailVault\Settings;

use OCP\IL10N;
use OCP\IURLGenerator;
use OCP\Settings\IIconSection;

/** "MailVault" entry in each user's Personal settings. */
class PersonalSection implements IIconSection {
    public function __construct(private IURLGenerator $url, private IL10N $l) {
    }

    public function getID(): string {
        return 'mailvault';
    }

    public function getName(): string {
        return 'MailVault';
    }

    public function getPriority(): int {
        return 80;
    }

    public function getIcon(): string {
        return $this->url->imagePath('mailvault', 'app-dark.svg');
    }
}
