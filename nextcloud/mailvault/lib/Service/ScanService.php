<?php
declare(strict_types=1);

namespace OCA\MailVault\Service;

use OCP\Files\Folder;
use OCP\Files\IAppData;
use OCP\Files\IRootFolder;
use OCP\Files\NotFoundException;
use OCP\IAppConfig;
use OCP\IConfig;
use OCP\IGroupManager;
use OCP\IUserManager;
use Psr\Log\LoggerInterface;

/**
 * Scan folders: the Nextcloud folders a user (or an admin, for others) maps to
 * MailVault. Each folder is walked for mail files; the result is cached so the
 * library opens instantly, and a background job keeps the cache fresh.
 */
class ScanService {
    public const MAIL_EXT = ['pst', 'ost', 'msg', 'eml', 'emlx', 'mbox', 'mbx', 'zip', 'tar', 'tgz', 'gz', '7z', 'rar', 'bz2', 'xz'];
    public const MAX_FILES = 500000;

    public function __construct(
        private IRootFolder $rootFolder,
        private IConfig $config,
        private IAppConfig $appConfig,
        private IAppData $appData,
        private IGroupManager $groups,
        private IUserManager $users,
        private LoggerInterface $logger,
    ) {
    }

    // ------------------------------------------------ a user's own scan folders
    /** @return list<array{fileId:int,label:string}> */
    public function userFolders(string $uid): array {
        $list = json_decode($this->config->getUserValue($uid, 'mailvault', 'ncfolders', '[]'), true);
        return is_array($list) ? array_values($list) : [];
    }

    public function saveUserFolders(string $uid, array $list): void {
        $this->config->setUserValue($uid, 'mailvault', 'ncfolders', json_encode(array_values($list)));
    }

    public function userFolderNode(string $uid, int $fileId): ?Folder {
        try {
            $node = $this->rootFolder->getUserFolder($uid)->getById($fileId)[0] ?? null;
        } catch (\Throwable) {
            return null;
        }
        return $node instanceof Folder ? $node : null;
    }

    // ------------------------------------------------ folders an admin maps for users and groups
    /** @return list<array{id:int,owner:string,fileId:int,path:string,label:string,mode:string,who:list<string>}> */
    public function mappings(): array {
        $list = json_decode($this->appConfig->getValueString('mailvault', 'mappings', '[]'), true);
        return is_array($list) ? array_values($list) : [];
    }

    public function saveMappings(array $list): void {
        $this->appConfig->setValueString('mailvault', 'mappings', json_encode(array_values($list)));
    }

    public function mapVisible(array $mp, string $uid): bool {
        if ($this->groups->isAdmin($uid) || $mp['owner'] === $uid || $mp['mode'] === 'everyone') {
            return true;
        }
        if ($mp['mode'] !== 'list') {
            return false;
        }
        $user = $this->users->get($uid);
        $mine = array_map('strtolower', array_merge([$uid], $user ? $this->groups->getUserGroupIds($user) : []));
        foreach ($mp['who'] as $w) {
            if (in_array(strtolower($w), $mine, true)) {
                return true;
            }
        }
        return false;
    }

    /** Mapped folders are read through the storage of the admin who mapped them. */
    public function mapNode(array $mp): ?Folder {
        return $this->userFolderNode($mp['owner'], (int)$mp['fileId']);
    }

    // ------------------------------------------------ scanning and the cache
    public function scanTree(Folder $folder): array {
        $count = 0;
        $walk = function (Folder $f) use (&$walk, &$count): array {
            $dirs = [];
            $files = [];
            foreach ($f->getDirectoryListing() as $n) {
                if ($count >= self::MAX_FILES || str_starts_with($n->getName(), '.')) {
                    continue;
                }
                if ($n instanceof Folder) {
                    $sub = $walk($n);
                    if ($sub['files'] || $sub['dirs']) {
                        $dirs[] = $sub;
                    }
                } elseif (in_array(strtolower(pathinfo($n->getName(), PATHINFO_EXTENSION)), self::MAIL_EXT, true)) {
                    $files[] = ['name' => $n->getName(), 'fileId' => $n->getId(), 'size' => $n->getSize(),
                        'mtime' => $n->getMTime()];
                    $count++;
                }
            }
            usort($dirs, fn ($a, $b) => strcasecmp($a['name'], $b['name']));
            usort($files, fn ($a, $b) => strcasecmp($a['name'], $b['name']));
            return ['name' => $f->getName(), 'dirs' => $dirs, 'files' => $files];
        };
        $tree = $walk($folder);
        return $tree + ['count' => $count, 'truncated' => $count >= self::MAX_FILES, 'scanned' => time()];
    }

    /** Cache key: "u-<uid>-<fileId>" for a user's own folder, "m-<id>" for an admin mapping. */
    private function cacheName(string $key): string {
        return preg_replace('/[^A-Za-z0-9_.@-]/', '_', $key) . '.json';
    }

    public function cached(string $key): ?array {
        try {
            $data = json_decode($this->appData->getFolder('scans')->getFile($this->cacheName($key))->getContent(), true);
            return is_array($data) ? $data : null;
        } catch (NotFoundException) {
            return null;
        } catch (\Throwable $e) {
            $this->logger->debug('MailVault scan cache read failed: ' . $e->getMessage());
            return null;
        }
    }

    private function store(string $key, array $tree): void {
        try {
            try {
                $dir = $this->appData->getFolder('scans');
            } catch (NotFoundException) {
                $dir = $this->appData->newFolder('scans');
            }
            $name = $this->cacheName($key);
            try {
                $dir->getFile($name)->putContent(json_encode($tree));
            } catch (NotFoundException) {
                $dir->newFile($name, json_encode($tree));
            }
        } catch (\Throwable $e) {
            $this->logger->warning('MailVault could not cache a folder scan: ' . $e->getMessage());
        }
    }

    public function forget(string $key): void {
        try {
            $this->appData->getFolder('scans')->getFile($this->cacheName($key))->delete();
        } catch (\Throwable) {
        }
    }

    /** Cached tree when fresh enough (or $refresh is false and any cache exists), otherwise a new scan. */
    public function scan(string $key, Folder $folder, bool $refresh = false): array {
        if (!$refresh && ($c = $this->cached($key)) !== null) {
            return $c;
        }
        $tree = $this->scanTree($folder);
        $this->store($key, $tree);
        return $tree;
    }

    /** Background job: rescan every user's folders and every admin mapping. Returns folders scanned. */
    public function scanEverything(): int {
        $n = 0;
        foreach ($this->mappings() as $mp) {
            $folder = $this->mapNode($mp);
            if ($folder !== null) {
                $this->scan('m-' . $mp['id'], $folder, true);
                $n++;
            }
        }
        $this->users->callForSeenUsers(function ($user) use (&$n) {
            $uid = $user->getUID();
            foreach ($this->userFolders($uid) as $f) {
                $folder = $this->userFolderNode($uid, (int)$f['fileId']);
                if ($folder !== null) {
                    try {
                        $this->scan("u-$uid-{$f['fileId']}", $folder, true);
                        $n++;
                    } catch (\Throwable $e) {
                        $this->logger->warning("MailVault scan of a folder for $uid failed: " . $e->getMessage());
                    }
                }
            }
        });
        return $n;
    }
}
