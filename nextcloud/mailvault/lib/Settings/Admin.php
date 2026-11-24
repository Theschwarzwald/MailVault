<?php
declare(strict_types=1);

namespace OCA\MailVault\Settings;

use OCP\AppFramework\Http\TemplateResponse;
use OCP\IAppConfig;
use OCP\Settings\ISettings;
use OCP\Util;

class Admin implements ISettings {
    public function __construct(private IAppConfig $config) {
    }

    public function getForm(): TemplateResponse {
        Util::addScript('mailvault', 'admin');
        return new TemplateResponse('mailvault', 'admin', [
            'engine_url' => $this->config->getValueString('mailvault', 'engine_url'),
            'path_map' => $this->config->getValueString('mailvault', 'path_map', ''),
            'has_token' => $this->config->getValueString('mailvault', 'engine_token', '') !== '',
        ], '');
    }

    public function getSection(): string {
        return 'additional';
    }

    public function getPriority(): int {
        return 50;
    }
}
