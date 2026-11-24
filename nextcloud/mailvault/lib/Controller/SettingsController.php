<?php
declare(strict_types=1);

namespace OCA\MailVault\Controller;

use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\JSONResponse;
use OCP\IAppConfig;
use OCP\IRequest;

class SettingsController extends Controller {
    public function __construct(string $appName, IRequest $request, private IAppConfig $config) {
        parent::__construct($appName, $request);
    }

    /** Admin only (no #[NoAdminRequired]). */
    public function save(string $engineUrl, string $engineToken = '', ?string $pathMap = null): JSONResponse {
        $engineUrl = trim($engineUrl);
        if ($engineUrl !== '' && !preg_match('#^https?://#', $engineUrl)) {
            return new JSONResponse(['error' => 'Engine URL must start with http:// or https://'], 400);
        }
        $this->config->setValueString('mailvault', 'engine_url', $engineUrl);
        if ($pathMap !== null) {
            $this->config->setValueString('mailvault', 'path_map', trim($pathMap));
        }
        if ($engineToken !== '') {
            $this->config->setValueString('mailvault', 'engine_token', $engineToken, false, true);
        }
        $ok = false;
        $msg = '';
        if ($engineUrl !== '') {
            $ch = curl_init(rtrim($engineUrl, '/') . '/api/config');
            curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 8,
                CURLOPT_HTTPHEADER => ['X-MailVault-Token: ' . $this->config->getValueString('mailvault', 'engine_token', ''),
                    'X-MailVault-User: admin-check']]);
            $out = curl_exec($ch);
            $code = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
            $msg = $out === false ? curl_error($ch) : "HTTP $code";
            curl_close($ch);
            $ok = $code === 200;
        }
        return new JSONResponse(['ok' => $ok, 'message' => $ok ? 'Connected to the MailVault engine' : "Saved, but the engine check failed ($msg)"]);
    }
}
