<?php
declare(strict_types=1);

namespace OCA\MailVault\Service;

use OCP\IAppConfig;
use OCP\IGroupManager;
use OCP\IUserManager;

/** Talks to the MailVault engine on behalf of a given user (also usable from background jobs). */
class EngineClient {
    public function __construct(private IAppConfig $config, private IGroupManager $groups, private IUserManager $users) {
    }

    public function configured(): bool {
        return $this->config->getValueString('mailvault', 'engine_url') !== ''
            && $this->config->getValueString('mailvault', 'engine_token', '') !== '';
    }

    /**
     * @param resource|string|null $body
     * @return array{0:int,1:string}
     */
    public function request(string $uid, string $method, string $path, $body = null, array $headers = [], ?int $size = null): array {
        $url = rtrim($this->config->getValueString('mailvault', 'engine_url'), '/');
        $token = $this->config->getValueString('mailvault', 'engine_token', '');
        if ($url === '' || $token === '') {
            throw new \RuntimeException('MailVault engine is not configured');
        }
        $user = $this->users->get($uid);
        $groupIds = $user ? $this->groups->getUserGroupIds($user) : [];
        $headers = array_merge($headers, ['X-MailVault-Token: ' . $token, 'X-MailVault-User: ' . $uid, 'Expect:',
            'X-MailVault-Admin: ' . ($this->groups->isAdmin($uid) ? '1' : '0'),
            'X-MailVault-Groups: ' . implode(',', array_map('rawurlencode', $groupIds))]);
        $ch = curl_init($url . '/' . ltrim($path, '/'));
        curl_setopt_array($ch, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_HTTPHEADER => $headers,
            CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 3600, CURLOPT_CONNECTTIMEOUT => 10]);
        if (is_resource($body)) {
            curl_setopt($ch, CURLOPT_UPLOAD, true);
            curl_setopt($ch, CURLOPT_INFILE, $body);
            if ($size !== null) {
                curl_setopt($ch, CURLOPT_INFILESIZE, $size);
            }
            curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
        } elseif ($body !== null) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
        }
        $out = curl_exec($ch);
        if ($out === false) {
            throw new \RuntimeException("Can't reach the MailVault engine: " . curl_error($ch));
        }
        return [(int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE), (string)$out];
    }
}
