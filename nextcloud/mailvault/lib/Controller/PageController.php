<?php
declare(strict_types=1);

namespace OCA\MailVault\Controller;

use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\Attribute\NoCSRFRequired;
use OCP\AppFramework\Http\ContentSecurityPolicy;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\IAppConfig;
use OCP\IRequest;
use OCP\Util;

class PageController extends Controller {
    public function __construct(string $appName, IRequest $request, private IAppConfig $config) {
        parent::__construct($appName, $request);
    }

    #[NoAdminRequired]
    #[NoCSRFRequired]
    public function index(): TemplateResponse {
        Util::addStyle('mailvault', 'app');
        Util::addStyle('mailvault', 'nextcloud');
        Util::addScript('mailvault', 'nextcloud-host');
        Util::addScript('mailvault', 'app');
        $resp = new TemplateResponse('mailvault', 'main', [
            'configured' => $this->config->getValueString('mailvault', 'engine_url') !== '',
        ]);
        $csp = new ContentSecurityPolicy();
        $csp->addAllowedFrameDomain("'self'");
        $csp->addAllowedImageDomain('data:');
        $csp->addAllowedImageDomain('blob:');
        $resp->setContentSecurityPolicy($csp);
        return $resp;
    }
}
