<?php
declare(strict_types=1);

namespace OCA\MailVault\Service;

use OCP\Encryption\IManager as IEncryptionManager;
use OCP\Files\File;
use OCP\Files\Folder;
use OCP\Files\IRootFolder;
use OCP\IAppConfig;
use OCP\IGroupManager;
use OCP\IUserManager;
use Psr\Log\LoggerInterface;

/**
 * Keeps each user's MailVault collection ("All mail") in step with their scan folders.
 *
 * Every mail file found in the user's mapped folders (their own, plus folders an admin
 * mapped for them) is sent to the engine. When the engine can read the file in place
 * (Nextcloud's data directory mounted into the engine container, see the admin
 * "path map" setting) only its path is sent; otherwise the file is uploaded once.
 */
class CollectionSync {
    public function __construct(
        private ScanService $scans,
        private IRootFolder $rootFolder,
        private IAppConfig $appConfig,
        private IGroupManager $groups,
        private IUserManager $users,
        private IEncryptionManager $encryption,
        private LoggerInterface $logger,
        private \OCP\IConfig $config,
    ) {
    }

    /** @return list<array{0:string,1:string}> Nextcloud path prefix => engine path prefix */
    private function pathMap(): array {
        $out = [];
        foreach (preg_split('/\R/', $this->appConfig->getValueString('mailvault', 'path_map', '')) as $line) {
            $parts = array_map('trim', explode('=', $line, 2));
            if (count($parts) === 2 && $parts[0] !== '' && $parts[1] !== '') {
                $out[] = [rtrim($parts[0], '/'), rtrim($parts[1], '/')];
            }
        }
        return $out;
    }

    /** Where the engine can read this folder's files, or null when it has to receive uploads. */
    private function enginePath(Folder $folder): ?string {
        $map = $this->pathMap();
        if (!$map || $this->encryption->isEnabled()) {
            return null;
        }
        try {
            $storage = $folder->getStorage();
            if (!$storage->instanceOfStorage(\OC\Files\Storage\Local::class)) {
                return null;
            }
            $local = $storage->getLocalFile($folder->getInternalPath());
        } catch (\Throwable) {
            return null;
        }
        if (!is_string($local) || $local === '') {
            return null;
        }
        foreach ($map as [$from, $to]) {
            if ($local === $from || str_starts_with($local, $from . '/')) {
                return $to . substr($local, strlen($from));
            }
        }
        return null;
    }

    /** Mail files in every scan folder this user can see, with their engine paths where possible. */
    public function filesFor(string $uid): array {
        $folders = [];
        foreach ($this->scans->userFolders($uid) as $f) {
            $node = $this->scans->userFolderNode($uid, (int)$f['fileId']);
            if ($node !== null) {
                $folders[] = ["u-$uid-{$f['fileId']}", $node, $f['label'], $uid];
            }
        }
        foreach ($this->scans->mappings() as $mp) {
            if ($this->scans->mapVisible($mp, $uid) && ($node = $this->scans->mapNode($mp)) !== null) {
                $folders[] = ['m-' . $mp['id'], $node, $mp['label'], $mp['owner']];
            }
        }
        $files = [];
        foreach ($folders as [$key, $node, $label, $owner]) {
            $tree = $this->scans->scan($key, $node);
            $base = $this->enginePath($node);
            $walk = function (array $t, string $rel) use (&$walk, &$files, $base, $label, $owner, $key) {
                foreach ($t['files'] ?? [] as $f) {
                    $id = (string)$f['fileId'];
                    if (isset($files[$id])) {
                        continue;                       // same file reachable from two scan folders
                    }
                    $files[$id] = ['id' => $id, 'name' => $f['name'], 'size' => (int)$f['size'], 'mtime' => (int)$f['mtime'],
                        'folder' => $label, 'where' => ltrim($rel, '/'),
                        'path' => $base !== null ? $base . $rel . '/' . $f['name'] : '',
                        '_owner' => $owner];
                }
                foreach ($t['dirs'] ?? [] as $d) {
                    $walk($d, $rel . '/' . $d['name']);
                }
            };
            $walk($tree, '');
        }
        return array_values($files);
    }

    /** Send the user's file list to the engine and upload what it cannot read in place. */
    public function sync(string $uid, EngineClient $engine): array {
        $files = $this->filesFor($uid);
        $owners = [];
        foreach ($files as &$f) {
            $owners[$f['id']] = $f['_owner'];
            unset($f['_owner']);
        }
        unset($f);
        $accounts = json_decode($this->config->getUserValue($uid, 'mailvault', 'accounts', '[]'), true) ?: [];
        [$code, $body] = $engine->request($uid, 'POST', 'api/collection/sync', json_encode(['files' => $files, 'accounts' => $accounts]),
            ['Content-Type: application/json']);
        $res = json_decode($body, true);
        if ($code !== 200 || !is_array($res)) {
            throw new \RuntimeException('MailVault engine refused the collection: ' . substr($body, 0, 200));
        }
        $uploaded = 0;
        foreach ($res['need_upload'] ?? [] as $id) {
            $owner = $owners[$id] ?? $uid;
            $node = $this->rootFolder->getUserFolder($owner)->getById((int)$id)[0] ?? null;
            if (!$node instanceof File) {
                continue;
            }
            $stream = $node->fopen('rb');
            try {
                [$c] = $engine->request($uid, 'POST', 'api/collection/upload?id=' . rawurlencode((string)$id), $stream,
                    ['Content-Type: application/octet-stream'], $node->getSize());
                if ($c === 200) {
                    $uploaded++;
                }
            } catch (\Throwable $e) {
                $this->logger->warning("MailVault could not upload {$node->getName()} for $uid: " . $e->getMessage());
            } finally {
                if (is_resource($stream)) {
                    fclose($stream);
                }
            }
        }
        return ['files' => count($files), 'uploaded' => $uploaded, 'in_place' => count($files) - count($res['need_upload'] ?? []),
            'accounts' => $res['accounts'] ?? []];
    }

    /** Background job: every user who has scan folders or can see a mapped folder. */
    public function syncAll(EngineClient $engine): void {
        $maps = $this->scans->mappings();
        $this->users->callForSeenUsers(function ($user) use ($engine, $maps) {
            $uid = $user->getUID();
            $has = $this->scans->userFolders($uid) !== [];
            foreach ($maps as $mp) {
                $has = $has || $this->scans->mapVisible($mp, $uid);
            }
            if (!$has) {
                return;
            }
            try {
                $this->sync($uid, $engine);
            } catch (\Throwable $e) {
                $this->logger->warning("MailVault collection sync for $uid failed: " . $e->getMessage());
            }
        });
    }
}
