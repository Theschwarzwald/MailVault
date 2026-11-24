<?php
declare(strict_types=1);

namespace OCA\MailVault\Settings;

use OCP\AppFramework\Http\TemplateResponse;
use OCP\IGroupManager;
use OCP\IURLGenerator;
use OCP\IUserSession;
use OCP\Settings\ISettings;
use OCP\Util;

/** Settings → MailVault: the folders MailVault scans for mail files. */
class Personal implements ISettings {
    public function __construct(private IUserSession $session, private IGroupManager $groups, private IURLGenerator $url) {
    }

    public function getForm(): TemplateResponse {
        Util::addScript('mailvault', 'personal');
        Util::addStyle('mailvault', 'personal');
        $u = $this->session->getUser();
        return new TemplateResponse('mailvault', 'personal', [
            'app_url' => $this->url->linkToRoute('mailvault.page.index'),
            'is_admin' => $u !== null && $this->groups->isAdmin($u->getUID()),
        ], '');
    }

    public function getSection(): string {
        return 'mailvault';
    }

    public function getPriority(): int {
        return 10;
    }
}
